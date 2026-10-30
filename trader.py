"""Rule-based, two-directional paper-trading bot that follows the real US rules for a small margin account.

Model: an Interactive Brokers (IBKR Pro, Tiered) margin account under Regulation T and FINRA Rule 4210.
Runs every 5 minutes during US market hours (a GitHub Actions job loops until the close). Each run:
  1. marks the portfolio to market with Yahoo Finance 5-minute bars,
  2. accrues margin interest and short borrow fees, applies deposits (deposits.json), books dividends,
  3. replays the bars since the last run against each stock position's disaster stop (25 % against cost),
  4. checks maintenance margin (FINRA 4210) and the 3x exposure cap,
  5. once per day after 10:00 New York time (strategy v5):
     - index sleeve (50 %): direction from SPY's 200/50-day trend, size = 25 % / QQQ's realised 20-day volatility,
       capped at 3x, built from cash-funded QQQ + TQQQ (long) or SQQQ (short);
     - momentum basket (50 %): monthly, the 10 strongest stocks (12-1 month) above their 200-day average while
       SPY is above its own; cash when SPY is below,
  6. logs every rule that limited or shaped a decision (compliance log) and a simple tax view
     (holding period, wash sales, dividends), and writes everything to ledger.json.

No AI and no paid service is involved; all decisions follow the fixed rules below.
"""
import json
import math
import os
import sys
import traceback
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import universe as U

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / "ledger.json"
DEPOSITS = ROOT / "deposits.json"
RESTORE = ROOT / "v3_restore.json"
ENGINE = "trend-event v6"

# ---- broker model: IBKR Pro (Tiered), US stocks/ETFs
SLIP = 0.0005                            # 0.05 % slippage on every fill
COMM_PER_SHARE, COMM_MIN, COMM_MAX_PCT = 0.0035, 0.35, 0.01
SEC_FEE_RATE = 20.60 / 1_000_000         # SEC Section 31 fee on sales (since 2026-04-04)
TAF_PER_SHARE, TAF_MAX = 0.000195, 9.79  # FINRA trading activity fee on sales (since 2026-01-01)
MARGIN_RATE = 0.0538                     # IBKR USD margin loan rate, < 100k (Sept 2026)
BORROW_RATE = 0.005                      # assumed fee for easy-to-borrow large caps
MIN_TRADE = 10.0

# ---- Regulation T / FINRA 4210
MIN_MARGIN_EQUITY = 2000.0               # FINRA 4210(b)(4): every short sale and margin purchase
REGT_INIT = 0.50                         # initial margin for stocks (long: 50 %, short: 150 % incl. proceeds)
MAINT_LONG, MAINT_SHORT = 0.25, 0.30     # FINRA minimum maintenance; x leverage for leveraged ETFs
SHORT_MIN_PER_SHARE = 5.0                # short maintenance at least 5 $/share for stocks >= 5 $
BUDGET_USE = 0.95                        # use at most 95 % of Reg T buying power at the daily rebalance
SSR_DROP = 0.10                          # SEC Rule 201 trigger (10 % below prior close)
GROSS_ECON_CAP = 3.0                     # never more than 3x economic exposure

# ---- portfolio construction
N_LONG, N_SHORT = 4, 3
KEEP_RANK_LONG, KEEP_RANK_SHORT = 10, 10
MIN_SHORT_PRICE = 10.0
REGIMES = {   # share of the Reg T budget per sleeve
    "bull":    {"label": "Güçlü yükseliş", "longStocks": 0.55, "bullEtf": 0.20, "bearEtf": 0.0, "shorts": 0.25},
    "neutral": {"label": "Kararsız piyasa", "longStocks": 0.60, "bullEtf": 0.0, "bearEtf": 0.0, "shorts": 0.40},
    "bear":    {"label": "Düşüş piyasası", "longStocks": 0.30, "bullEtf": 0.0, "bearEtf": 0.25, "shorts": 0.45},
}
HIGH_VIX, HIGH_VIX_BUDGET = 35.0, 0.60

# ---- risk
STOP_LONG = {1: (0.92, 0.88), 3: (0.85, 0.80)}   # (x cost, x highest since entry)
STOP_SHORT = (1.08, 1.12)                        # (x cost, x lowest since entry)
TAKE_PROFIT = 0.20
SHOCK_PCT = 7.0
BRAKE1, BRAKE2, SQUEEZE = -3.0, -5.0, 3.0         # SPY day change thresholds (%)
LONG_CAP_BRAKE1, LONG_CAP_BRAKE2, SHORT_CAP_SQUEEZE = 1.0, 0.5, 0.5   # x equity (economic exposure)
STALE_MIN = 35
DAILY_AFTER = dtime(10, 0)
CHECK_DAYS_KEPT = 5
COMPLIANCE_KEPT = 400
WASH_DAYS = 30

# ---- strategy v6 (from 2 Nov 2026, fresh 3.000 $ account): 50 % two-directional, volatility-targeted index sleeve
# + 50 % news-shock sleeve (big opening gaps that the market reverses in the first hour). The momentum basket is off.
INDEX_ETF, INDEX_BULL, INDEX_BEAR = "QQQ", "TQQQ", "SQQQ"   # 1x, 3x and -3x Nasdaq 100
INDEX_SLEEVE = (INDEX_ETF, INDEX_BULL, INDEX_BEAR)
INDEX_SHARE, BASKET_SHARE, EVENT_SHARE = 0.50, 0.0, 0.50   # share of equity per sleeve (basket off since v6)
DIR_SRC = "SPY"                           # direction from SPY's 200/50-day trend (QQQ's own trend tested worse)
MAX_SHORT_EXPO = 3.0                      # short side at most -3x
VOL_TARGET, VOL_FLOOR, MAX_EXPO = 0.25, 0.05, 3.0   # sleeve exposure = 25 % / realised 20-day vol, at most 3x
VT_BAND = 0.30                            # trade the index sleeve only when its exposure is >= 0.3x off target
BASKET_SHORTS = False                     # in a market down-trend: short the weakest names (True) or hold cash (False)
BASKET_N, BASKET_KEEP, GROUP_CAP = 10, 20, 3
DISASTER, COOLDOWN_DAYS = 0.25, 30        # single-stock stop 25 % against cost, then 30 days out
INVEST_USE = 0.98                         # keep ~2 % cash for costs; never borrow
CORE_ETF = INDEX_ETF
# news-shock sleeve (tested in event_study.py, Dec 2023 - Oct 2026)
EVENT_GAP = 0.08                          # opening gap of at least 8 % against the previous close
EVENT_MIN_MOVE = 0.005                    # first hour must move at least 0.5 % against the gap
EVENT_AFTER, EVENT_LAST_ENTRY = dtime(10, 30), dtime(11, 30)   # decide after the first hour
EVENT_HOLD_DAYS = 2                       # exit at the close two trading days after entry
EVENT_EXIT_AFTER = dtime(15, 50)
EVENT_SLOTS, EVENT_RISK, EVENT_CAP = 4, 0.01, 0.25   # max open trades; risk 1 % of the sleeve per trade; 25 % cap
SPIKE_MOVE = 0.30                         # sert yükseliş short'u: previous day's close at least 30 % above the day before
SPIKE_MIN_PRICE, SPIKE_MIN_DV = 5.0, 50.0  # real price >= 5 $ the day before, 20-day average >= 50 M $ traded a day
SPIKE_HOLD_DAYS = 10                      # trading days including the entry day; cover at the close of the last one
SPIKE_STOP = 0.50                         # stop 50 % above the entry price
SPIKE_SIZE, SPIKE_SLOTS = 0.05, 6         # each trade 5 % of the event sleeve; at most 6 open
SPIKE_BORROW = 0.30                       # assumed borrow fee for freshly spiked small stocks (hard to borrow), per year
SPIKE_SLIP = 0.0015                       # wider spread than large caps: 0.15 % per fill
SPIKE_AFTER, SPIKE_LAST_ENTRY = dtime(9, 35), dtime(10, 30)
SANCTIONED_NAMES = ("SMIC", "Semiconductor Manufacturing International", "Hikvision", "SenseTime", "Sugon", "Dawning",
                    "China Mobile", "China Telecom", "China Unicom", "Inspur", "DJI", "Megvii", "iFlytek", "Dahua")
MOVERS = ROOT / "movers.json"
SPIKE_TEST_KEY = "fadeUp|r5_dv50|t30|s50|h10"   # spike_results.json candidate that matches these settings
SPIKE_RULE_TEXT = (
    "Sert yükseliş short'u: bir Nasdaq hissesi bir günde en az %30 yükselerek kapanırsa ertesi sabah açılışta (09:35 ET) "
    "short açılır. Şartlar: önceki gün fiyatı en az 5 $, son 20 günde günde ortalama en az 50 milyon $ işlem (ödünç "
    "bulunabilecek kadar büyük ve likit), satın alma haberiyle yatay seyretmiyor, ABD yaptırım listesinde değil, o sabah "
    "%10'dan fazla düşmemiş (SEC Rule 201). Büyüklük haber kolunun %5'i, aynı anda en fazla 6 işlem; zarar-kes girişin "
    "%50 üstü; çıkış 10. işlem gününün kapanışında. Ödünç ücreti yıllık %30 varsayılır. Gerekçe: 2019–2026 testinde bu "
    "tür sıçramalar sonraki iki haftada çoğunlukla geri verildi.")
RESET_CAPITAL = 3000.0
DIP_LEVELS = (-0.10, -0.20)               # Nasdaq 100 watch: correction / bear-market thresholds from the all-time high
DIP_NO_SHORT = True                       # after a 20 % fall from the high (until a new high) the index sleeve never shorts
DIP_RULE_TEXT = (
    "Kural: Nasdaq 100 (QQQ) zirvesinden %20 veya daha fazla düşmüşse endeks kolu short açmaz; yön aşağı olsa bile yeni "
    "zirveye kadar nakitte bekler. O noktada short geç kalmıştır ve ayı piyasalarının sert tepki yükselişlerine yakalanır. "
    "1999–2026 testinde bu kural endeks kolunun yıllık getirisini %14,7'den %15,4'e çıkardı, en kötü düşüşünü %−46'dan "
    "%−43'e indirdi; 2018–2026'da %17,0'dan %20,2'ye çıkardı. Düşüşte kaldıraçla almak ise 2000 çöküşünde hesabı sıfırlardı, "
    "bu yüzden kullanılmıyor.")

# ---- 3-month evaluation before any real money (v3 ledgers: 12 Oct 2026 - 12 Jan 2027; v4 sets its own dates)
EVAL_START, EVAL_START_TS, EVAL_END = "2026-10-12", "2026-10-12T14:07:00-04:00", "2027-01-12"
EVAL_MAX_DD, EVAL_UPTIME = -20.0, 95.0          # criteria: drawdown not worse than -20 %, bot ran on >= 95 % of days
SETUP_RULES = {"geçiş", "aracı kurum ücreti", "para yatırma"}

HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
}

YF_SOURCE = {"title": "yfinance (Yahoo Finance verileri)", "url": "https://github.com/ranaroussi/yfinance"}
LEGAL = {
    "regt": {"title": "FINRA Rule 4210 (kredi/teminat kuralları, Reg T ile birlikte)",
             "url": "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210"},
    "min2000": {"title": "FINRA Regulatory Notice 21-24: 2.000 $ asgari özsermaye",
                "url": "https://www.finra.org/rules-guidance/notices/21-24"},
    "pdt": {"title": "FINRA: gün içi teminat standartları PDT kuralının yerini aldı (SEC onayı 14 Nis 2026)",
            "url": "https://www.finra.org/compliance-tools/weekly-archive/04152026"},
    "letf": {"title": "FINRA Regulatory Notice 09-53: kaldıraçlı ETF teminatı",
             "url": "https://www.finra.org/rules-guidance/notices/09-53"},
    "ssr": {"title": "SEC Rule 201 (alternatif uptick kuralı)", "url": "https://www.sec.gov/news/press/2010/2010-26.htm"},
    "secfee": {"title": "SEC Section 31 ücret oranı (2026)", "url": "https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2"},
    "ibkr_comm": {"title": "IBKR komisyonları (Pro Tiered)", "url": "https://investors.interactivebrokers.com/en/pricing/commissions-home.php"},
    "ibkr_margin": {"title": "IBKR kredi faiz oranları", "url": "https://interactivebrokers.ca/en/index.php?f=47205"},
    "ibkr_frac": {"title": "IBKR kesirli hisse (açığa satış dahil)", "url": "https://interactivebrokers.ie/en/trading/fractional-trading.php"},
    "irs409": {"title": "IRS Topic 409: sermaye kazançları ve kayıpları", "url": "https://www.irs.gov/taxtopics/tc409"},
    "irs550": {"title": "IRS Publication 550: wash sale ve açığa satış", "url": "https://www.irs.gov/publications/p550"},
}

SCHEDULE_TEXT = ("Piyasa açıkken 5 dakikada bir (GitHub Actions döngüsü). Günlük rejim, sıralama ve "
                 "alım-satımlar 10:00'dan (New York) sonraki ilk çalışmada.")
CONFIG_TEXT = {
    "strategyName": "İki yönlü endeks kolu + haber şoku kolu (v6, 2 Kasım 2026'dan itibaren yeni 3.000 $ hesap)",
    "strategySummary": (
        "Paranın yarısı Nasdaq 100'de, S&P 500 trendinin yönünde ve oynaklığa göre ayarlanan kaldıraçla (−3x ile +3x arası): "
        "yükselişte QQQ ve 3x fon TQQQ, teyitli düşüşte −3x ters fon SQQQ. Diğer yarı haber şoku kolunda: bir hisse "
        "haberle önceki kapanışa göre en az %8 boşlukla açılır ve ilk saatte piyasa bu tepkinin tersine dönerse o yöne girilir "
        "(sert düşüp toparlanana long, sert yükselip sönene short), 2 işlem günü tutulur, zarar-kes ilk saatin öbür ucunda. "
        "Aynı kolun ikinci tetiği (Kasım 2026'dan itibaren): bir günde %30+ yükselen likit Nasdaq hissesine ertesi sabah short, "
        "10 işlem günü tutulur. "
        "Aralık 2023 – Ekim 2026 testinde (164 hisse) bu kural işlem başına ortalama %+0,74 getirdi ve dönemin iki yarısında "
        "da kârlıydı; haberin yönünde girmek ise zarar ettirdi. Kâr garantisi yoktur: işlemlerin yarısından fazlası zararla "
        "kapandı, kâr kazananların daha büyük olmasından geliyor. Önceki hesap (8–30 Ekim) arşivlendi."),
    "execution": (
        "Yahoo Finance verileri (yfinance): kararlar bir önceki günün kapanış fiyatlarıyla, işlemler 5 dakikalık güncel "
        "fiyatla. Kesirli hisse. Her emirde IBKR Pro Tiered komisyonu: hisse başına 0,0035 $, en az 0,35 $, en fazla işlem "
        "tutarının %1'i. Satışlarda SEC ücreti (milyon $'da 20,60 $) ve FINRA TAF. %0,05 fiyat kayması. Kaldıraç 3x "
        "fonlarla sağlanır ve hepsi nakitle alınır; kredi faizi ödenmez. Temettüler hesaba geçer."),
    "broker": "Interactive Brokers, IBKR Pro (Tiered) kredili hesap modeli",
    "rules": [
        "Yön kararı (her gün 10:00'dan sonra, dünkü kapanışla): S&P 500 (SPY) 200 günlük ortalamasının üstündeyse yön YUKARI "
        "(long). Altındaysa ve 50 günlük ortalaması da 200 günlüğün altına inmişse yön AŞAĞI (short). Arada kalırsa kararsız: "
        "endeks kolu nakitte bekler. Geriye dönük testte yönü QQQ'nun kendi trendinden almak daha kötü sonuç verdi (en büyük "
        "düşüş %50); geniş piyasa daha az yanlış sinyal üretiyor.",
        "Endeks kolu (paranın %50'si): hedef maruziyet = %25 ÷ QQQ'nun son 20 gündeki yıllık oynaklığı, en fazla 3x. "
        "Oynaklık %12 iken 2x, %25 iken 1x, %40 iken 0,6x. Long'da QQQ ile 3x fon TQQQ karıştırılır, short'ta −3x ters fon "
        "SQQQ alınır. Hedefle mevcut maruziyet arasındaki fark 0,3x'ten küçükse işlem yapılmaz (gereksiz komisyon olmasın).",
        "Nasdaq düşüş kuralı: Nasdaq 100 (QQQ) zirvesinden %20 veya daha fazla düşmüşse endeks kolu short açmaz, yeni zirveye "
        "kadar nakitte bekler (yön yukarıysa long kuralı aynen çalışır). Sayfanın kenarındaki panel zirveden uzaklığı canlı "
        "gösterir.",
        "Haber şoku kolu (paranın %50'si): her gün 10:30'da (ilk saatten sonra) 166 büyük hisse taranır. Önceki kapanışa göre "
        "en az %8 boşlukla açılan ve ilk saatte haberin tersine en az %0,5 dönen hisseye o yönde girilir: düşüşle açılıp "
        "toparlanana long, yükselişle açılıp sönene short. Haberin yönünde devam edenlere girilmez (testte zarar ettirdi).",
        "Haber şoku risk kuralları: zarar-kes ilk saatin en düşük (long) ya da en yüksek (short) seviyesinin hemen ötesi; "
        "büyüklük, zarar-keste kaybın kolun yaklaşık %1'i olacağı şekilde ayarlanır (bir işlem en fazla kolun %25'i); "
        "aynı anda en fazla 4 işlem; çıkış 2 işlem günü sonra kapanışta. SEC Rule 201 yüzünden %10'dan fazla düşmüş "
        "hisseye short açılmaz; özsermaye 2.000 $'ın altına inerse short açılmaz (FINRA 4210).",
        SPIKE_RULE_TEXT,
        "Kaldıraç sınırı: endeks kolu en fazla 3x, haber kolu 1x; hesap düzeyindeki 3x sınırı, Reg T başlangıç teminatı ve "
        "FINRA 4210 bakım teminatı her emirde ve her çalışmada kontrol edilir. 3x fonlar için FINRA kaldıraçlı ETF teminatı %75'tir.",
        "3x ve −3x fonlar günlük getiriyi üçe katlar; dalgalı ve yatay piyasada zamanla değer kaybeder (oynaklık erimesi) ve "
        "tek bir kötü günde endeks %10 düşerse TQQQ yaklaşık %30 düşer. Bu yüzden yalnızca trend belirgin ve oynaklık "
        "düşükken büyük tutulur.",
        "Vergi (öğrenme amaçlı, basitleştirilmiş): 1 yıl veya daha kısa tutulan pozisyonların kazancı kısa vadelidir ve normal "
        "gelir gibi vergilenir. Zararla satılan hisse 30 gün içinde yeniden alınırsa (wash sale) zarar o yıl düşülemez. Net "
        "sermaye zararının yılda en fazla 3.000 $'ı gelirden düşülebilir. Vergi tavsiyesi değildir.",
        "Veri 35 dakikadan eskiyse o varlıkta işlem yapılmaz. Her işlem fiyatı, saati, kaynağı, komisyonu, ücretleri ve "
        "tetikleyen kuralla kaydedilir.",
    ],
}


# ---------------------------------------------------------------- formatting
def trn(x, d=2):
    s = f"{abs(x):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("−" if x < 0 else "") + s


def trpct(x, d=2):
    return ("+" if x > 0 else "−" if x < 0 else "") + "%" + trn(abs(x), d)


def usd(x):
    return trn(x, 2) + " $"


def iso(ts):
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    return ts.astimezone(NY).isoformat(timespec="seconds")


def hm(ts):
    return pd.Timestamp(ts).tz_convert(NY).strftime("%H:%M")


def src(t):
    return {"title": f"Yahoo Finance: {t}", "url": U.quote_url(t)}


def num(x):
    """Ledger values can be missing or null in old entries."""
    return float(x or 0)


def floor4(x):
    return math.floor(x * 10000 + 1e-9) / 10000


def commission(shares, value):
    """IBKR Pro Tiered: 0.0035 $/share, min 0.35 $, max 1 % of trade value."""
    return round(min(max(COMM_MIN, COMM_PER_SHARE * shares), COMM_MAX_PCT * value), 2)


def sale_fees(value, shares):
    """SEC fee + FINRA TAF on a sale (both rounded up to the cent, minimum 1 cent)."""
    if value <= 0:
        return 0.0
    sec = max(0.01, math.ceil(value * SEC_FEE_RATE * 100 - 1e-9) / 100)
    taf = min(TAF_MAX, max(0.01, math.ceil(shares * TAF_PER_SHARE * 100 - 1e-9) / 100))
    return round(sec + taf, 2)


def init_rate(t, side):
    """Initial margin rate: Reg T 50 %, or the higher FINRA leveraged-ETF requirement."""
    lev = U.lev_of(t)
    base = MAINT_LONG if side == "long" else MAINT_SHORT
    return max(REGT_INIT, min(1.0, base * lev))


def maint_req(t, side, shares, price):
    lev = U.lev_of(t)
    mv = shares * price
    if side == "long":
        return mv * min(1.0, MAINT_LONG * lev)
    if price >= 5:
        return max(MAINT_SHORT * lev * mv, SHORT_MIN_PER_SHARE * shares)
    return max(mv, 2.5 * shares)


def market_open(now):
    d = now.date().isoformat()
    if now.weekday() >= 5 or d in HOLIDAYS:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 10)


def trading_days(d0, d1):
    """Market days from d0 to d1 inclusive (ISO strings)."""
    out, d, end = [], date.fromisoformat(d0), date.fromisoformat(d1)
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in HOLIDAYS:
            out.append(d.isoformat())
        d = date.fromordinal(d.toordinal() + 1)
    return out


def direction(p):
    """+1 if the position gains when the market rises, -1 otherwise."""
    if p["side"] == "short":
        return -1
    return -1 if U.LEVERAGED.get(p["ticker"], (None, 1))[1] < 0 else 1


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, ledger, data, now, deposits=None, restore=None):
        self.L = ledger
        self.data = data
        self.now = now
        self.today = now.date().isoformat()
        self.P = ledger["portfolio"]
        self.C = ledger["config"]
        self.M = ledger.setdefault("meta", {})
        self.deposits = deposits or []
        self.restore = restore
        self.actions, self.notes, self.new_trades = [], [], []
        self.status = "normal"
        self.quotes = {}
        self.migrate()
        self.C["schedule"] = SCHEDULE_TEXT
        self.C.update(CONFIG_TEXT)
        self.C["maxLeverage"] = MAX_EXPO
        self.C["dipRule"] = DIP_RULE_TEXT
        self.C["spikeRule"] = {"text": SPIKE_RULE_TEXT, "testKey": SPIKE_TEST_KEY, "testPortfolio": "f5_s6",
                               "move": SPIKE_MOVE, "minPrice": SPIKE_MIN_PRICE, "minDollarVolM": SPIKE_MIN_DV,
                               "stop": SPIKE_STOP, "holdDays": SPIKE_HOLD_DAYS, "size": SPIKE_SIZE, "slots": SPIKE_SLOTS,
                               "borrow": SPIKE_BORROW}
        self.C["legalSources"] = list(LEGAL.values())
        self.C["brokerModel"] = {
            "name": "Interactive Brokers — IBKR Pro (Tiered)", "commission": "0,0035 $/hisse, en az 0,35 $, en fazla %1",
            "marginRate": MARGIN_RATE, "borrowRate": BORROW_RATE, "minMarginEquity": MIN_MARGIN_EQUITY,
            "regTInitial": REGT_INIT, "maintLong": MAINT_LONG, "maintShort": MAINT_SHORT}

    # ---------------- compliance log
    def comply(self, rule, title, text, source=None):
        log = self.L.setdefault("compliance", [])
        log.append({"at": iso(self.now), "date": self.today, "rule": rule, "title": title, "text": text,
                    "source": LEGAL.get(source) if source else None})
        del log[:-COMPLIANCE_KEPT]

    # ---------------- migrations
    def migrate(self):
        eng = self.M.get("engine")
        if eng == ENGINE:
            return
        if eng == "reg-t-bot v3":
            self._to_v4()
            eng = "trend-momentum v4"
        if eng == "trend-momentum v4":
            self._to_v5()
            eng = "trend-vol v5"
        if eng == "trend-vol v5":
            self._to_v6()
            return
        if eng != "long-short-bot v2":       # v1 ledgers: same field upgrades v2 did
            for p in self.P["positions"]:
                p.setdefault("side", "long")
                p.setdefault("lev", U.lev_of(p["ticker"]))
                if "extremeSinceEntry" not in p:
                    p["extremeSinceEntry"] = p.pop("highSinceEntry", p["lastPrice"])
                p.setdefault("tookProfit", False)
            self.P.setdefault("netDeposits", float(self.C["startCapital"]))
            self.M.setdefault("units", float(self.C["startCapital"]))
            self.M.setdefault("appliedDeposits", [])
        elif self.restore:
            self._rollback_v2()
        self.P["realizedPL"] = round(sum(num(t.get("realizedPL")) for t in self.L["trades"]
                                         if not t.get("cancelled") and t.get("action") in ("SELL", "COVER")), 2)
        self._rebuild_costs()
        self._rebuild_tax()
        self.M["lastAccrualDate"] = self.M.get("lastAccrualDate") or self.today
        self.M["regime"] = {"name": "neutral", **REGIMES["neutral"]}
        self.M["lastDailyDate"] = None          # run the compliant daily decision today
        self.M["engine"] = ENGINE
        self.comply("geçiş", "Gerçek kurallara geçiş",
                    "Bot, ABD'de 2.000 $'lık bir kredili hesapta gerçekte yapılabilecek işlemlerle sınırlandı: Reg T başlangıç "
                    "teminatı, FINRA 4210 bakım teminatı ve 2.000 $ asgari özsermaye, SEC Rule 201, IBKR Pro komisyonları ve "
                    "gerçek kredi faizi. Aracı kurum modeli: Interactive Brokers.", "regt")

    def _to_v4(self):
        """Switch from the v3 long/short engine to the v4 trend + momentum engine (positions are rebalanced today)."""
        sc = self.L.get("scorecard")
        if sc:
            self.L.setdefault("evalHistory", []).append({
                "strategy": "v3: kaldıraçlı long/short momentum", "start": sc.get("start"), "end": self.today,
                "returnPct": sc.get("returnPct"), "spyReturnPct": sc.get("spyReturnPct"), "excessPct": sc.get("excessPct"),
                "maxDrawdownPct": sc.get("maxDrawdownPct"), "trades": sc.get("trades"), "daysElapsed": sc.get("daysElapsed")})
        d0 = date.fromisoformat(self.today)
        m = d0.month + 3
        end = date(d0.year + (m - 1) // 12, (m - 1) % 12 + 1, min(d0.day, 28))
        self.M["evaluation"] = {"start": self.today, "end": end.isoformat(), "startTs": iso(self.now),
                                "strategy": "v4"}
        self.L.pop("scorecard", None)
        for p in self.P["positions"]:
            if p["side"] == "long" and p.get("lev", 1) == 1:
                p["stopMode"] = "disaster"
                p["stopPrice"] = round(p["avgCost"] * (1 - DISASTER), 2)
        self.M.update(lastRebalance=None, trendUp=None, lastDailyDate=None, engine="trend-momentum v4")
        self.M.setdefault("cooldown", {})
        self.comply("strateji", "Strateji değişti: trend filtresi + QQQ + aylık momentum sepeti",
                    "v3 (kaldıraçlı long/short, dar zarar-kes, günlük işlem) 12–15 Ekim'de SPY'ın %8,6 gerisinde kaldı. 2018–2026 "
                    "geriye dönük testte de yıllık yaklaşık %−4 getirdi, en kötü düşüşü %−61 oldu ve 8,8 yılda yaklaşık 2.500 $ "
                    "işlem maliyeti yarattı; aynı dönemde SPY yıllık %14,4 getirdi. Yerine kaldıraçsız v4 geldi (aynı testte "
                    "yıllık yaklaşık %19, en kötü düşüş %−28): SPY 200 günlük ortalamanın "
                    "üstündeyken paranın yarısı QQQ'da, yarısı aylık yenilenen 10 hisselik momentum sepetinde (sektör başına en "
                    "fazla 3). Short ve 3x ETF pozisyonları bugün kapatılıyor. Değerlendirme karnesi bugünden itibaren 3 ay için "
                    "yeniden başladı.", None)
        self.notes.append("v4 stratejisine geçildi")

    def _to_v5(self):
        """Switch from v4 (long-only, no leverage) to v5: two-directional, volatility-targeted index sleeve (QQQ, TQQQ,
        SQQQ) + the same monthly momentum basket. Index positions are resized at today's daily decision."""
        sc = self.L.get("scorecard")
        if sc and sc.get("strategy") == "v4" and sc.get("start") and sc.get("start") < self.today:
            self.L.setdefault("evalHistory", []).append({
                "strategy": "v4: kaldıraçsız QQQ + momentum sepeti", "start": sc.get("start"), "end": self.today,
                "returnPct": sc.get("returnPct"), "spyReturnPct": sc.get("spyReturnPct"), "excessPct": sc.get("excessPct"),
                "maxDrawdownPct": sc.get("maxDrawdownPct"), "trades": sc.get("trades"), "daysElapsed": sc.get("daysElapsed")})
        d0 = date.fromisoformat(self.today)
        m = d0.month + 3
        end = date(d0.year + (m - 1) // 12, (m - 1) % 12 + 1, min(d0.day, 28))
        self.M["evaluation"] = {"start": self.today, "end": end.isoformat(), "startTs": iso(self.now), "strategy": "v5"}
        self.L.pop("scorecard", None)
        for p in self.P["positions"]:
            if p["ticker"] in INDEX_SLEEVE and p["side"] == "long":
                p["stopMode"], p["stopPrice"] = "managed", None
            elif p.get("lev", 1) == 1:
                p["stopMode"] = "disaster"
                p["stopPrice"] = round(p["avgCost"] * (1 - DISASTER if p["side"] == "long" else 1 + DISASTER), 2)
        self.M.update(lastDailyDate=None, engine=ENGINE)
        self.M.setdefault("cooldown", {})
        self.comply("strateji", "Strateji değişti: iki yönlü, oynaklık hedefli kaldıraç (en fazla 3x)",
                    "v4 (kaldıraçsız, yalnızca long) 15–23 Ekim'de SPY ile başa baş gitti ama düşüşten kazanamıyordu; trend "
                    "filtresi SPY'a baktığı için Nasdaq ve çipler düşerken tam yatırımda kaldı. v5'te paranın yarısı Nasdaq "
                    "100'de S&P 500 trendinin yönünde tutulur: yükselişte QQQ ve 3x fon TQQQ, teyitli düşüşte −3x ters fon SQQQ. "
                    "Büyüklük oynaklığa göre ayarlanır (%25 ÷ 20 günlük oynaklık, en fazla 3x). Diğer yarı aynı aylık momentum "
                    "sepeti. Tüm alımlar nakitle yapılır, kredi kullanılmaz. 2018–2026 geriye dönük testte v5 yıllık %20,4 "
                    "getirdi, en kötü düşüşü %−31 oldu (v4: %18,7 ve %−26; SPY: %14,2 ve %−34). Değerlendirme karnesi bugünden "
                    "itibaren 3 ay için yeniden başladı.", "letf")
        self.notes.append("v5 stratejisine geçildi")

    def _to_v6(self):
        """Fresh 3.000 $ account for v6. The old account is summarised in `previousAccounts` (its full ledger is kept
        in archive/ in the repository); positions are not sold, the old paper account simply ends here."""
        L, P, C, M = self.L, self.P, self.C, self.M
        sc = L.get("scorecard")
        hist = list(L.get("evalHistory", []))
        if sc and sc.get("strategy") == "v5" and sc.get("start"):
            hist.append({"strategy": "v5: iki yönlü endeks + momentum sepeti", "start": sc.get("start"), "end": self.today,
                         "returnPct": sc.get("returnPct"), "spyReturnPct": sc.get("spyReturnPct"),
                         "excessPct": sc.get("excessPct"), "maxDrawdownPct": sc.get("maxDrawdownPct"),
                         "trades": sc.get("trades"), "daysElapsed": sc.get("daysElapsed")})
        costs = P.get("costs", {})
        prev = {"name": f"Hesap {len(L.get('previousAccounts', [])) + 1}", "start": C.get("startDate"), "end": self.today,
                "invested": P.get("netDeposits"), "endValue": P.get("totalValue"), "twrPct": P.get("twrPct"),
                "spyTwrPct": P.get("benchmarkTwrPct"), "realizedPL": P.get("realizedPL"),
                "trades": sum(1 for t in L.get("trades", []) if t.get("action") in ("BUY", "SELL", "SHORT", "COVER")
                              and not t.get("cancelled")),
                "costs": round(sum(num(v) for k, v in costs.items() if k != "dividendsReceived"), 2),
                "positionsAtEnd": [{"ticker": p["ticker"], "side": p["side"], "value": p.get("marketValue"),
                                    "unrealizedPct": p.get("unrealizedPct")} for p in P.get("positions", [])],
                "archive": f"archive/ledger_{self.today}.json"}
        d0 = date.fromisoformat(self.today)
        m = d0.month + 3
        end = date(d0.year + (m - 1) // 12, (m - 1) % 12 + 1, min(d0.day, 28))
        newC = {k: v for k, v in C.items() if k not in ("deposits", "benchmark")}
        newC.update(startCapital=RESET_CAPITAL, startDate=self.today,
                    benchmark={"ticker": "SPY", "startPrice": None, "startAt": None, "shares": 0.0})
        newM = {"engine": ENGINE, "units": RESET_CAPITAL, "appliedDeposits": list(M.get("appliedDeposits", [])),
                "lastAccrualDate": self.today, "cooldown": {}, "nasdaq": M.get("nasdaq") or {}, "lastDailyDate": None,
                "lastRebalance": None, "trendUp": None,
                "evaluation": {"start": self.today, "end": end.isoformat(), "startTs": iso(self.now), "strategy": "v6"}}
        newP = {"cash": RESET_CAPITAL, "positions": [], "realizedPL": 0.0, "netDeposits": RESET_CAPITAL,
                "costs": {"slippage": 0.0, "commission": 0.0, "fees": 0.0, "interest": 0.0, "borrow": 0.0,
                          "dividendsPaid": 0.0, "dividendsReceived": 0.0}}
        prev_accounts = list(L.get("previousAccounts", [])) + [prev]
        L.clear()
        L.update({"config": newC, "portfolio": newP, "meta": newM, "trades": [], "snapshots": [], "checks": {},
                  "runs": [], "compliance": [], "tax": {}, "evalHistory": hist, "previousAccounts": prev_accounts})
        self.P, self.C, self.M = L["portfolio"], L["config"], L["meta"]
        self.comply("yeni hesap", "Hesap 3.000 $ ile sıfırdan başladı (v6)",
                    f"Önceki hesap {prev['start']} – {prev['end']}: {usd(prev['invested'] or 0)} yatırıldı, son değer "
                    f"{usd(prev['endValue'] or 0)} (zaman ağırlıklı %{trn(prev['twrPct'] or 0)}; SPY %{trn(prev['spyTwrPct'] or 0)}). "
                    "Yeni hesapta paranın yarısı iki yönlü endeks kolunda, yarısı haber şoku kolunda: açılışta %8 veya daha "
                    "fazla boşlukla açılan hisse ilk saatte haberin tersine dönerse o yöne girilir. Yalnızca long çalışan "
                    "momentum sepeti kapatıldı.", None)
        self.notes.append("v6: yeni hesap")

    def _rollback_v2(self):
        """Cancel the v2 trades of 12 Oct 2026 13:01 (a real broker would have rejected them for exceeding
        Reg T buying power) and restore the positions held just before them."""
        r = self.restore
        cancelled, removed_pl = [], 0.0
        last_px = {p["ticker"]: p["lastPrice"] for p in self.P["positions"]}
        v2_init = sum(init_rate(p["ticker"], p.get("side", "long")) * p["shares"] * p["lastPrice"] for p in self.P["positions"])
        v2_eq = self.P.get("totalValue", 0)
        for t in self.L["trades"]:
            if t.get("ts", "") >= r["v2Start"] and t.get("action") in ("BUY", "SELL", "SHORT", "COVER"):
                t["cancelled"] = True
                t["cancelReason"] = ("İptal edildi: bu emir gerçek bir aracı kurumda Reg T alım gücünü aştığı için "
                                     "reddedilirdi (12 Ekim gerçek kurallara geçiş).")
                cancelled.append(t)
                px = last_px.get(t["ticker"], t["quotePrice"])
                if t["action"] == "BUY":
                    removed_pl += t["shares"] * (px - t["execPrice"])
                elif t["action"] == "SHORT":
                    removed_pl += t["shares"] * (t["execPrice"] - px) - num(t.get("fees"))
        for run in self.L.get("runs", []):
            if run.get("at", "") >= r["v2Start"]:
                run["cancelled"] = True
                run["headline"] = "İPTAL EDİLDİ (gerçek kurallara aykırıydı) · " + run.get("headline", "")
        self.P["positions"] = json.loads(json.dumps(r["positions"]))
        self._backfill_commissions()
        dep = sum(t["value"] for t in self.L["trades"] if t.get("action") == "DEPOSIT")
        cash = float(self.C["startCapital"]) + dep
        for t in self.L["trades"]:
            if t.get("cancelled"):
                continue
            a = t.get("action")
            if a in ("BUY", "COVER"):
                cash -= t["value"] + num(t.get("commission"))
            elif a in ("SELL", "SHORT"):
                cash += t["value"] - num(t.get("fees")) - num(t.get("commission"))
        old = self.P.get("costs", {})
        cash -= num(old.get("interest")) + num(old.get("borrow")) + num(old.get("dividendsPaid"))
        cash += num(old.get("dividendsReceived"))
        self.P["cash"] = round(cash, 2)
        self.comply("Reg T", "Kurallara aykırı işlemler iptal edildi",
                    f"12 Ekim 13:01'deki {len(cancelled)} işlem (3x'e yakın kaldıraçla long/short) gerçek bir hesapta yapılamazdı: "
                    "Reg T'ye göre long hisselerde değerin %50'si, short'ta %150'si (satış geliri + %50), 3x ETF'lerde %75'i kadar "
                    f"teminat gerekir. {usd(v2_eq)} özsermayeye karşılık bu pozisyonların başlangıç teminatı {usd(v2_init)} "
                    "ediyordu. Aracı kurum bu emirleri "
                    f"reddederdi. İşlemler iptal edildi, pozisyonlar 13:01 öncesine döndürüldü; iptal edilen işlemlerin o "
                    f"zamana kadarki kâr/zararı ({usd(removed_pl)}) kayıttan çıkarıldı.", "regt")
        self.notes.append(f"{len(cancelled)} kurala aykırı işlem iptal edildi")

    def _backfill_commissions(self):
        """The first trades (8-9 Oct) were booked without a broker commission. A real IBKR account would have
        charged one, so it is added retroactively (and the SEC/FINRA fees on sales)."""
        total, n = 0.0, 0
        for t in self.L["trades"]:
            if t.get("cancelled") or t.get("action") not in ("BUY", "SELL", "SHORT", "COVER") or "commission" in t:
                continue
            comm = commission(t["shares"], t["value"])
            new_fees = 0.0
            if t["action"] in ("SELL", "SHORT") and not num(t.get("fees")):
                new_fees = sale_fees(t["value"], t["shares"])
                t["fees"] = new_fees
            t["commission"] = comm
            t["commissionBackfilled"] = True
            if t["action"] in ("SELL", "COVER"):
                t["realizedPL"] = round(num(t.get("realizedPL")) - comm - new_fees, 2)
            total += comm + new_fees
            n += 1
        if n:
            self.comply("aracı kurum ücreti", "İlk işlemlere komisyon eklendi",
                        f"8–9 Ekim'deki {n} işlem komisyonsuz kaydedilmişti. Gerçek bir IBKR hesabında her emirde komisyon "
                        f"(en az 0,35 $) ve satışlarda SEC/FINRA ücreti alınırdı; toplam {usd(total)} geriye dönük düşüldü.",
                        "ibkr_comm")

    def _rebuild_tax(self):
        """Tax view per year from the live (non-cancelled) trades."""
        tax = {}
        for t in self.L["trades"]:
            if t.get("cancelled") or t.get("action") not in ("BUY", "SELL", "SHORT", "COVER"):
                continue
            y = tax.setdefault(t["date"][:4], {})
            y["commissions"] = round(y.get("commissions", 0) + num(t.get("commission")) + num(t.get("fees")), 2)
            if t["action"] in ("SELL", "COVER"):
                y["shortTermRealized"] = round(y.get("shortTermRealized", 0) + num(t.get("realizedPL")), 2)
        old = self.L.get("tax", {})
        for yr, d in old.items():
            for k in ("marginInterest", "dividendsReceived", "dividendsPaidShort", "washDisallowed"):
                if d.get(k):
                    tax.setdefault(yr, {})[k] = d[k]
        self.L["tax"] = tax

    def _rebuild_costs(self):
        c = {"slippage": 0.0, "commission": 0.0, "fees": 0.0, "interest": 0.0, "borrow": 0.0, "dividendsPaid": 0.0,
             "dividendsReceived": 0.0}
        old = self.P.get("costs", {})
        for k in ("interest", "borrow", "dividendsPaid", "dividendsReceived"):
            c[k] = old.get(k, 0.0)
        for t in self.L["trades"]:
            if t.get("cancelled") or t.get("action") not in ("BUY", "SELL", "SHORT", "COVER"):
                continue
            c["slippage"] += abs(t.get("execPrice", 0) - t.get("quotePrice", 0)) * t.get("shares", 0)
            c["commission"] += num(t.get("commission"))
            c["fees"] += num(t.get("fees"))
        self.P["costs"] = {k: round(v, 4) for k, v in c.items()}

    # ---------------- bookkeeping
    def _trade_id(self):
        prefix = "t-" + self.today.replace("-", "") + "-"
        used = [int(t["id"][-2:]) for t in self.L["trades"] if t.get("id", "").startswith(prefix)]
        return prefix + f"{(max(used) if used else 0) + 1:02d}"

    def _pos(self, t, side=None):
        for p in self.P["positions"]:
            if p["ticker"] == t and (side is None or p["side"] == side):
                return p
        return None

    def _cost(self, key, amount):
        c = self.P.setdefault("costs", {})
        c[key] = round(c.get(key, 0.0) + amount, 4)

    def _tax(self, key, amount):
        y = self.L.setdefault("tax", {}).setdefault(self.today[:4], {})
        y[key] = round(y.get(key, 0.0) + amount, 2)

    def _log(self, action, t, side, shares, quote, quote_at, ex, value, comm, fees, reason, rule, realized=None, extra=None):
        tr = {
            "id": self._trade_id(), "ts": iso(self.now), "date": self.today, "action": action, "side": side,
            "ticker": t, "name": U.name_of(t), "kind": U.kind_of(t), "shares": shares,
            "quotePrice": round(quote, 4), "quoteAt": iso(quote_at), "slippagePct": SLIP * 100, "execPrice": ex,
            "value": value, "commission": comm, "fees": fees,
            "priceSource": {"title": f"Yahoo Finance 5 dk mum ({hm(quote_at)} ET)", "url": U.quote_url(t)},
            "reason": reason, "rule": rule, "sources": [src(t), YF_SOURCE], "runId": self.today, "by": "bot",
        }
        if realized is not None:
            tr["realizedPL"] = realized
        if extra:
            tr.update(extra)
        self.L["trades"].append(tr)
        self.new_trades.append(tr)
        return tr

    def _new_position(self, t, side, shares, ex, value, quote, quote_at, reason):
        lev = U.lev_of(t)
        managed = t in INDEX_SLEEVE
        stop = None if managed else round(ex * (1 - DISASTER if side == "long" else 1 + DISASTER), 2)
        return {"stopMode": "managed" if managed else "disaster",
            "ticker": t, "name": U.name_of(t), "kind": U.kind_of(t), "side": side, "lev": lev,
            "shares": shares, "avgCost": ex, "costBasis": value, "lastPrice": quote, "lastPriceAt": iso(quote_at),
            "priceSource": {"title": "Yahoo Finance", "url": U.quote_url(t)},
            "marketValue": round(shares * quote * (1 if side == "long" else -1), 2), "exposure": round(shares * quote * lev, 2),
            "unrealized": 0.0, "unrealizedPct": 0.0, "openedAt": self.today, "openedTs": iso(quote_at),
            "lastAddDate": self.today, "stopPrice": stop, "extremeSinceEntry": quote, "tookProfit": False, "thesis": reason,
        }

    # ---------------- Reg T checks
    def _init_req(self):
        return sum(init_rate(p["ticker"], p["side"]) * p["shares"] * p["lastPrice"] for p in self.P["positions"])

    def _allowed_mv(self, t, side, want_mv):
        """Largest new market value the broker would accept for this order (Reg T + 2.000 $ minimum)."""
        self.revalue()
        eq = self.P["totalValue"]
        if side == "short" and eq < MIN_MARGIN_EQUITY:
            return 0.0, "min2000"
        rooms = {"regt": (eq * 0.99 - self._init_req()) / init_rate(t, side),
                 "econ": (GROSS_ECON_CAP * eq - self.P["grossExposure"]) / U.lev_of(t)}
        if side == "long" and eq < MIN_MARGIN_EQUITY:
            rooms["cash"] = max(0.0, self.P["cash"]) / (1 + SLIP) - COMM_MIN
        why = min(rooms, key=rooms.get)
        room = max(0.0, rooms[why])
        if room >= want_mv:
            return want_mv, None
        return room, why

    def _wash_check_open(self, t, side, value):
        """Opening trade: a loss-closing trade in the same ticker/side within 30 days -> wash sale."""
        d0 = date.fromisoformat(self.today)
        for tr in reversed(self.L["trades"]):
            if tr.get("cancelled") or tr.get("ticker") != t or tr.get("side", "long") != side:
                continue
            if tr.get("action") not in ("SELL", "COVER") or num(tr.get("realizedPL")) >= 0 or tr.get("washMatched"):
                continue
            if (d0 - date.fromisoformat(tr["date"])).days <= WASH_DAYS:
                loss = -tr["realizedPL"]
                tr["washMatched"] = True
                tr["washSale"] = {"disallowed": round(loss, 2), "replacementDate": self.today}
                self._tax("washDisallowed", loss)
                p = self._pos(t, side)
                if p:
                    p["taxBasisAdj"] = round(p.get("taxBasisAdj", 0) + loss, 2)
                self.comply("wash sale", f"{t}: wash sale", f"{tr['date']} tarihinde {t} {usd(loss)} zararla kapatılmıştı; "
                            "30 gün içinde yeniden açıldığı için bu zarar bu yıl vergiden düşülemez, yeni pozisyonun "
                            "maliyetine eklenir (IRS wash sale kuralı).", "irs550")
                return loss
        return 0.0

    # ---------------- order execution
    def open_long(self, t, dollars, quote, quote_at, reason, rule):
        ex = round(quote * (1 + SLIP), 4)
        shares = floor4(dollars / ex)
        value = round(shares * ex, 2)
        if shares <= 0 or value < MIN_TRADE:
            return None
        comm = commission(shares, value)
        self.P["cash"] = round(self.P["cash"] - value - comm, 2)
        self._cost("slippage", shares * (ex - quote))
        self._cost("commission", comm)
        self._tax("commissions", comm)
        p = self._pos(t, "long")
        if p:
            tot = round(p["shares"] + shares, 4)
            p["avgCost"] = round((p["avgCost"] * p["shares"] + ex * shares) / tot, 4)
            p["costBasis"] = round(p["costBasis"] + value, 2)
            p["shares"] = tot
            p["lastAddDate"] = self.today
        else:
            self.P["positions"].append(self._new_position(t, "long", shares, ex, value, quote, quote_at, reason))
        tr = self._log("BUY", t, "long", shares, quote, quote_at, ex, value, comm, 0.0, reason, rule)
        self._wash_check_open(t, "long", value)
        self.actions.append(f"{t} long ({usd(value)})")
        return tr

    def open_short(self, t, dollars, quote, quote_at, reason, rule, slip=SLIP, locate=None):
        ex = round(quote * (1 - slip), 4)
        shares = floor4(dollars / ex)
        value = round(shares * ex, 2)
        if shares <= 0 or value < MIN_TRADE:
            return None
        comm = commission(shares, value)
        fees = sale_fees(value, shares)
        self.P["cash"] = round(self.P["cash"] + value - fees - comm, 2)
        self._cost("slippage", shares * (quote - ex))
        self._cost("commission", comm)
        self._cost("fees", fees)
        self._tax("commissions", comm + fees)
        p = self._pos(t, "short")
        if p:
            tot = round(p["shares"] + shares, 4)
            p["avgCost"] = round((p["avgCost"] * p["shares"] + ex * shares) / tot, 4)
            p["costBasis"] = round(p["costBasis"] + value, 2)
            p["shares"] = tot
            p["lastAddDate"] = self.today
        else:
            self.P["positions"].append(self._new_position(t, "short", shares, ex, value, quote, quote_at, reason))
        tr = self._log("SHORT", t, "short", shares, quote, quote_at, ex, value, comm, fees, reason, rule,
                       extra={"locate": locate or "Kolay ödünç (ETB) varsayımı: büyük, likit hisse"})
        self._wash_check_open(t, "short", value)
        self.actions.append(f"{t} short ({usd(value)})")
        return tr

    def _close_common(self, p, shares, full, realized, action, value, comm, fees, quote, quote_at, ex, reason, rule):
        held = (date.fromisoformat(self.today) - date.fromisoformat(p.get("openedAt", self.today))).days
        term = "kısa vade" if held <= 365 or p["side"] == "short" else "uzun vade"
        extra = {"holdingDays": held, "taxTerm": term}
        self._tax("shortTermRealized" if term == "kısa vade" else "longTermRealized", realized)
        if realized < 0 and not full and p.get("lastAddDate") and \
                (date.fromisoformat(self.today) - date.fromisoformat(p["lastAddDate"])).days <= WASH_DAYS:
            extra["washSale"] = {"disallowed": round(-realized, 2), "replacementDate": p["lastAddDate"]}
            extra["washMatched"] = True
            self._tax("washDisallowed", -realized)
            p["taxBasisAdj"] = round(p.get("taxBasisAdj", 0) - realized, 2)
            self.comply("wash sale", f"{p['ticker']}: wash sale", f"Pozisyonun bir kısmı {usd(-realized)} zararla kapatıldı; "
                        f"kalan hisseler {p['lastAddDate']} tarihinde (30 gün içinde) alındığı için bu zarar bu yıl düşülemez.",
                        "irs550")
        return self._log(action, p["ticker"], p["side"], shares, quote, quote_at, ex, value, comm, fees, reason, rule,
                         realized, extra)

    def close_long(self, p, shares, quote, quote_at, reason, rule):
        shares = min(floor4(shares) if shares < p["shares"] else p["shares"], p["shares"])
        if shares <= 0:
            return None
        full = p["shares"] - shares < 0.0001
        if full:
            shares = p["shares"]
        ex = round(quote * (1 - SLIP), 4)
        value = round(shares * ex, 2)
        comm = commission(shares, value)
        fees = sale_fees(value, shares)
        realized = round(shares * (ex - p["avgCost"]) - fees - comm, 2)
        self.P["cash"] = round(self.P["cash"] + value - fees - comm, 2)
        self.P["realizedPL"] = round(self.P.get("realizedPL", 0) + realized, 2)
        self._cost("slippage", shares * (quote - ex))
        self._cost("commission", comm)
        self._cost("fees", fees)
        self._tax("commissions", comm + fees)
        tr = self._close_common(p, shares, full, realized, "SELL", value, comm, fees, quote, quote_at, ex, reason, rule)
        self._shrink(p, shares, full)
        self.actions.append(f"{p['ticker']} long {'kapatıldı' if full else 'azaltıldı'} ({usd(value)})")
        return tr

    def cover_short(self, p, shares, quote, quote_at, reason, rule):
        shares = min(floor4(shares) if shares < p["shares"] else p["shares"], p["shares"])
        if shares <= 0:
            return None
        full = p["shares"] - shares < 0.0001
        if full:
            shares = p["shares"]
        ex = round(quote * (1 + p.get("slip", SLIP)), 4)
        value = round(shares * ex, 2)
        comm = commission(shares, value)
        realized = round(shares * (p["avgCost"] - ex) - comm, 2)
        self.P["cash"] = round(self.P["cash"] - value - comm, 2)
        self.P["realizedPL"] = round(self.P.get("realizedPL", 0) + realized, 2)
        self._cost("slippage", shares * (ex - quote))
        self._cost("commission", comm)
        self._tax("commissions", comm)
        tr = self._close_common(p, shares, full, realized, "COVER", value, comm, 0.0, quote, quote_at, ex, reason, rule)
        self._shrink(p, shares, full)
        self.actions.append(f"{p['ticker']} short {'kapatıldı' if full else 'azaltıldı'} ({usd(value)})")
        return tr

    def close(self, p, shares, quote, quote_at, reason, rule):
        fn = self.close_long if p["side"] == "long" else self.cover_short
        return fn(p, shares, quote, quote_at, reason, rule)

    def _shrink(self, p, shares, full):
        if full:
            self.P["positions"].remove(p)
        else:
            remaining = round(p["shares"] - shares, 4)
            p["costBasis"] = round(p["costBasis"] * remaining / p["shares"], 2)
            p["shares"] = remaining

    # ---------------- valuation
    def revalue(self):
        long_v = short_v = gross = net = req = init = 0.0
        for p in self.P["positions"]:
            q = self.quotes.get(p["ticker"])
            if q:
                p["lastPrice"] = round(q["price"], 4)
                p["lastPriceAt"] = iso(q["at"])
                p["priceSource"] = {"title": f"Yahoo Finance 5 dk mum ({hm(q['at'])} ET)", "url": U.quote_url(p["ticker"])}
            val = p["shares"] * p["lastPrice"]
            lev = p.get("lev", 1)
            if p["side"] == "long":
                p["marketValue"] = round(val, 2)
                p["unrealized"] = round(val - p["costBasis"], 2)
                long_v += val
            else:
                p["marketValue"] = round(-val, 2)
                p["unrealized"] = round(p["costBasis"] - val, 2)
                short_v += val
            p["exposure"] = round(val * lev, 2)
            p["unrealizedPct"] = round(p["unrealized"] / p["costBasis"] * 100, 2) if p["costBasis"] else 0.0
            p["maintReq"] = round(maint_req(p["ticker"], p["side"], p["shares"], p["lastPrice"]), 2)
            gross += val * lev
            net += val * lev * direction(p)
            req += p["maintReq"]
            init += init_rate(p["ticker"], p["side"]) * val
        equity = round(self.P["cash"] + long_v - short_v, 2)
        units = self.M.get("units") or self.C["startCapital"]
        dep = self.P.get("netDeposits", self.C["startCapital"])
        excess = equity - init
        self.P.update(
            longValue=round(long_v, 2), shortValue=round(short_v, 2), positionsValue=round(long_v - short_v, 2),
            totalValue=equity, grossExposure=round(gross, 2), netExposure=round(net, 2),
            leverage=round(gross / equity, 3) if equity > 0 else None, maintenanceReq=round(req, 2),
            marginBuffer=round(equity - req, 2), regTInitialReq=round(init, 2), regTExcess=round(excess, 2),
            buyingPower=round(max(0.0, excess) / REGT_INIT, 2) if equity >= MIN_MARGIN_EQUITY else round(max(0.0, self.P["cash"]), 2),
            marginEligible=equity >= MIN_MARGIN_EQUITY,
            totalPL=round(equity - dep, 2), totalPLPct=round((equity - dep) / dep * 100, 2),
            twrPct=round((equity / units - 1) * 100, 2), asOf=iso(self.now))
        spy = self.quotes.get("SPY")
        if spy and not self.C["benchmark"].get("startPrice"):
            self.C["benchmark"].update(startPrice=round(spy["price"], 2), startAt=iso(spy["at"]),
                                       shares=round(float(self.C["startCapital"]) / spy["price"], 6))
        if spy:
            self.P["benchmarkPrice"] = round(spy["price"], 2)
            self.P["benchmarkValue"] = round(self.C["benchmark"]["shares"] * spy["price"], 2)
            self.P["benchmarkTwrPct"] = round((spy["price"] / self.C["benchmark"]["startPrice"] - 1) * 100, 2)
        # positions' market values are rounded to the cent one by one, so allow half a cent per position
        assert abs(self.P["cash"] + sum(p["marketValue"] for p in self.P["positions"]) - equity) <= 0.011 + 0.005 * len(self.P["positions"])

    # ---------------- quotes
    def load_quotes(self, tickers):
        tickers = sorted(set(tickers) | {"SPY"})
        bars = self.data.intraday(tickers)
        hist = self.data.daily(tickers, period="6mo")
        today = self.now.date()
        for t in tickers:
            b, h = bars.get(t), hist.get(t)
            if b is None or b.empty or h is None or h.empty:
                self.notes.append(f"{t}: güncel veri alınamadı")
                continue
            b = b[b.index.date == today]
            if b.empty:
                self.notes.append(f"{t}: bugüne ait veri yok")
                continue
            past = h[h.index.date < today]
            if past.empty:
                continue
            prev = float(past["Close"].iloc[-1])
            last_ts = b.index[-1]
            price = float(b["Close"].iloc[-1])
            closes = list(past["Close"].iloc[-49:]) + [price]
            age = (pd.Timestamp(self.now) - last_ts).total_seconds() / 60
            day_low = float(b["Low"].min())
            ssr_y = len(past) >= 2 and float(past["Low"].iloc[-1]) <= float(past["Close"].iloc[-2]) * (1 - SSR_DROP)
            self.quotes[t] = {"price": price, "at": last_ts, "prevClose": prev, "bars": b,
                              "dayChangePct": (price / prev - 1) * 100, "sma50": sum(closes) / len(closes),
                              "stale": age > STALE_MIN,
                              "ssr": day_low <= prev * (1 - SSR_DROP) or ssr_y}

    def quotes_for(self, tickers):
        missing = [t for t in tickers if t not in self.quotes]
        if missing:
            self.load_quotes(missing)

    # ---------------- daily accruals, deposits and dividends
    def accrue(self):
        last = self.M.get("lastAccrualDate") or self.today
        days = (date.fromisoformat(self.today) - date.fromisoformat(last)).days
        if days <= 0:
            return
        debit = max(0.0, -self.P["cash"])
        interest = round(debit * MARGIN_RATE * days / 360, 2)
        borrow = round(sum(p["shares"] * p["lastPrice"] * p.get("borrowRate", BORROW_RATE) for p in self.P["positions"]
                           if p["side"] == "short") * days / 360, 2)
        if interest or borrow:
            self.P["cash"] = round(self.P["cash"] - interest - borrow, 2)
            self._cost("interest", interest)
            self._cost("borrow", borrow)
            self._tax("marginInterest", interest)
            self.notes.append(f"{days} günlük kredi faizi {usd(interest)}, ödünç ücreti {usd(borrow)} düşüldü")
        self.M["lastAccrualDate"] = self.today

    def apply_deposits(self):
        applied = set(self.M.setdefault("appliedDeposits", []))
        spy = self.quotes.get("SPY")
        for d in self.deposits:
            if d["id"] in applied or d.get("date", "") > self.today or not spy:
                continue
            amount = float(d["amount"])
            self.revalue()
            nav = self.P["totalValue"] / self.M["units"]
            self.M["units"] = round(self.M["units"] + amount / nav, 6)
            self.P["cash"] = round(self.P["cash"] + amount, 2)
            self.P["netDeposits"] = round(self.P.get("netDeposits", self.C["startCapital"]) + amount, 2)
            self.C["benchmark"]["shares"] = round(self.C["benchmark"]["shares"] + amount / spy["price"], 6)
            self.C.setdefault("deposits", []).append({"id": d["id"], "date": self.today, "amount": amount,
                                                      "note": d.get("note", "")})
            self.L["trades"].append({
                "id": self._trade_id(), "ts": iso(self.now), "date": self.today, "action": "DEPOSIT",
                "ticker": "NAKİT", "name": "Sermaye ekleme", "shares": 0, "value": amount, "fees": 0.0,
                "reason": (d.get("note") or "Sermaye eklendi") + f". Kıyas için aynı tutarda SPY ({trn(spy['price'])} $) "
                          "de kıyas portföyüne eklendi; getiri yüzdesi zaman ağırlıklı hesaplanır.",
                "rule": "para yatırma", "sources": [], "runId": self.today, "by": "bot"})
            self.new_trades.append(self.L["trades"][-1])
            self.M["appliedDeposits"].append(d["id"])
            self.actions.append(f"{usd(amount)} sermaye eklendi")
            self.revalue()
            eq = self.P["totalValue"]
            self.comply("para yatırma", f"{usd(amount)} sermaye eklendi",
                        (d.get("note") or "Sermaye eklendi") + f". Yeni özsermaye {usd(eq)}; FINRA 4210'un 2.000 $ asgarisinin "
                        + (f"{usd(eq - MIN_MARGIN_EQUITY)} üstünde." if eq >= MIN_MARGIN_EQUITY else
                           f"hâlâ {usd(MIN_MARGIN_EQUITY - eq)} altında."), "min2000")

    def dividends(self):
        """Book ex-dividend amounts: received on longs, paid on shorts (payment in lieu)."""
        if not hasattr(self.data, "dividends") or not self.P["positions"]:
            return
        divs = self.data.dividends([p["ticker"] for p in self.P["positions"]])
        today = self.now.date()
        for p in self.P["positions"]:
            s = divs.get(p["ticker"])
            if s is None or s.empty:
                continue
            todays = s[s.index.date == today]
            if todays.empty:
                continue
            per_share = float(todays.iloc[-1])
            amt = round(per_share * p["shares"], 2)
            if amt <= 0:
                continue
            if p["side"] == "long":
                self.P["cash"] = round(self.P["cash"] + amt, 2)
                self._cost("dividendsReceived", amt)
                self._tax("dividendsReceived", amt)
                text = (f"{p['ticker']} bugün temettü dağıttı (hisse başı {trn(per_share, 4)} $); {usd(amt)} hesaba geçti. "
                        "Gerçekte nakit ödeme tarihinde gelir; 1099-DIV ile raporlanır.")
            else:
                self.P["cash"] = round(self.P["cash"] - amt, 2)
                self._cost("dividendsPaid", amt)
                self._tax("dividendsPaidShort", amt)
                text = (f"{p['ticker']} bugün temettü dağıttı; short pozisyon olduğu için hisse başı {trn(per_share, 4)} $, "
                        f"toplam {usd(amt)} hisseyi ödünç verene ödendi (payment in lieu).")
            self.comply("temettü", f"{p['ticker']}: temettü", text, "irs550")
            self.notes.append(text)

    # ---------------- intraday risk guard
    def guard(self):
        for p in list(self.P["positions"]):
            q = self.quotes.get(p["ticker"])
            if not q or q["stale"]:
                self.notes.append(f"{p['ticker']}: veri gecikmeli, işlem yapılmadı")
                continue
            if p.get("stopMode") == "managed":
                continue                          # index sleeve: sized daily by volatility, no price stop
            b = q["bars"]
            if p.get("openedTs"):
                b = b[b.index > pd.Timestamp(p["openedTs"])]
            if p.get("lastBarTs"):
                b = b[b.index >= pd.Timestamp(p["lastBarTs"])]
            ext, stop, hit = p["extremeSinceEntry"], p["stopPrice"], None
            fixed = p.get("stopMode") in ("disaster", "event")
            event = p.get("stopMode") == "event"
            if p["side"] == "long":
                trail = 0.0 if fixed else STOP_LONG[p.get("lev", 1)][1]
                for ts, row in b.iterrows():
                    eff = round(max(stop, ext * trail), 2)
                    if row["Low"] <= eff:
                        hit = (ts, eff, float(row["Low"]), float(row["Open"]) if row["Open"] < eff else eff)
                        break
                    ext = max(ext, float(row["High"]))
                    stop = round(max(stop, ext * trail), 2)
                stop = round(max(stop, ext * trail), 2)
            else:
                trail = float("inf") if fixed else STOP_SHORT[1]
                for ts, row in b.iterrows():
                    eff = round(min(stop, ext * trail), 2)
                    if row["High"] >= eff:
                        hit = (ts, eff, float(row["High"]), float(row["Open"]) if row["Open"] > eff else eff)
                        break
                    ext = min(ext, float(row["Low"]))
                    stop = round(min(stop, ext * trail), 2)
                stop = round(min(stop, ext * trail), 2)
            if hit:
                ts, eff, extreme, fill = hit
                word = "indi" if p["side"] == "long" else "çıktı"
                head = ((f"Sert yükseliş short'unun zarar-kesi (giriş fiyatının %{trn(SPIKE_STOP * 100, 0)} üstü) "
                         "tetiklendi: ") if p.get("spike") else
                        (f"Haber şoku zarar-kesi (ilk saatin {'en düşük' if p['side'] == 'long' else 'en yüksek'} "
                         "seviyesinin ötesi) tetiklendi: ") if event else
                        (f"Felaket zarar-kesi (maliyetin %25 {'altı' if p['side'] == 'long' else 'üstü'}) tetiklendi: "
                         if fixed else "Bekleyen zarar-kes emri tetiklendi: "))
                reason = (head
                          + f"{hm(ts)} ET mumunda fiyat {trn(extreme)} $'a {word}, zarar-kes seviyesi {trn(eff)} $. "
                          + (f"Mum seviyenin ötesinde açıldığı için açılış fiyatından ({trn(fill)} $) kapatıldı."
                             if fill != eff else f"Emir {trn(fill)} $'dan gerçekleşti."))
                self.close(p, p["shares"], fill, ts,
                           reason + (f" {COOLDOWN_DAYS} gün yeniden alınmayacak." if fixed and not event else ""),
                           "sert yükseliş zarar-kesi" if p.get("spike") else "haber şoku zarar-kesi" if event else
                           "felaket zarar-kesi" if fixed else "zarar-kes")
                if fixed and not event:
                    self.M.setdefault("cooldown", {})[p["ticker"]] = self.today
                self.status = "alarm"
                continue
            p["extremeSinceEntry"] = round(ext, 4)
            p["stopPrice"] = stop
            if len(b):
                p["lastBarTs"] = iso(b.index[-1])
            if fixed:
                continue
            price, avg = q["price"], p["avgCost"]
            gain = (price / avg - 1) if p["side"] == "long" else (1 - price / avg)
            if not p.get("tookProfit") and gain >= TAKE_PROFIT:
                p["tookProfit"] = True
                self.close(p, floor4(p["shares"] / 2), price, q["at"],
                           f"Kâr alma: pozisyon lehimize %{trn(gain * 100, 1)} hareket etti (maliyet {trn(avg)} $, fiyat "
                           f"{trn(price)} $); yarısı kapatıldı, kalanı izleyen zarar-kes ile taşınıyor.", "kâr alma")
                if self.status == "normal":
                    self.status = "uyari"
                continue
            if p.get("lev", 1) == 1:
                chg = q["dayChangePct"]
                if p["side"] == "long" and chg <= -SHOCK_PCT and price < q["sma50"]:
                    self.close(p, p["shares"], price, q["at"],
                               f"Tek hisse şoku: gün içinde {trpct(chg)} düştü ve 50 günlük ortalamanın ({trn(q['sma50'])} $) "
                               "altına indi; zarar-kes beklenmeden satıldı.", "tek hisse şoku")
                    self.status = "alarm"
                elif p["side"] == "short" and chg >= SHOCK_PCT and price > q["sma50"]:
                    self.close(p, p["shares"], price, q["at"],
                               f"Tek hisse şoku: gün içinde {trpct(chg)} yükseldi ve 50 günlük ortalamanın "
                               f"({trn(q['sma50'])} $) üstüne çıktı; short zarar-kes beklenmeden kapatıldı.", "tek hisse şoku")
                    self.status = "alarm"
        # v5: no intraday crash/squeeze brakes (they were not part of the tested rules); the index sleeve's size is
        # set by volatility each day and the maintenance-margin check still runs on every run.

    def _reduce(self, sign, cap, reason, rule, key):
        """Close positions of one direction (worst first) until that direction's exposure <= cap."""
        self.revalue()
        exp = sum(p["exposure"] for p in self.P["positions"] if direction(p) == sign)
        need = exp - cap
        for p in sorted([p for p in self.P["positions"] if direction(p) == sign], key=key):
            if need <= 0.5:
                break
            q = self.quotes.get(p["ticker"])
            if not q or q["stale"]:
                continue
            per_share = q["price"] * p.get("lev", 1)
            shares = min(p["shares"], math.ceil(need / per_share * 10000) / 10000)
            if self.close(p, shares, q["price"], q["at"], reason(p, q), rule):
                need -= shares * per_share

    def brakes(self):
        spy = self.quotes.get("SPY")
        if not spy or spy["stale"]:
            return
        chg = spy["dayChangePct"]
        self.revalue()
        eq = self.P["totalValue"]
        if chg <= BRAKE1:
            cap_x = LONG_CAP_BRAKE2 if chg <= BRAKE2 else LONG_CAP_BRAKE1
            self.M["brakeDate"] = self.today
            self.status = "alarm"
            self._reduce(1, cap_x * eq,
                         lambda p, q: (f"Çöküş freni: SPY önceki kapanışa göre {trpct(chg)} düştü; long maruziyet net değerin "
                                       f"en fazla {trn(cap_x, 1)} katına indiriliyor ({p['ticker']} {trpct(q['dayChangePct'])})."),
                         "çöküş freni", key=lambda p: self.quotes.get(p["ticker"], {}).get("dayChangePct", 0) * p.get("lev", 1))
        elif chg >= SQUEEZE:
            self.M["squeezeDate"] = self.today
            self.status = "alarm"
            self._reduce(-1, SHORT_CAP_SQUEEZE * eq,
                         lambda p, q: (f"Sıkışma freni: SPY önceki kapanışa göre {trpct(chg)} yükseldi; short maruziyet net "
                                       f"değerin en fazla {trn(SHORT_CAP_SQUEEZE, 1)} katına indiriliyor "
                                       f"({p['ticker']} {trpct(q['dayChangePct'])})."),
                         "sıkışma freni", key=lambda p: -self.quotes.get(p["ticker"], {}).get("dayChangePct", 0))
        elif abs(chg) >= 1.5 and self.status == "normal":
            self.status = "uyari"

    def margin_check(self):
        """Maintenance margin (FINRA 4210). IBKR liquidates in real time when equity < requirement."""
        self.revalue()
        eq = self.P["totalValue"]
        if eq <= 0:
            for p in list(self.P["positions"]):
                q = self.quotes.get(p["ticker"], {"price": p["lastPrice"], "at": pd.Timestamp(self.now)})
                self.close(p, p["shares"], q["price"], q["at"], "Hesap değeri sıfırın altına indi; tüm pozisyonlar "
                           "kapatıldı.", "teminat çağrısı")
            self.status = "alarm"
            return

        def bad():
            self.revalue()
            return (self.P["totalValue"] < self.P["maintenanceReq"]
                    or (self.P["leverage"] or 0) > GROSS_ECON_CAP + 0.25)

        if not bad():
            return
        self.status = "alarm"
        why = (f"Teminat çağrısı: net değer {usd(self.P['totalValue'])}, FINRA 4210 bakım teminatı "
               f"{usd(self.P['maintenanceReq'])}. Aracı kurum (IBKR) bu durumda beklemeden pozisyon kapatır.")
        self.comply("FINRA 4210", "Teminat çağrısı", why, "regt")
        for p in sorted(list(self.P["positions"]), key=lambda p: p["unrealizedPct"]):
            if not bad():
                break
            q = self.quotes.get(p["ticker"])
            if not q or q["stale"]:
                continue
            self.close(p, p["shares"], q["price"], q["at"], why + " En çok zararda olan pozisyondan başlanarak kapatılıyor.",
                       "teminat çağrısı")

    # ---------------- daily decision
    def rank(self, hist):
        rows = []
        for t in U.UNIVERSE:
            h = hist.get(t)
            if h is None or len(h) < 130:
                continue
            c = list(h["Close"])
            price = c[-1]
            r21, r63, r126 = (price / c[-22] - 1) * 100, (price / c[-64] - 1) * 100, (price / c[-127] - 1) * 100
            sma50 = sum(c[-50:]) / 50
            score = 0.2 * r21 + 0.5 * r63 + 0.3 * r126
            why = []
            if price < 5:
                why.append("fiyat 5 $ altında")
            if price <= sma50:
                why.append("50 günlük ortalamanın altında")
            if r63 <= 0:
                why.append("3 aylık getiri negatif")
            short_ok = (U.kind_of(t) == "Hisse" and price >= MIN_SHORT_PRICE and price < sma50 and r21 < 0 and r63 < 0)
            rows.append({"ticker": t, "name": U.name_of(t), "price": round(price, 2), "r21": round(r21, 2),
                         "r63": round(r63, 2), "r126": round(r126, 2), "sma50": round(sma50, 2),
                         "score": round(score, 2), "eligible": not why, "why": ", ".join(why), "shortOk": short_ok})
        rows.sort(key=lambda r: r["score"], reverse=True)
        n = 0
        for r in rows:
            if r["eligible"]:
                n += 1
                r["rank"] = n
        shorts = sorted([r for r in rows if r["shortOk"]], key=lambda r: r["score"])
        for i, r in enumerate(shorts, 1):
            r["srank"] = i
        return rows, shorts

    def regime(self, hist):
        spy = hist.get("SPY")
        vix = hist.get("^VIX")
        c = list(spy["Close"]) if spy is not None else []
        v = float(vix["Close"].iloc[-1]) if vix is not None and len(vix) else 20.0
        if len(c) < 200:
            reg = {"name": "neutral", **REGIMES["neutral"], "budget": 1.0}
            reg["text"] = "SPY geçmişi yetersiz; kararsız rejim varsayıldı."
            return reg
        price, sma50, sma200 = c[-1], sum(c[-50:]) / 50, sum(c[-200:]) / 200
        name = "bear" if price < sma200 else ("bull" if price > sma50 and v < 25 else "neutral")
        reg = {"name": name, **REGIMES[name], "budget": HIGH_VIX_BUDGET if v >= HIGH_VIX else 1.0}
        parts = [f"long hisse %{int(reg['longStocks'] * 100)}"]
        if reg["bullEtf"]:
            parts.append(f"3x endeks ETF'i %{int(reg['bullEtf'] * 100)}")
        if reg["bearEtf"]:
            parts.append(f"−3x ters ETF %{int(reg['bearEtf'] * 100)}")
        parts.append(f"short %{int(reg['shorts'] * 100)}")
        reg["text"] = (f"{reg['label']}: SPY {trn(price)} $, 50 günlük ortalama {trn(sma50)} $, 200 günlük ortalama "
                       f"{trn(sma200)} $, VIX {trn(v)}. Reg T alım gücünün dağılımı: " + ", ".join(parts) + "."
                       + (" VIX yüksek olduğu için alım gücünün yalnızca %60'ı kullanılıyor." if reg["budget"] < 1 else ""))
        return reg

    def indicators(self, hist):
        out = []
        for t, label in U.INDICATORS:
            h = hist.get(t)
            if h is None or len(h) < 2:
                continue
            last, prev = float(h["Close"].iloc[-1]), float(h["Close"].iloc[-2])
            if t == "^TNX":
                val = "%" + trn(last, 2)
                chg = ("+" if last >= prev else "−") + trn(abs(last - prev) * 100, 0) + " baz puan"
            elif t == "^VIX":
                val, chg = trn(last, 2), trpct((last / prev - 1) * 100)
            else:
                val, chg = trn(last, 2) + " $", trpct((last / prev - 1) * 100)
            out.append({"label": label, "value": val, "change": chg, "ticker": t})
        return out

    def trend(self, done):
        """Market trend for the basket from completed daily bars: SPY's last close vs its 200-day average."""
        spy = done.get("SPY")
        c = list(spy["Close"]) if spy is not None else []
        if len(c) < 200:
            return {"up": True, "text": "SPY geçmişi yetersiz; trend yukarı varsayıldı.", "spy": None, "sma200": None}
        sma200 = sum(c[-200:]) / 200
        up = c[-1] > sma200
        gap = (c[-1] / sma200 - 1) * 100
        if BASKET_SHARE <= 0:
            tail = "(v6'da momentum sepeti kapalı; bu bilgi kayıt içindir.)"
        elif up:
            tail = "Sepet: en güçlü 10 hissede long."
        elif BASKET_SHORTS:
            tail = "Sepet: düşüş trendinde en zayıf 10 hissede short."
        else:
            tail = "Sepet: hisseler satılıp nakitte bekleniyor; SPY ortalamanın üstüne dönünce yeniden alınacak."
        text = (f"Piyasa trendi {'yukarı' if up else 'AŞAĞI'}: SPY dünkü kapanış {trn(c[-1])} $, 200 günlük ortalama "
                f"{trn(sma200)} $ ({trpct(gap)}). " + tail)
        return {"up": up, "text": text, "spy": c[-1], "sma200": sma200, "gapPct": gap}

    def nasdaq_watch(self):
        """Daily: the Nasdaq 100 fund's (QQQ) all-time closing high from its full history."""
        h = self.data.daily([INDEX_ETF], period="max").get(INDEX_ETF)
        if h is None or len(h) < 2:
            return
        done = h[h.index.date < self.now.date()]["Close"]
        if done.empty:
            return
        W = self.M.get("nasdaq") or {}
        self.M["nasdaq"] = W
        W.update(ath=round(float(done.max()), 2), athDate=done.idxmax().date().isoformat(), athChecked=self.today)
        if self.quotes.get(INDEX_ETF):
            self.nasdaq_update()
        else:
            self.nasdaq_update(float(done.iloc[-1]), done.index[-1])

    def nasdaq_update(self, price=None, at=None):
        """Every run: distance of QQQ from its all-time high, the alert level and whether a 20 % fall is active."""
        W = self.M.get("nasdaq")
        q = self.quotes.get(INDEX_ETF)
        if not W or not W.get("ath"):
            return
        if price is None:
            if not q:
                return
            price, at = q["price"], q["at"]
        if price > W["ath"]:
            W.update(ath=round(price, 2), athDate=self.today)
        dd = price / W["ath"] - 1
        level = "bear" if dd <= DIP_LEVELS[1] else "correction" if dd <= DIP_LEVELS[0] else "normal"
        if level == "bear" and not W.get("active"):
            W.update(active=True, since=self.today)
            self.comply("Nasdaq izleme", "Nasdaq 100 zirvesinden %20 düştü",
                        f"QQQ {trn(price)} $, {W['athDate']} tarihli zirve {trn(W['ath'])} $ ({trpct(dd * 100)}). Bu, "
                        "piyasada ayı piyasası eşiği sayılır.", None)
        elif W.get("active") and dd >= 0:
            W.update(active=False, endedAt=self.today)
        W.update(price=round(price, 2), at=iso(at), ddPct=round(dd * 100, 2), level=level,
                 triggerPrice=round(W["ath"] * (1 + DIP_LEVELS[1]), 2),
                 correctionPrice=round(W["ath"] * (1 + DIP_LEVELS[0]), 2))

    def index_signal(self, done):
        """Direction and volatility-targeted size of the index sleeve, from completed daily bars."""
        h, s_ = done.get(INDEX_ETF), done.get(DIR_SRC)
        if h is None or s_ is None or len(h) < 201 or len(s_) < 200:
            return None
        c = h["Close"].astype(float)
        rv = float(c.pct_change().iloc[-20:].std() * math.sqrt(252))
        sc = [float(x) for x in s_["Close"]]
        last, sma50, sma200 = sc[-1], sum(sc[-50:]) / 50, sum(sc[-200:]) / 200
        dirn = 1 if last > sma200 else (-1 if sma50 < sma200 else 0)
        W = self.M.get("nasdaq") or {}
        blocked = DIP_NO_SHORT and dirn < 0 and bool(W.get("active"))
        if blocked:
            dirn = 0
        raw = VOL_TARGET / max(rv, VOL_FLOOR)
        cap = MAX_EXPO if dirn >= 0 else min(MAX_EXPO, MAX_SHORT_EXPO)
        mag = min(cap, raw)
        target = round(dirn * mag, 2)
        word = "AŞAĞI, SHORT YOK" if blocked else {1: "YUKARI", -1: "AŞAĞI", 0: "KARARSIZ"}[dirn]
        low = "aşağı (short yok)" if blocked else {1: "yukarı", -1: "aşağı", 0: "kararsız"}[dirn]
        why = (f"{DIR_SRC} dünkü kapanış {trn(last)} $, 200 günlük ortalama {trn(sma200)} $ "
               f"({trpct((last / sma200 - 1) * 100)}), 50 günlük ortalama {trn(sma50)} $")
        den = max(rv, VOL_FLOOR)
        size = (f"{INDEX_ETF}'nun 20 günlük oynaklığı yıllık %{trn(rv * 100, 1)}; büyüklük %{trn(VOL_TARGET * 100, 0)} ÷ %{trn(den * 100, 1)}"
                + (" (oynaklık alt sınırı)" if rv < VOL_FLOOR else "") + f" = {trn(raw, 2)}x"
                + (f", {trn(cap, 0)}x {'short ' if dirn < 0 else ''}sınırına indirildi" if raw > cap else ""))
        if blocked:
            tail = (f"Yön aşağı, ama Nasdaq 100 zirvesinden {trpct(W.get('ddPct', 0))} aşağıda ({W.get('since', '')} tarihinden beri "
                    "%20'nin üstünde düşüş): bu noktada short geç kalmış olur ve sert tepki yükselişlerine yakalanır. Kural gereği "
                    "endeks kolu yeni zirveye kadar short açmaz, nakitte bekler.")
        elif dirn:
            tail = f"{size}. Hedef maruziyet {'+' if target > 0 else '−'}{trn(abs(target), 2)}x."
        else:
            tail = ("Fiyat 200 günlük ortalamanın altında ama 50 günlük ortalama henüz 200 günlüğün altına inmedi; yön teyit "
                    "edilmediği için endeks kolu nakitte.")
        text = f"Endeks kolu yönü {word}: {why}. {tail}"
        return {"dir": dirn, "word": word, "wordLow": low, "vol": round(rv * 100, 2), "magnitude": round(mag, 2), "target": target,
                "price": round(last, 2), "sma50": round(sma50, 2), "sma200": round(sma200, 2), "src": DIR_SRC, "text": text,
                "dipBlock": blocked}

    @staticmethod
    def index_weights(target):
        """Sleeve weights that give `target` x exposure with cash-funded ETFs: QQQ + TQQQ long, SQQQ short."""
        if target >= 0:
            a = max(0.0, (target - 1) / 2)
            return {INDEX_ETF: target if target <= 1 else 1 - a, INDEX_BULL: a, INDEX_BEAR: 0.0}
        return {INDEX_ETF: 0.0, INDEX_BULL: 0.0, INDEX_BEAR: min(1.0, -target / 3)}

    def index_state(self):
        self.revalue()
        sleeve = max(0.0, self.P["totalValue"] * INVEST_USE * INDEX_SHARE)
        mv = {t: (self._pos(t, "long") or {}).get("marketValue", 0.0) for t in INDEX_SLEEVE}
        now = (mv[INDEX_ETF] + 3 * mv[INDEX_BULL] - 3 * mv[INDEX_BEAR]) / sleeve if sleeve > 0 else 0.0
        return sleeve, mv, now

    def _index_reason(self, t, sig, have, want):
        lev = {INDEX_ETF: "1x", INDEX_BULL: "3x", INDEX_BEAR: "−3x"}[t]
        return f"Endeks kolu, {t} ({lev}): {usd(have)} → {usd(want)}. {sig['text']}"

    def index_rebalance(self, sig, phase, decisions, rejected, sources):
        """phase 'sell': decide and trim/close sleeve positions above target (returns False when no trade is needed);
        phase 'buy': top up the positions below target with the cash left."""
        sleeve, mv, now = self.index_state()
        target = sig["target"]
        if phase == "sell":
            self.M["index"] = {**{k: sig[k] for k in ("dir", "word", "vol", "magnitude", "target", "src")},
                               "now": round(now, 2), "date": self.today}
            flat = all(v < MIN_TRADE for v in mv.values())
            if abs(target - now) < VT_BAND and (target == 0) == flat:
                decisions.append(f"Endeks kolu: hedef {trn(target, 2)}x, mevcut {trn(now, 2)}x; fark {trn(VT_BAND, 1)}x'ten "
                                 "küçük olduğu için işlem yapılmadı.")
                return False
            decisions.append(f"Endeks kolu: maruziyet {trn(now, 2)}x → {trn(target, 2)}x.")
        w = self.index_weights(target)
        want = {t: w[t] * sleeve for t in INDEX_SLEEVE}
        self.quotes_for(list(INDEX_SLEEVE))
        for t in INDEX_SLEEVE:
            p, q = self._pos(t, "long"), self.quotes.get(t)
            have = p["marketValue"] if p else 0.0
            if phase == "sell" and p and (have - want[t] >= MIN_TRADE or want[t] < MIN_TRADE):
                if not q or q["stale"]:
                    rejected.append(f"{t}: güncel fiyat yok, endeks kolu satışı sonraki çalışmaya kaldı.")
                    continue
                full = want[t] < MIN_TRADE
                shares = p["shares"] if full else (have - want[t]) / q["price"]
                if self.close(p, shares, q["price"], q["at"], self._index_reason(t, sig, have, 0.0 if full else want[t]),
                              "endeks kolu"):
                    decisions.append(f"{t} {'SAT' if full else 'AZALT'}: {usd(have)} → {usd(0.0 if full else want[t])}.")
                    sources.append(src(t))
            elif phase == "buy" and want[t] - have >= MIN_TRADE:
                if not q or q["stale"]:
                    rejected.append(f"{t}: güncel fiyat yok, endeks kolu alımı sonraki çalışmaya kaldı.")
                    continue
                room = max(0.0, self.P["cash"]) / (1 + SLIP) - COMM_MIN * 2
                amount, limit = self._allowed_mv(t, "long", min(want[t] - have, room))
                if amount < MIN_TRADE:
                    rejected.append(f"{t}: endeks kolu alımı yapılamadı ("
                                    + ("nakit kalmadı; kredi kullanılmıyor" if not limit else
                                       "Reg T teminatı yetmedi" if limit == "regt" else "3x maruziyet sınırı") + ").")
                    continue
                if limit:
                    self.comply("Reg T" if limit == "regt" else "risk", f"{t}: emir küçültüldü",
                                f"İstenen {usd(want[t] - have)} yerine {usd(amount)}; daha büyüğü "
                                + ("Reg T başlangıç teminatını aşardı." if limit == "regt" else "3x ekonomik maruziyet sınırını aşardı."),
                                "regt" if limit == "regt" else "letf")
                if self.open_long(t, amount, q["price"], q["at"], self._index_reason(t, sig, have, have + amount), "endeks kolu"):
                    decisions.append(f"{t} AL: {usd(amount)}" + (f" (×{U.lev_of(t)} = {usd(amount * U.lev_of(t))} ekonomik "
                                     "maruziyet)" if t != INDEX_ETF else "") + ".")
                    sources.append(src(t))
        return True

    def momentum_table(self, done):
        cool = self.M.get("cooldown", {})
        d0 = date.fromisoformat(self.today)
        rows = []
        for t in U.UNIVERSE:
            if t in U.ETFS:
                continue
            h = done.get(t)
            if h is None or len(h) < 253:
                continue
            c = list(h["Close"])
            price = c[-1]
            mom = c[-22] / c[-253] - 1
            sma200 = sum(c[-200:]) / 200
            cooled = t in cool and (d0 - date.fromisoformat(cool[t])).days <= COOLDOWN_DAYS
            why = []
            if price < 5:
                why.append("fiyat 5 $ altında")
            if price <= sma200:
                why.append("200 günlük ortalamanın altında")
            if mom <= 0:
                why.append("12-1 ay getirisi negatif")
            if cooled:
                why.append(f"{cool[t]} felaket zarar-kesi sonrası bekleme")
            short_ok = (U.kind_of(t) == "Hisse" and price >= MIN_SHORT_PRICE and price < sma200 and mom < 0 and not cooled)
            rows.append({"ticker": t, "name": U.name_of(t), "price": round(price, 2), "mom": round(mom * 100, 2),
                         "score": round(mom * 100, 2), "r21": round((price / c[-22] - 1) * 100, 2),
                         "r63": round((price / c[-64] - 1) * 100, 2), "r126": round((price / c[-127] - 1) * 100, 2),
                         "sma200": round(sma200, 2), "group": U.group_of(t),
                         "groupLabel": U.GROUP_LABELS.get(U.group_of(t), ""), "eligible": not why, "why": ", ".join(why),
                         "shortOk": short_ok})
        rows.sort(key=lambda r: r["mom"], reverse=True)
        n = 0
        for r in rows:
            if r["eligible"]:
                n += 1
                r["rank"] = n
        for i, r in enumerate(sorted([r for r in rows if r["shortOk"]], key=lambda r: r["mom"]), 1):
            r["srank"] = i
        return rows

    def basket_rebalance(self, trend, rows, decisions, rejected, sources):
        self.revalue()
        if not trend["up"]:
            for p in [p for p in self.P["positions"] if p["ticker"] not in INDEX_SLEEVE and p.get("sleeve") != "event"]:
                if p["side"] == "short" and BASKET_SHORTS:
                    continue
                q = self.quotes.get(p["ticker"])
                if not q or q["stale"]:
                    rejected.append(f"{p['ticker']}: güncel fiyat yok, satış sonraki çalışmaya kaldı.")
                    continue
                self.close(p, p["shares"], q["price"], q["at"],
                           "Trend filtresi: SPY 200 günlük ortalamasının altına indi; momentum sepeti kapatılıyor.",
                           "trend filtresi")
                sources.append(src(p["ticker"]))
            if BASKET_SHORTS:
                self.basket_shorts(rows, decisions, rejected, sources)
            else:
                decisions.append("Momentum sepeti satıldı; sepet kısmı nakitte bekliyor.")
            return
        by_t = {r["ticker"]: r for r in rows}
        keep = []
        for p in list(self.P["positions"]):
            t = p["ticker"]
            if t in INDEX_SLEEVE or p.get("sleeve") == "event":
                continue
            q = self.quotes.get(t)
            r = by_t.get(t)
            if p["side"] == "long" and t not in U.LEVERAGED and r and r.get("rank", 99) <= BASKET_KEEP:
                keep.append(t)
                decisions.append(f"{t} tut: momentum sırası {r['rank']} (12-1 ay getirisi {trpct(r['mom'])}).")
                continue
            why = ("piyasa trendi yukarı, short sepet kapatılıyor" if p["side"] == "short" else
                   "3x fonlar yalnızca endeks kolunda kullanılıyor" if t in U.LEVERAGED else
                   r["why"] if r and not r["eligible"] else
                   f"momentum sırası {r['rank']}, ilk {BASKET_KEEP} dışında" if r else "momentum verisi yok")
            if not q or q["stale"]:
                rejected.append(f"{t}: kapatılmalıydı ama güncel fiyat yok, sonraki çalışmaya kaldı.")
                continue
            self.close(p, p["shares"], q["price"], q["at"], f"Aylık dengeleme: {why}.", "dengeleme")
            decisions.append(f"{t} {'SAT' if p['side'] == 'long' else 'KAPAT'}: {why}.")
            sources.append(src(t))
        groups = {}
        for t in keep:
            g = U.group_of(t)
            groups[g] = groups.get(g, 0) + 1
        chosen = list(keep)
        for r in rows:
            if len(chosen) >= BASKET_N:
                break
            if not r["eligible"] or r["ticker"] in chosen:
                continue
            if groups.get(r["group"], 0) >= GROUP_CAP:
                rejected.append(f"{r['ticker']}: sıra {r['rank']} ama {r['groupLabel']} sektöründen zaten {GROUP_CAP} hisse var.")
                continue
            chosen.append(r["ticker"])
            groups[r["group"]] = groups.get(r["group"], 0) + 1
        self.revalue()
        eq = self.P["totalValue"]
        each = eq * INVEST_USE * BASKET_SHARE / BASKET_N
        targets = [(t, each) for t in chosen]
        self.quotes_for([t for t, _ in targets])
        for t, tgt in targets:                                   # trims first: they free cash
            p, q = self._pos(t, "long"), self.quotes.get(t)
            if p and q and not q["stale"] and p["marketValue"] > tgt * 1.25:
                self.close(p, (p["marketValue"] - tgt) / q["price"], q["price"], q["at"],
                           f"Aylık dengeleme: pozisyon hedefin ({usd(tgt)}) %25'ten fazla üstünde; hedefe indirildi.", "dengeleme")
        for t, tgt in targets:
            p, q = self._pos(t, "long"), self.quotes.get(t)
            have = p["marketValue"] if p else 0.0
            if have >= tgt * 0.8:
                continue
            if not q or q["stale"]:
                rejected.append(f"{t}: güncel 5 dakikalık fiyat yok, alım sonraki dengelemeye kaldı.")
                continue
            room = max(0.0, self.P["cash"]) / (1 + SLIP) - COMM_MIN * 2
            mv = min(tgt - have, room)
            if mv < MIN_TRADE:
                rejected.append(f"{t}: nakit kalmadı (kredi kullanılmıyor).")
                continue
            r = by_t[t]
            why = (f"Momentum sepeti: 12-1 ay getirisi {trpct(r['mom'])} ile uygun hisseler arasında {r['rank']}. sırada; "
                   f"fiyat 200 günlük ortalamanın ({trn(r['sma200'])} $) üstünde; sektör: {r['groupLabel']}. Ay sonuna kadar "
                   f"tutulur; yalnızca maliyetin %25 altına inerse satılır.")
            if p:
                why = f"Hedef büyüklüğe tamamlama ({usd(have)} → {usd(tgt)}). " + why
            if self.open_long(t, mv, q["price"], q["at"], why, "momentum sepeti"):
                decisions.append(f"{t} AL: {usd(mv)}" + (" (tamamlama)" if p else "") + ".")
                sources.append(src(t))

    def basket_shorts(self, rows, decisions, rejected, sources):
        """Market down-trend (only when BASKET_SHORTS): short the 10 weakest stocks below their 200-day average."""
        shorts = sorted([r for r in rows if r.get("shortOk")], key=lambda r: r["mom"])
        srank = {r["ticker"]: r["srank"] for r in shorts}
        keep = [p["ticker"] for p in self.P["positions"]
                if p["side"] == "short" and srank.get(p["ticker"], 999) <= BASKET_KEEP]
        for p in [p for p in self.P["positions"] if p["side"] == "short" and p["ticker"] not in keep]:
            q = self.quotes.get(p["ticker"])
            if not q or q["stale"]:
                continue
            self.close(p, p["shares"], q["price"], q["at"], "Aylık dengeleme: short sıralamasında ilk "
                       f"{BASKET_KEEP} dışına çıktı.", "dengeleme")
        chosen = keep + [r["ticker"] for r in shorts if r["ticker"] not in keep][:max(0, BASKET_N - len(keep))]
        self.revalue()
        each = self.P["totalValue"] * INVEST_USE * BASKET_SHARE / BASKET_N
        self.quotes_for(chosen)
        logged = False
        for t in chosen:
            p, q = self._pos(t, "short"), self.quotes.get(t)
            have = abs(p["marketValue"]) if p else 0.0
            if have >= each * 0.8:
                continue
            if not q or q["stale"]:
                rejected.append(f"{t}: güncel fiyat yok, short sonraki çalışmaya kaldı.")
                continue
            if q.get("ssr"):
                rejected.append(f"{t}: SEC Rule 201 (açığa satış devre kesicisi) aktif; bugün short açılamaz.")
                self.comply("SEC Rule 201", f"{t}: açığa satış kısıtı",
                            f"{t} önceki kapanışa göre %10 veya daha fazla düştüğü için (bugün ya da dün) en iyi alış fiyatının "
                            "altında açığa satış yasak; bot bugün bu hisseyi short'lamadı.", "ssr")
                continue
            amount, limit = self._allowed_mv(t, "short", each - have)
            if limit == "min2000":
                rejected.append(f"{t}: özsermaye 2.000 $'ın altında, açığa satış yapılamaz (FINRA 4210).")
                if not logged:
                    logged = True
                    self.comply("FINRA 4210(b)(4)", "Açığa satış engellendi: özsermaye 2.000 $ altında",
                                f"Özsermaye {usd(self.P['totalValue'])}. Açığa satış için hesapta en az 2.000 $ özsermaye "
                                "olmalı; aracı kurum bu emirleri kabul etmez.", "min2000")
                continue
            if amount < MIN_TRADE:
                rejected.append(f"{t}: short açılamadı (Reg T teminatı yetmedi).")
                continue
            r = next(x for x in shorts if x["ticker"] == t)
            why = (f"Short sepeti (piyasa trendi aşağı): 12-1 ay getirisi {trpct(r['mom'])} ile en zayıf hisseler arasında "
                   f"{r['srank']}. sırada; fiyat 200 günlük ortalamanın ({trn(r['sma200'])} $) altında. Reg T: satış geliri + "
                   "%50 teminat. Fiyat maliyetin %25 üstüne çıkarsa kapatılır.")
            if self.open_short(t, amount, q["price"], q["at"], why, "short sepeti"):
                decisions.append(f"{t} SHORT: {usd(amount)}.")
                sources.append(src(t))

    def daily(self):
        held = [p["ticker"] for p in self.P["positions"]]
        extra = list(INDEX_SLEEVE) + [DIR_SRC]
        hist = self.data.daily(U.UNIVERSE + extra + held + [t for t, _ in U.INDICATORS], period="2y")
        today = self.now.date()
        done = {t: h[h.index.date < today] for t, h in hist.items() if h is not None and len(h)}
        inds = self.indicators(hist)
        self.dividends()
        trend = self.trend(done)
        rows = self.momentum_table(done)
        try:
            self.nasdaq_watch()
        except Exception as e:                     # the watch is informational; never block the daily decision
            self.notes.append(f"Nasdaq izleme verisi alınamadı: {type(e).__name__}")
        sig = self.index_signal(done)
        month = self.today[:7]
        flip = self.M.get("trendUp") is not None and self.M.get("trendUp") != trend["up"]
        due = self.M.get("lastRebalance") != month or flip
        decisions, rejected, watch = [], [], []
        decisions.append(sig["text"] if sig else "Endeks kolu: QQQ geçmişi alınamadı; bugün değişiklik yapılmadı.")
        decisions.append(trend["text"])
        sources = [YF_SOURCE] + [src(t) for t, _ in U.INDICATORS] + [src(INDEX_ETF)]
        basket_word = "long" if trend["up"] else ("short" if BASKET_SHORTS else "nakit")
        idx_word = (f"endeks {'+' if sig['target'] > 0 else '−' if sig['target'] < 0 else ''}{trn(abs(sig['target']), 2)}x"
                    if sig else "endeks verisi yok")
        self.M["regime"] = {"name": "up" if sig and sig["dir"] > 0 else "down" if sig and sig["dir"] < 0 else "neutral",
                            "label": f"Yön {sig['wordLow'] if sig else '—'}: {idx_word}, "
                                     + (f"sepet {basket_word}" if BASKET_SHARE > 0 else "haber şoku kolu açık"),
                            "budget": 1.0}
        go = self.index_rebalance(sig, "sell", decisions, rejected, sources) if sig else False
        if BASKET_SHARE <= 0:
            due = False
            decisions.append("Momentum sepeti v6'da kapalı: paranın yarısı endeks kolunda, yarısı haber şoku kolunda.")
        elif due:
            if flip:
                decisions.append("Piyasa trendi yön değiştirdiği için sepet ay beklenmeden dengelendi.")
            self.basket_rebalance(trend, rows, decisions, rejected, sources)
            self.M["lastRebalance"] = month
        if BASKET_SHARE > 0 and not due:
            nxt = [d for d in trading_days(self.today, (date.fromisoformat(self.today) + timedelta(days=40)).isoformat())
                   if d[:7] != month]
            decisions.append(f"Sepet: ay içi kontrol, felaket zarar-kesleri izlendi, işlem gerekmedi. Sonraki aylık dengeleme "
                             f"{nxt[0] if nxt else 'gelecek ay'}.")
        if go:
            self.index_rebalance(sig, "buy", decisions, rejected, sources)
        self.M["trendUp"] = trend["up"]
        held_now = {p["ticker"] for p in self.P["positions"]}
        for r in [x for x in rows if x["eligible"] and x["ticker"] not in held_now][:5]:
            watch.append({"ticker": r["ticker"], "note": f"Sepet adayı: sıra {r['rank']}, 12-1 ay {trpct(r['mom'])}, {r['groupLabel']}."})
        shorts = sorted([r for r in rows if r.get("shortOk")], key=lambda r: r["mom"])
        self.revalue()
        self.M["lastDailyDate"] = self.today
        P = self.P
        parts = [f"{i['label']} {i['change']}" for i in inds if i["ticker"] in ("SPY", "QQQ", "IWM")]
        others = [f"{i['label']} {i['value']} ({i['change']})" for i in inds if i["ticker"] in ("^VIX", "CL=F", "^TNX")]
        summary = (" · ".join(parts) + ". " + "; ".join(others) + ". " + (sig["text"] + " " if sig else "") + trend["text"] +
                   f" Sonuç: net değer {usd(P['totalValue'])}, nakit {usd(P['cash'])}, kaldıraç {trn(P['leverage'] or 0, 2)}x.")
        n_open = sum(1 for t in self.new_trades if t["action"] in ("BUY", "SHORT"))
        n_close = sum(1 for t in self.new_trades if t["action"] in ("SELL", "COVER"))
        tail = f"{self.M['regime']['label']}, kaldıraç {trn(P['leverage'] or 0, 2)}x"
        headline = (f"{n_open} alım, {n_close} satış · {tail}" if (n_open or n_close) else f"İşlem yok · {tail}")
        uniq, seen = [], set()
        for s_ in sources:
            if s_["url"] not in seen:
                seen.add(s_["url"])
                uniq.append({**s_, "publisher": "Yahoo Finance" if "yahoo" in s_["url"] else "GitHub",
                             "usedFor": "Fiyat ve geçmiş veriler" if "yahoo" in s_["url"] else "Veri kütüphanesi"})
        self.L["runs"].append({
            "date": self.today, "at": iso(self.now), "by": "bot", "headline": headline, "marketSummary": summary,
            "regime": self.M["regime"], "rebalanced": due, "index": self.M.get("index"),
            "indicators": [{"label": i["label"], "value": i["value"], "change": i["change"]} for i in inds],
            "decisions": decisions, "rejected": rejected, "watchlist": watch, "sources": uniq,
            "ranking": rows[:20] if BASKET_SHARE > 0 else [], "shortRanking": shorts[:10] if BASKET_SHARE > 0 else [],
            "rankingNote": ("Puan = 12-1 ay getirisi (son ay hariç 12 aylık getiri, %). Long için uygun: fiyat 200 günlük "
                            "ortalamanın üstünde ve puan pozitif; sepet ilk 10'dan (sektör başına en fazla 3) oluşur, ilk 20'de "
                            "kalan tutulur. Short listesi: fiyatı 200 günlük ortalamanın altında ve puanı negatif en zayıf "
                            "hisseler" + (" (piyasa trendi aşağı dönerse short sepeti buradan kurulur)." if BASKET_SHORTS
                                          else " (yalnızca izleme; v5'te sepet düşüşte nakde geçer).")),
            "nextSteps": "Endeks kolunun yönü ve büyüklüğü her gün 10:00'dan sonra, haber şoku taraması 10:30'dan sonra; "
                         "haber işlemlerinin zarar-kesleri piyasa açıkken 5 dakikada bir kontrol ediliyor.",
        })

    # ---------------- news-shock sleeve (v6)
    def _trading_day_after(self, d, n):
        days = trading_days(d, (date.fromisoformat(d) + timedelta(days=n * 2 + 10)).isoformat())
        return days[min(n, len(days) - 1)]

    def _headline(self, t):
        """Latest Yahoo Finance headline for the ticker (best effort; the trade never depends on it)."""
        try:
            import news as N
            items = N.fetch_yf(t)
        except Exception:
            items = []
        at = lambda n: n.get("at").isoformat() if hasattr(n.get("at"), "isoformat") else str(n.get("at") or "")
        cutoff = (date.fromisoformat(self.today) - timedelta(days=3)).isoformat()
        fresh = [n for n in items if at(n)[:10] >= cutoff]
        n = (fresh or items or [None])[0]
        return {"title": n["title"], "url": n["url"], "publisher": n.get("publisher"), "at": at(n) or None} if n else None

    def event_scan(self):
        """Once a day after the first hour (10:30 New York time): stocks that opened at least 8 % away from yesterday's
        close and that the market turned against the gap during the first hour. Trade in the direction of that turn,
        stop just beyond the other end of the first-hour range, exit at the close two trading days later."""
        self.M["eventScanDate"] = self.today
        uni = U.EVENT_UNIVERSE
        bars = self.data.intraday(uni)
        hist = self.data.daily(uni, period="5d")
        today = self.now.date()
        scan = {"date": self.today, "at": iso(self.now), "checked": 0, "gaps": [], "taken": [], "skipped": [],
                "rule": f"Açılış boşluğu en az %{trn(EVENT_GAP * 100, 0)}; ilk saatte haberin tersine en az "
                        f"%{trn(EVENT_MIN_MOVE * 100, 1)} dönüş; {EVENT_HOLD_DAYS} işlem günü tut, zarar-kes ilk saatin öbür ucu."}
        held = {p["ticker"] for p in self.P["positions"]}
        open_ev = [p for p in self.P["positions"] if p.get("sleeve") == "event"]
        cands = []
        for t in uni:
            b, h = bars.get(t), hist.get(t)
            if b is None or h is None:
                continue
            b = b[b.index.date == today]
            past = h[h.index.date < today]
            if b.empty or len(past) < 2:
                continue
            scan["checked"] += 1
            first = b.index[0]
            if first.hour != 9 or first.minute != 30:
                continue
            h1 = b[b.index < first.replace(hour=10, minute=30)]
            if len(h1) < 10:
                continue
            pc, o = float(past["Close"].iloc[-1]), float(h1["Open"].iloc[0])
            gap = o / pc - 1
            if abs(gap) < EVENT_GAP or o < 5:
                continue
            hc, hh, hl = float(h1["Close"].iloc[-1]), float(h1["High"].max()), float(h1["Low"].min())
            move = hc / o - 1
            row = {"ticker": t, "name": U.name_of(t), "gapPct": round(gap * 100, 2), "firstHourPct": round(move * 100, 2),
                   "prevClose": round(pc, 2), "open": round(o, 2), "h1High": round(hh, 2), "h1Low": round(hl, 2),
                   "h1Close": round(hc, 2)}
            scan["gaps"].append(row)
            if abs(move) < EVENT_MIN_MOVE:
                scan["skipped"].append({**row, "why": "ilk saatte belirgin bir yön yok"})
                continue
            if (move > 0) == (gap > 0):
                scan["skipped"].append({**row, "why": "ilk saatte haberin yönünde devam etti; testte bu durum zarar ettirdi"})
                continue
            side = "long" if move > 0 else "short"
            ssr = hl <= pc * (1 - SSR_DROP) or float(past["Low"].iloc[-1]) <= float(past["Close"].iloc[-2]) * (1 - SSR_DROP)
            cands.append({**row, "side": side, "ssr": ssr,
                          "stop": round(hl * 0.999, 2) if side == "long" else round(hh * 1.001, 2)})
        cands.sort(key=lambda r: -abs(r["gapPct"]))
        self.revalue()
        sleeve = self.P["totalValue"] * EVENT_SHARE
        used = sum(abs(p["marketValue"]) for p in open_ev)
        slots = EVENT_SLOTS - len([p for p in open_ev if not p.get("spike")])
        lines = []
        for r in cands:
            t, side = r["ticker"], r["side"]
            if t in held:
                scan["skipped"].append({**r, "why": "bu hisse zaten portföyde"})
                continue
            if slots <= 0:
                scan["skipped"].append({**r, "why": f"en fazla {EVENT_SLOTS} açık haber işlemi"})
                continue
            if side == "short" and r["ssr"]:
                scan["skipped"].append({**r, "why": "SEC Rule 201: hisse önceki kapanışa göre %10 düştüğü için short açılamaz"})
                self.comply("SEC Rule 201", f"{t}: açığa satış kısıtı",
                            f"{t} bugün ya da dün önceki kapanışa göre %10 veya daha fazla düştü; en iyi alış fiyatının altında "
                            "açığa satış yasak. Haber şoku kuralı bu hissede short açmadı.", "ssr")
                continue
            self.quotes_for([t])
            q = self.quotes.get(t)
            if not q or q["stale"]:
                scan["skipped"].append({**r, "why": "güncel fiyat yok"})
                continue
            price = q["price"]
            risk = (price / r["stop"] - 1) if side == "long" else (r["stop"] / price - 1)
            if risk <= 0.002:
                scan["skipped"].append({**r, "why": "fiyat zarar-kes seviyesine çok yakın"})
                continue
            want = min(sleeve * min(EVENT_CAP, EVENT_RISK / risk), max(0.0, sleeve - used))
            if side == "long":
                want = min(want, max(0.0, self.P["cash"]) / (1 + SLIP) - COMM_MIN * 2)
            amount, limit = self._allowed_mv(t, side, want)
            if amount < MIN_TRADE:
                scan["skipped"].append({**r, "why": "özsermaye 2.000 $ altında, short açılamaz (FINRA 4210)"
                                        if limit == "min2000" else "yeterli nakit ya da teminat yok"})
                continue
            news = self._headline(t)
            exit_day = self._trading_day_after(self.today, EVENT_HOLD_DAYS)
            word = "düşüşle" if r["gapPct"] < 0 else "yükselişle"
            turn = "toparlandı" if side == "long" else "geri verdi"
            reason = (f"Haber şoku: {t} önceki kapanışa ({trn(r['prevClose'])} $) göre {trpct(r['gapPct'])} {word} açıldı, ilk "
                      f"saatte {trpct(r['firstHourPct'])} {turn}: piyasa haberin ilk tepkisini abartılı buldu. Kural gereği "
                      f"{'long' if side == 'long' else 'short'} açıldı. Zarar-kes {trn(r['stop'])} $ (ilk saatin "
                      f"{'en düşüğünün' if side == 'long' else 'en yükseğinin'} hemen ötesi, risk %{trn(risk * 100, 1)}); "
                      f"çıkış {exit_day} kapanışında. Büyüklük: haber kolunun %{trn(min(EVENT_CAP, EVENT_RISK / risk) * 100, 0)}'i, "
                      f"zarar-keste kaybı kolun yaklaşık %{trn(EVENT_RISK * 100, 0)}'i.")
            if news:
                reason += f" Haber: “{news['title']}” ({news.get('publisher') or 'Yahoo Finance'})."
            fn = self.open_long if side == "long" else self.open_short
            tr = fn(t, amount, price, q["at"], reason, "haber şoku")
            if not tr:
                scan["skipped"].append({**r, "why": "emir gerçekleşmedi"})
                continue
            if news:
                tr["sources"].insert(0, {"title": news["title"], "url": news["url"]})
            pos = self._pos(t, side)
            pos.update(sleeve="event", stopMode="event", stopPrice=r["stop"], extremeSinceEntry=price, exitDate=exit_day,
                       event={k: r[k] for k in ("gapPct", "firstHourPct", "prevClose", "open", "h1High", "h1Low")},
                       headline=news)
            used += amount
            slots -= 1
            held.add(t)
            scan["taken"].append({**r, "amount": round(amount, 2), "price": round(price, 2), "exitDate": exit_day,
                                  "headline": news})
            lines.append(f"Haber şoku: {t} {'LONG' if side == 'long' else 'SHORT'} {usd(amount)} (boşluk "
                         f"{trpct(r['gapPct'])}, ilk saat {trpct(r['firstHourPct'])}).")
        if not scan["gaps"]:
            lines.append(f"Haber şoku: {scan['checked']} hissede %{trn(EVENT_GAP * 100, 0)}+ açılış boşluğu yok; işlem yapılmadı.")
        elif not scan["taken"]:
            lines.append(f"Haber şoku: {len(scan['gaps'])} hissede büyük boşluk var ama kurala uyan işlem çıkmadı.")
        self.M["eventScan"] = scan
        run = next((x for x in reversed(self.L["runs"]) if x.get("date") == self.today), None)
        if run is not None:
            run["events"] = scan
            run.setdefault("decisions", []).extend(lines)
            if scan["taken"]:
                run["headline"] = f"{len(scan['taken'])} haber şoku işlemi · " + run.get("headline", "")
        else:
            self.notes.extend(lines)

    def _trading_day_before(self, d):
        days = trading_days((date.fromisoformat(d) - timedelta(days=12)).isoformat(), d)
        return days[-2] if len(days) >= 2 and days[-1] == d else days[-1]

    def spike_scan(self):
        """Once a day right after the open (sert yükseliş short'u): the Nasdaq stocks that closed at least 30 % higher on
        the previous trading day (list written by movers.py) are shorted at the open, stop 50 % above the entry, covered at
        the close of the 10th trading day. spike_study.py (2019-2026, real price >= 5 $, 20 M $ a day): such spikes gave
        back a large part of the jump; profitable in both halves of the period, after a 30 %/year borrow fee."""
        self.M["spikeScanDate"] = self.today
        prev_day = self._trading_day_before(self.today)
        try:
            mv = json.loads(MOVERS.read_text(encoding="utf-8"))
        except Exception:
            mv = {}
        day = (mv.get("spikeHistory") or {}).get(prev_day) or {}
        scan = {"date": self.today, "at": iso(self.now), "spikeDate": prev_day, "final": day.get("final"),
                "listAt": day.get("at"), "candidates": [], "taken": [], "skipped": [],
                "rule": f"{prev_day} günü en az %{trn(SPIKE_MOVE * 100, 0)} yükselen Nasdaq hissesine açılışta short; zarar-kes "
                        f"girişin %{trn(SPIKE_STOP * 100, 0)} üstü; {SPIKE_HOLD_DAYS}. işlem günü kapanışta çıkış."}
        rows = sorted([r for r in day.get("rows") or [] if (r.get("d1Pct") or 0) >= SPIKE_MOVE * 100],
                      key=lambda r: -r["d1Pct"])
        scan["candidates"] = [r["ticker"] for r in rows]
        self.revalue()
        sleeve = self.P["totalValue"] * EVENT_SHARE
        open_ev = [p for p in self.P["positions"] if p.get("sleeve") == "event"]
        used = sum(abs(p["marketValue"]) for p in open_ev)
        slots = SPIKE_SLOTS - len([p for p in open_ev if p.get("spike")])
        held = {p["ticker"] for p in self.P["positions"]}
        lines = []
        for r in rows:
            t = r["ticker"]
            base = {k: r.get(k) for k in ("ticker", "name", "d1Pct", "prevClose", "price", "dollarVolM")}
            why = None
            if any(n.lower() in (r.get("name") or "").lower() for n in SANCTIONED_NAMES):
                why = "ABD yaptırım listesindeki şirket; asla işlem yapılmaz"
            elif r.get("buyout"):
                why = "satın alma fiyatı gibi yatay seyrediyor; short anlamsız"
            elif (r.get("prevClose") or 0) < SPIKE_MIN_PRICE:
                why = f"önceki kapanış {trn(SPIKE_MIN_PRICE, 0)} $'ın altında (çok küçük hisse)"
            elif (r.get("dollarVolM") or 0) < SPIKE_MIN_DV:
                why = f"günlük işlem hacmi {trn(SPIKE_MIN_DV, 0)} milyon $'ın altında"
            elif t in held:
                why = "bu hisse zaten portföyde"
            elif slots <= 0:
                why = f"en fazla {SPIKE_SLOTS} açık sert yükseliş short'u"
            if why:
                scan["skipped"].append({**base, "why": why})
                continue
            self.quotes_for([t])
            q = self.quotes.get(t)
            if not q or q["stale"]:
                scan["skipped"].append({**base, "why": "güncel fiyat yok"})
                continue
            price = q["price"]
            if q["ssr"]:
                scan["skipped"].append({**base, "why": "SEC Rule 201: bugün %10'dan fazla düştü, short açılamaz"})
                self.comply("SEC Rule 201", f"{t}: açığa satış kısıtı",
                            f"{t} önceki kapanışa göre %10 veya daha fazla düştü; en iyi alış fiyatının altında açığa satış "
                            "yasak. Sert yükseliş kuralı bu hissede short açmadı.", "ssr")
                continue
            if price < SPIKE_MIN_PRICE:
                scan["skipped"].append({**base, "why": f"fiyat {trn(SPIKE_MIN_PRICE, 0)} $'ın altında"})
                continue
            want = min(sleeve * SPIKE_SIZE, max(0.0, sleeve - used))
            amount, limit = self._allowed_mv(t, "short", want)
            if amount < MIN_TRADE:
                scan["skipped"].append({**base, "why": "özsermaye 2.000 $ altında, short açılamaz (FINRA 4210)"
                                        if limit == "min2000" else "haber kolunda ya da teminatta yer yok"})
                continue
            stop = round(price * (1 + SPIKE_STOP), 2)
            exit_day = self._trading_day_after(self.today, SPIKE_HOLD_DAYS - 1)
            news = r.get("headline") or self._headline(t)
            back = (price / r["price"] - 1) * 100 if r.get("price") else None
            reason = (f"Sert yükseliş short'u: {t} ({r.get('name') or t}) {prev_day} günü {trpct(r['d1Pct'])} yükseldi "
                      f"({trn(r['prevClose'])} $ → {trn(r['price'])} $)"
                      + (f"; bugün {trn(price)} $'dan ({trpct(back)}) işlem görüyor" if back is not None else "")
                      + f". 2019–2026 testinde bu tür sıçramalar sonraki iki haftada genellikle geri verildi. Short açıldı: "
                      f"kolun %{trn(SPIKE_SIZE * 100, 0)}'i, zarar-kes {trn(stop)} $ (girişin %{trn(SPIKE_STOP * 100, 0)} "
                      f"üstü), çıkış {exit_day} kapanışında. Ödünç ücreti yıllık %{trn(SPIKE_BORROW * 100, 0)} varsayıldı.")
            if news and news.get("title"):
                reason += f" Haber: “{news['title']}” ({news.get('publisher') or 'Yahoo Finance'})."
            tr = self.open_short(t, amount, price, q["at"], reason, "sert yükseliş short", slip=SPIKE_SLIP,
                                 locate=f"Zor ödünç (HTB) varsayımı: yıllık %{trn(SPIKE_BORROW * 100, 0)} ödünç ücreti")
            if not tr:
                scan["skipped"].append({**base, "why": "emir gerçekleşmedi"})
                continue
            if news and news.get("url"):
                tr["sources"].insert(0, {"title": news["title"], "url": news["url"]})
            pos = self._pos(t, "short")
            pos.update(sleeve="event", spike=True, stopMode="event", stopPrice=stop, extremeSinceEntry=price,
                       exitDate=exit_day, holdDays=SPIKE_HOLD_DAYS, borrowRate=SPIKE_BORROW, slip=SPIKE_SLIP,
                       name=r.get("name") or t, headline=news,
                       event={"spikeDate": prev_day, "spikePct": r["d1Pct"], "prevClose": r["prevClose"], "close": r["price"]})
            used += amount
            slots -= 1
            held.add(t)
            scan["taken"].append({**base, "amount": round(amount, 2), "entry": round(price, 2), "stop": stop,
                                  "exitDate": exit_day, "headline": news})
            lines.append(f"Sert yükseliş short'u: {t} {usd(amount)} ({prev_day} günü {trpct(r['d1Pct'])}).")
        if not rows:
            lines.append(f"Sert yükseliş short'u: {prev_day} günü %{trn(SPIKE_MOVE * 100, 0)}+ yükselen uygun Nasdaq hissesi "
                         "yok" + ("" if day else " (hareketliler listesi bulunamadı)") + ".")
        elif not scan["taken"]:
            lines.append(f"Sert yükseliş short'u: {len(rows)} aday vardı, kurala uyan işlem çıkmadı.")
        self.M["spikeScan"] = scan
        run = next((x for x in reversed(self.L["runs"]) if x.get("date") == self.today), None)
        if run is not None:
            run["spikes"] = scan
            run.setdefault("decisions", []).extend(lines)
        else:
            self.notes.extend(lines)

    def event_exits(self):
        for p in [p for p in self.P["positions"] if p.get("sleeve") == "event" and p.get("exitDate")]:
            due = self.today > p["exitDate"] or (self.today == p["exitDate"] and self.now.time() >= EVENT_EXIT_AFTER)
            q = self.quotes.get(p["ticker"])
            if not due or not q or q["stale"]:
                continue
            if p.get("spike"):
                self.close(p, p["shares"], q["price"], q["at"],
                           f"Sert yükseliş short'unun süresi doldu: {p.get('openedAt')} tarihinde açıldı; plan gereği "
                           f"{p.get('holdDays', SPIKE_HOLD_DAYS)}. işlem gününün kapanışında kapatıldı.", "sert yükseliş çıkış")
                continue
            self.close(p, p["shares"], q["price"], q["at"],
                       f"Haber şoku işleminin süresi doldu: {p.get('openedAt')} tarihinde açıldı; plan gereği "
                       f"{EVENT_HOLD_DAYS} işlem günü sonra kapanışta kapatıldı.", "haber şoku çıkış")

    # ---------------- run
    def run(self):
        held = [p["ticker"] for p in self.P["positions"]]
        self.load_quotes(held + ["SPY", INDEX_ETF])
        if "SPY" not in self.quotes:
            self.status = "uyari"
            self.notes.append("SPY verisi alınamadı; bu çalışmada işlem yapılmadı")
            self.revalue()
            self.log_check("guard")
            return
        self.accrue()
        self.apply_deposits()
        self.guard()
        self.margin_check()
        self.nasdaq_update()
        self.event_exits()
        kind = "guard"
        if (self.now.time() >= DAILY_AFTER and self.M.get("lastDailyDate") != self.today
                and not self.quotes["SPY"]["stale"]):
            self.daily()
            kind = "daily"
            self.margin_check()
        if (EVENT_SHARE > 0 and SPIKE_AFTER <= self.now.time() < SPIKE_LAST_ENTRY
                and self.M.get("spikeScanDate") != self.today and not self.quotes["SPY"]["stale"]):
            try:
                self.spike_scan()
                kind = "spike" if kind == "guard" else kind
            except Exception as e:             # one rule; never block the rest of the run
                self.M["spikeScanDate"] = self.today
                self.notes.append(f"Sert yükseliş taraması yapılamadı: {type(e).__name__}: {e}")
            self.margin_check()
        if (EVENT_SHARE > 0 and EVENT_AFTER <= self.now.time() < EVENT_LAST_ENTRY
                and self.M.get("eventScanDate") != self.today and not self.quotes["SPY"]["stale"]):
            try:
                self.event_scan()
                kind = "event"
            except Exception as e:             # the scan is one sleeve; never block the rest of the run
                self.M["eventScanDate"] = self.today
                self.notes.append(f"Haber şoku taraması yapılamadı: {type(e).__name__}: {e}")
            self.margin_check()
        self.revalue()
        if self.status == "normal":
            for p in self.P["positions"]:
                d = self._dist(p)
                if d is not None and d < 3:
                    self.status = "uyari"
                    self.notes.append(f"{p['ticker']} zarar-kes seviyesine %3'ten yakın")
                    break
            if self.P["marginBuffer"] < 0.10 * self.P["totalValue"]:
                self.status = "uyari"
                self.notes.append("Bakım teminatı tamponu net değerin %10'unun altında")
        self.log_check(kind)
        self.snapshot()
        self.scorecard()

    @staticmethod
    def _dist(p):
        if not p.get("stopPrice"):
            return None
        if p["side"] == "long":
            return (p["lastPrice"] / p["stopPrice"] - 1) * 100
        return (p["stopPrice"] / p["lastPrice"] - 1) * 100

    def log_check(self, kind):
        spy = self.quotes.get("SPY", {})
        entry = {
            "at": iso(self.now), "kind": kind, "status": self.status,
            "spyPrice": round(spy["price"], 2) if spy else None,
            "spyChangePct": round(spy["dayChangePct"], 2) if spy else None,
            "totalValue": self.P["totalValue"], "benchmarkValue": self.P.get("benchmarkValue"),
            "leverage": self.P.get("leverage"), "marginBuffer": self.P.get("marginBuffer"),
            "positions": [{
                "ticker": p["ticker"], "side": p["side"], "price": p["lastPrice"],
                "dayChangePct": round(self.quotes[p["ticker"]]["dayChangePct"], 2) if p["ticker"] in self.quotes else None,
                "stopPrice": p.get("stopPrice"),
                "distancePct": round(self._dist(p), 2) if self._dist(p) is not None else None,
            } for p in self.P["positions"]],
            "actions": self.actions,
        }
        if self.notes:
            entry["note"] = "; ".join(self.notes)
        elif not self.actions:
            dist = [e["distancePct"] for e in entry["positions"] if e["distancePct"] is not None]
            entry["note"] = ("Normal: " + (f"pozisyonlar zarar-kese %{trn(min(dist), 1)}–%{trn(max(dist), 1)} uzakta, " if dist else "")
                             + (f"kaldıraç {trn(self.P['leverage'], 2)}x, " if self.P.get("leverage") else "")
                             + (f"SPY {trpct(spy['dayChangePct'])}; " if spy else "") + "işlem gerekmedi.")
        checks = self.L.setdefault("checks", {})
        checks.setdefault(self.today, []).append(entry)
        for d in sorted(checks)[:-CHECK_DAYS_KEPT]:
            del checks[d]
        self.M["lastRunAt"] = iso(self.now)

    def scorecard(self):
        """Running 3-month evaluation (time-weighted, so deposits do not count as gains)."""
        E = self.M.setdefault("evaluation", {})
        ev_start, ev_end, ev_ts = E.get("start", EVAL_START), E.get("end", EVAL_END), E.get("startTs", EVAL_START_TS)
        if self.today < ev_start:
            return
        units = self.M.get("units") or self.C["startCapital"]
        nav = self.P["totalValue"] / units
        spy = self.quotes.get("SPY")
        spy_px = spy["price"] if spy else self.P.get("benchmarkPrice")
        if "navStart" not in E:
            seen = [e for d in sorted(self.L.get("checks", {})) for e in self.L["checks"][d]
                    if e.get("at", "") >= ev_ts and e.get("totalValue")]
            first = seen[0] if seen else {"at": iso(self.now), "totalValue": self.P["totalValue"], "spyPrice": spy_px}
            E.update(startAt=first["at"], navStart=first["totalValue"] / units, spyStart=first["spyPrice"] or spy_px,
                     peak=first["totalValue"] / units, maxDD=0.0, maxDDAt=first["at"], errors=E.get("errors", 0),
                     runDays=sorted({e["at"][:10] for e in seen}))
            for e in seen:
                self._track_dd(E, e["totalValue"] / units, e["at"])
        self._track_dd(E, nav, iso(self.now))
        if self.today not in E["runDays"]:
            E["runDays"].append(self.today)
        ret = (nav / E["navStart"] - 1) * 100
        spy_ret = (spy_px / E["spyStart"] - 1) * 100 if spy_px and E.get("spyStart") else None
        closes = [t for t in self.L["trades"] if not t.get("cancelled") and t.get("action") in ("SELL", "COVER")
                  and t.get("ts", "") > E["startAt"]]     # the switch-over trades themselves are not counted
        wins = [num(t.get("realizedPL")) for t in closes if num(t.get("realizedPL")) > 0]
        losses = [num(t.get("realizedPL")) for t in closes if num(t.get("realizedPL")) <= 0]
        margin_calls = sum(1 for t in self.L["trades"] if t.get("rule") == "teminat çağrısı" and t.get("ts", "") >= E["startAt"]
                           and not t.get("cancelled"))
        comp = {}
        for c in self.L.get("compliance", []):
            if c.get("at", "") > E["startAt"] and c.get("rule") not in SETUP_RULES:
                comp[c["rule"]] = comp.get(c["rule"], 0) + 1
        # monthly returns from the daily snapshots (last snapshot of each month)
        months, prev_nav, prev_spy = [], E["navStart"], E["spyStart"]
        snaps = [s for s in self.L["snapshots"] if s["date"] >= ev_start and s.get("twrPct") is not None]
        by_month = {}
        for sn in snaps:
            by_month[sn["date"][:7]] = sn
        for m in sorted(by_month):
            sn = by_month[m]
            m_nav = 1 + sn["twrPct"] / 100
            m_spy = sn.get("benchmarkPrice")
            row = {"month": m, "returnPct": round((m_nav / prev_nav - 1) * 100, 2)}
            if m_spy and prev_spy:
                row["spyPct"] = round((m_spy / prev_spy - 1) * 100, 2)
                row["excessPct"] = round(row["returnPct"] - row["spyPct"], 2)
            months.append(row)
            prev_nav, prev_spy = m_nav, m_spy or prev_spy
        today_counts = self.now.time() >= dtime(9, 35)
        due = [d for d in trading_days(ev_start, min(self.today, ev_end)) if d < self.today or today_counts]
        run_days = [d for d in E["runDays"] if d in due]
        all_days = trading_days(ev_start, ev_end)
        uptime = round(len(run_days) / len(due) * 100, 1) if due else 100.0
        excess = round(ret - spy_ret, 2) if spy_ret is not None else None
        dd_now = round((nav / E["peak"] - 1) * 100, 2) + 0.0
        self.L["scorecard"] = {
            "start": ev_start, "startAt": E["startAt"], "end": ev_end, "asOf": iso(self.now),
            "strategy": E.get("strategy", "v3"), "history": self.L.get("evalHistory", []),
            "daysElapsed": len(due), "daysTotal": len(all_days), "finished": self.today >= ev_end,
            "returnPct": round(ret, 2), "spyReturnPct": round(spy_ret, 2) if spy_ret is not None else None,
            "excessPct": excess, "maxDrawdownPct": E["maxDD"], "maxDrawdownAt": E["maxDDAt"], "drawdownNowPct": dd_now,
            "trades": {"closed": len(closes), "wins": len(wins),
                       "winRatePct": round(len(wins) / len(closes) * 100, 1) if closes else None,
                       "avgWin": round(sum(wins) / len(wins), 2) if wins else None,
                       "avgLoss": round(sum(losses) / len(losses), 2) if losses else None,
                       "profitFactor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
                       "realizedPL": round(sum(wins) + sum(losses), 2)},
            "monthly": months, "uptime": {"due": len(due), "ran": len(run_days), "pct": uptime},
            "errors": E.get("errors", 0), "marginCalls": margin_calls, "compliance": comp,
            "criteria": [
                {"key": "spy", "label": "SPY'ı geçmek (masraflar düşülmüş)", "ok": excess is not None and excess > 0,
                 "value": excess, "unit": "pct"},
                {"key": "dd", "label": f"En büyük düşüş %{trn(-EVAL_MAX_DD, 0)}'den az", "ok": E["maxDD"] > EVAL_MAX_DD,
                 "value": E["maxDD"], "unit": "pct"},
                {"key": "margin", "label": "Teminat çağrısı yok", "ok": margin_calls == 0, "value": margin_calls, "unit": "count"},
                {"key": "uptime", "label": f"Bot işlem günlerinin en az %{trn(EVAL_UPTIME, 0)}'inde çalıştı",
                 "ok": uptime >= EVAL_UPTIME, "value": uptime, "unit": "pctPlain"},
            ],
        }

    @staticmethod
    def _track_dd(E, nav, at):
        if nav > E["peak"]:
            E["peak"] = nav
        dd = round((nav / E["peak"] - 1) * 100, 2)
        if dd < E["maxDD"]:
            E["maxDD"], E["maxDDAt"] = dd, at

    def snapshot(self):
        snaps = [s for s in self.L["snapshots"] if s["date"] != self.today]
        snaps.append({"date": self.today, "at": iso(self.now), "totalValue": self.P["totalValue"],
                      "cash": self.P["cash"], "positionsValue": self.P["positionsValue"], "plPct": self.P["totalPLPct"],
                      "twrPct": self.P.get("twrPct"), "netDeposits": self.P.get("netDeposits"),
                      "leverage": self.P.get("leverage"), "benchmarkPrice": self.P.get("benchmarkPrice"),
                      "benchmarkValue": self.P.get("benchmarkValue"),
                      "holdings": [{"ticker": p["ticker"], "side": p["side"], "value": p["marketValue"]}
                                   for p in self.P["positions"]]})
        snaps.sort(key=lambda s: s["date"])
        self.L["snapshots"] = snaps


def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def save_ledger(ledger, path=LEDGER):
    Path(path).write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    now = datetime.fromisoformat(os.environ["BOT_NOW"]).astimezone(NY) if os.environ.get("BOT_NOW") else datetime.now(NY)
    if not market_open(now) and os.environ.get("BOT_FORCE") != "1":
        print(f"{now:%Y-%m-%d %H:%M} ET: piyasa kapalı, çıkılıyor.")
        return 0
    import data
    ledger = load_json(LEDGER)
    status, actions, traded = "hata", [], False
    try:
        eng = Engine(ledger, data, now, load_json(DEPOSITS, []), load_json(RESTORE))
        eng.run()
        ledger["meta"]["lastError"] = None
        status, actions, traded = eng.status, eng.actions, bool(eng.new_trades)
    except Exception as e:
        traceback.print_exc()
        ledger = load_json(LEDGER)  # discard partial changes
        ledger.setdefault("meta", {})["lastError"] = f"{iso(now)}: {type(e).__name__}: {e}"
        if "evaluation" in ledger["meta"]:
            ledger["meta"]["evaluation"]["errors"] = ledger["meta"]["evaluation"].get("errors", 0) + 1
        ledger["meta"]["lastRunAt"] = iso(now)
    save_ledger(ledger)
    if traded:
        Path(".traded").write_text("1")
    print(f"{now:%H:%M} ET | durum={status} | net={ledger['portfolio']['totalValue']} | "
          f"kaldıraç={ledger['portfolio'].get('leverage')} | işlemler={actions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
