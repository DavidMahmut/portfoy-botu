"""Rule-based long/short paper-trading bot that follows the real US rules for a small margin account.

Model: an Interactive Brokers (IBKR Pro, Tiered) margin account under Regulation T and FINRA Rule 4210.
Runs every 5 minutes during US market hours (a GitHub Actions job loops until the close). Each run:
  1. marks the portfolio to market with Yahoo Finance 5-minute bars,
  2. accrues margin interest and short borrow fees, applies deposits (deposits.json), books dividends,
  3. replays the bars since the last run against each position's standing stop order,
  4. applies take-profit, single-stock shock, crash/squeeze brakes and the maintenance-margin check,
  5. once per day after 10:00 New York time, sets the market regime, ranks the universe by momentum
     and rebalances within Reg T buying power (longs in the leaders, shorts in the weakest
     downtrending stocks, a 3x index ETF in a strong bull market or a -3x inverse ETF in a bear market),
  6. logs every rule that limited or shaped a decision (compliance log) and a simple tax view
     (holding period, wash sales, dividends), and writes everything to ledger.json.

No AI and no paid service is involved; all decisions follow the fixed rules below.
"""
import json
import math
import os
import sys
import traceback
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import universe as U

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / "ledger.json"
DEPOSITS = ROOT / "deposits.json"
RESTORE = ROOT / "v3_restore.json"
ENGINE = "reg-t-bot v3"

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
    "strategyName": "Long/short momentum botu (gerçek kurallara uygun kredili hesap)",
    "strategySummary": (
        "En güçlü momentuma sahip hisselerde long, en zayıf ve düşüş trendindeki hisselerde short pozisyon alır. "
        "ABD'de 2.000 $'lık bir kredili hesapta gerçekte yapılabilecek işlemlerle sınırlıdır: Reg T başlangıç teminatı, "
        "FINRA 4210 bakım teminatı ve 2.000 $ asgari özsermaye, SEC Rule 201, gerçek aracı kurum ücretleri. Güçlü "
        "yükselişte 3x endeks ETF'i, düşüş piyasasında −3x ters ETF kullanır. Aracı kurum modeli: Interactive Brokers "
        "(IBKR Pro, Tiered). İlk gün (8 Ekim) alımlarını Claude yaptı; 12 Ekim'de sermaye önce 2.000 $'a, ardından FINRA'nın "
        "2.000 $ asgarisinin üstünde güvenli bir tampon kalsın diye 3.000 $'a çıkarıldı."),
    "execution": (
        "Yahoo Finance 5 dakikalık verileri (yfinance). Kesirli hisse (IBKR kredili hesapta kesirli açığa satışa da izin "
        "veriyor). Her emirde IBKR Pro Tiered komisyonu: hisse başına 0,0035 $, en az 0,35 $, en fazla işlem tutarının %1'i. "
        "Satışlarda (açığa satış dahil) SEC ücreti (milyon $'da 20,60 $) ve FINRA TAF (hisse başına 0,000195 $, en az "
        "0,01 $). %0,05 fiyat kayması. Kredi faizi yıllık %5,38 (IBKR, 100 bin $ altı), açığa satış ödünç ücreti yıllık "
        "%0,5 (kolay ödünç alınan büyük hisseler varsayımı). Temettüler long'da alınır, short'ta ödenir."),
    "broker": "Interactive Brokers, IBKR Pro (Tiered) kredili hesap modeli",
    "rules": [
        "Hesap: Reg T kredili hesap. FINRA 4210(b)(4) gereği her açığa satış ve kredili alım için en az 2.000 $ özsermaye "
        "gerekir. Özsermaye 2.000 $'ın altına inerse yeni short açılmaz ve long'lar yalnızca nakitle alınır; mevcut "
        "pozisyonlar için ek para istenmez.",
        "Başlangıç teminatı (Reg T): long hisselerde değerin %50'si, short'ta %150'si (satış geliri + %50). 3x ETF'lerde "
        "FINRA'nın kaldıraçlı ETF kuralı gereği %75. Yeni bir emir ancak sonrasında başlangıç teminatı özsermayeyi "
        "aşmıyorsa verilir; aşacaksa aracı kurum emri reddeder ve bu mevzuat günlüğüne yazılır.",
        "Bakım teminatı (FINRA 4210): long %25, short değerin %30'u ama hisse başına en az 5 $, 3x ETF long %75. "
        "Özsermaye bunun altına inerse aracı kurum (IBKR) gerçek zamanlı olarak pozisyon kapatır; bot da en çok zararda "
        "olandan başlayarak kapatır.",
        "Gün içi işlem: Eski 'pattern day trader' kuralı (25.000 $ asgari, 5 günde en fazla 3 gün içi işlem) SEC'in 14 Nisan "
        "2026 onayıyla 4 Haziran 2026'dan itibaren kaldırıldı; yerine gün içi teminat yeterliliği geldi. Bot gün içi "
        "işlem saymaz ama her an bakım teminatını kontrol eder.",
        "Açığa satış: SEC Rule 201 gereği hisse önceki kapanışa göre %10 düştüğünde o gün ve ertesi gün en iyi alış "
        "fiyatının altında açığa satış yapılamaz; bot bu durumda yeni short açmaz. Ödünç (locate) için yalnızca büyük, "
        "likit ve 10 $ üstü hisseler seçilir (kolay ödünç varsayımı).",
        "Piyasa rejimi (her gün): Güçlü yükseliş (SPY 50 ve 200 günlük ortalamasının üstünde, VIX < 25) → Reg T alım "
        "gücünün %55'i long hisse, %20'si 3x endeks ETF'i (TQQQ/UPRO/SOXL), %25'i short. Kararsız → %60 long, %40 short. "
        "Düşüş (SPY 200 günlük ortalamanın altında) → %30 long, %25 −3x ters ETF (SQQQ/SPXU/SOXS), %45 short. VIX ≥ 35 "
        "ise alım gücünün yalnızca %60'ı kullanılır. Ekonomik kaldıraç (3x ETF'ler 3 katıyla) hiçbir zaman 3x'i geçmez.",
        "Momentum puanı = 0,2 × 1 aylık + 0,5 × 3 aylık + 0,3 × 6 aylık getiri. Long: fiyatı 50 günlük ortalamanın "
        "üstünde ve 3 aylık getirisi pozitif olanlardan en yüksek puanlı 4 hisse/ETF; ilk 10'dan düşen satılır. Short: "
        "fiyatı 10 $ üstü, 50 günlük ortalamanın altında, 1 ve 3 aylık getirisi negatif hisselerden en zayıf 3'ü; en zayıf "
        "10'dan çıkan kapatılır.",
        "Hedef büyüklükten %40 küçük kalan pozisyon tamamlanır, %50 büyüyen pozisyon hedefe indirilir.",
        "Bekleyen zarar-kes emri: long'da maliyet × 0,92 ile en yüksek fiyat × 0,88'in büyüğü (3x ETF'lerde × 0,85 ve "
        "× 0,80); short'ta maliyet × 1,08 ile en düşük fiyat × 1,12'nin küçüğü. 5 dakikalık mumlarla sırayla kontrol edilir; "
        "fiyat seviyeyi aşarak açılırsa açılış fiyatından kapatılır.",
        "Kâr alma ±%20'de yarısı; tek hisse şoku (ters yönde %7 ve 50 günlük ortalamanın kırılması) → kapat. Çöküş freni: "
        "SPY −%3 → long maruziyet ≤ 1x (−%5'te 0,5x), o gün yeni long yok. Sıkışma freni: SPY +%3 → short maruziyet "
        "≤ 0,5x, o gün yeni short yok.",
        "Vergi (öğrenme amaçlı, basitleştirilmiş): 1 yıl veya daha kısa tutulan pozisyonların kazancı kısa vadeli sayılır "
        "ve normal gelir gibi vergilenir. Zararla kapatılan bir pozisyon 30 gün içinde yeniden açılırsa (wash sale) o zarar "
        "o yıl düşülemez, yeni pozisyonun maliyetine eklenir. Net sermaye zararının yıllık en fazla 3.000 $'ı gelirden "
        "düşülebilir. Kredi faizi yatırım faizi gideri olarak (Form 4952) düşülebilir. Bunlar vergi tavsiyesi değildir.",
        "Veri 35 dakikadan eskiyse o varlıkta işlem yapılmaz. Her işlem fiyatı, saati, kaynağı, komisyonu, ücretleri, "
        "elde tutma süresi ve tetikleyen kuralla kaydedilir.",
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
        self.C["maxLeverage"] = GROSS_ECON_CAP
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
        stop = round(ex * (STOP_LONG[lev][0] if side == "long" else STOP_SHORT[0]), 2)
        return {
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

    def open_short(self, t, dollars, quote, quote_at, reason, rule):
        ex = round(quote * (1 - SLIP), 4)
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
                       extra={"locate": "Kolay ödünç (ETB) varsayımı: büyük, likit hisse"})
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
        ex = round(quote * (1 + SLIP), 4)
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
        if spy:
            self.P["benchmarkPrice"] = round(spy["price"], 2)
            self.P["benchmarkValue"] = round(self.C["benchmark"]["shares"] * spy["price"], 2)
            self.P["benchmarkTwrPct"] = round((spy["price"] / self.C["benchmark"]["startPrice"] - 1) * 100, 2)
        assert abs(self.P["cash"] + sum(p["marketValue"] for p in self.P["positions"]) - equity) <= 0.011

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
        short_v = sum(p["shares"] * p["lastPrice"] for p in self.P["positions"] if p["side"] == "short")
        interest = round(debit * MARGIN_RATE * days / 360, 2)
        borrow = round(short_v * BORROW_RATE * days / 360, 2)
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
            b = q["bars"]
            if p.get("openedTs"):
                b = b[b.index > pd.Timestamp(p["openedTs"])]
            if p.get("lastBarTs"):
                b = b[b.index >= pd.Timestamp(p["lastBarTs"])]
            ext, stop, hit = p["extremeSinceEntry"], p["stopPrice"], None
            if p["side"] == "long":
                trail = STOP_LONG[p.get("lev", 1)][1]
                for ts, row in b.iterrows():
                    eff = round(max(stop, ext * trail), 2)
                    if row["Low"] <= eff:
                        hit = (ts, eff, float(row["Low"]), float(row["Open"]) if row["Open"] < eff else eff)
                        break
                    ext = max(ext, float(row["High"]))
                    stop = round(max(stop, ext * trail), 2)
                stop = round(max(stop, ext * trail), 2)
            else:
                trail = STOP_SHORT[1]
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
                reason = (f"Bekleyen zarar-kes emri tetiklendi: {hm(ts)} ET mumunda fiyat {trn(extreme)} $'a {word}, "
                          f"zarar-kes seviyesi {trn(eff)} $. "
                          + (f"Mum seviyenin ötesinde açıldığı için açılış fiyatından ({trn(fill)} $) kapatıldı."
                             if fill != eff else f"Emir {trn(fill)} $'dan gerçekleşti."))
                self.close(p, p["shares"], fill, ts, reason, "zarar-kes")
                self.status = "alarm"
                continue
            p["extremeSinceEntry"] = round(ext, 4)
            p["stopPrice"] = stop
            if len(b):
                p["lastBarTs"] = iso(b.index[-1])
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
        self.brakes()

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

    def daily(self):
        held = [p["ticker"] for p in self.P["positions"]]
        hist = self.data.daily(U.UNIVERSE + list(U.LEVERAGED) + held + [t for t, _ in U.INDICATORS], period="1y")
        ranking, shorts = self.rank(hist)
        inds = self.indicators(hist)
        reg = self.regime(hist)
        self.M["regime"] = {k: v for k, v in reg.items() if k != "text"}
        by_t = {r["ticker"]: r for r in ranking}
        short_by_t = {r["ticker"]: r for r in shorts}
        decisions, rejected, watch = [reg["text"]], [], []
        min2000_logged = False
        sources = [YF_SOURCE] + [src(t) for t, _ in U.INDICATORS]
        self.dividends()

        self.revalue()
        eq = self.P["totalValue"]
        eligible = eq >= MIN_MARGIN_EQUITY
        budget = BUDGET_USE * eq * reg["budget"]
        idx_rows = []
        for t in ("QQQ", "SPY", "SMH"):
            if t in by_t:
                idx_rows.append(by_t[t])
            elif hist.get(t) is not None and len(hist[t]) >= 130:
                c = list(hist[t]["Close"])
                idx_rows.append({"ticker": t, "score": 0.2 * (c[-1] / c[-22] - 1) * 100 + 0.5 * (c[-1] / c[-64] - 1) * 100
                                 + 0.3 * (c[-1] / c[-127] - 1) * 100})
        bull_etf = bear_etf = None
        if reg["bullEtf"] and idx_rows:
            bull_etf = U.BULL_ETF.get(max(idx_rows, key=lambda r: r["score"])["ticker"])
        if reg["bearEtf"] and idx_rows:
            bear_etf = U.BEAR_ETF.get(min(idx_rows, key=lambda r: r["score"])["ticker"])
        if not eligible:
            self.comply("FINRA 4210(b)(4)", "2.000 $ altı: kredi ve short yok",
                        f"Özsermaye {usd(eq)}; 2.000 $'ın altında olduğu için yeni açığa satış ve kredili alım yapılamaz. "
                        "Long'lar yalnızca mevcut nakitle alınır, mevcut pozisyonlar tutulabilir.", "min2000")
            decisions.append("Özsermaye 2.000 $'ın altında: yeni short ve kredili alım yok (FINRA 4210).")
        # market value targets = sleeve's share of the initial-margin budget / initial margin rate
        # (below 2.000 $ the account works like a cash account: rate 100 %, no shorts; the short sleeve stays in cash)
        def rate(t):
            return init_rate(t, "long") if eligible else 1.0
        long_t = budget * reg["longStocks"] / (REGT_INIT if eligible else 1.0) / N_LONG
        short_t = budget * reg["shorts"] / REGT_INIT / N_SHORT if eligible else 0.0
        bull_t = budget * reg["bullEtf"] / rate(bull_etf) if bull_etf else 0.0
        bear_t = budget * reg["bearEtf"] / rate(bear_etf) if bear_etf else 0.0

        # 1) exits
        for p in list(self.P["positions"]):
            t = p["ticker"]
            q = self.quotes.get(t)
            if t in U.LEVERAGED:
                if t in (bull_etf, bear_etf):
                    decisions.append(f"{t} tut: rejim dilimi ({U.kind_of(t)}).")
                    continue
                why = "piyasa rejimi değişti, bu endeks dilimi artık hedefte değil"
            elif p["side"] == "long":
                r = by_t.get(t)
                if r and r["eligible"] and r.get("rank", 99) <= KEEP_RANK_LONG:
                    decisions.append(f"{t} long tut: momentum sıralamasında {r['rank']}. (puan {trn(r['score'])}), "
                                     f"zarar-kes {trn(p['stopPrice'])} $.")
                    continue
                why = (r["why"] if r and not r["eligible"] else
                       f"sıralamada {r['rank']}. sıraya düştü (ilk {KEEP_RANK_LONG} dışında)" if r else "momentum verisi yok")
            else:
                r = short_by_t.get(t)
                if r and r["srank"] <= KEEP_RANK_SHORT:
                    decisions.append(f"{t} short tut: en zayıflar listesinde {r['srank']}. (puan {trn(r['score'])}), "
                                     f"zarar-kes {trn(p['stopPrice'])} $.")
                    continue
                why = ("düşüş trendi bozuldu (50 günlük ortalamanın üstüne çıktı veya getiriler pozitife döndü)" if not r
                       else f"en zayıflar listesinde {r['srank']}. sıraya çıktı")
            if not q or q["stale"]:
                decisions.append(f"{t} kapatılmalıydı ama güncel fiyat yok; sonraki çalışmaya bırakıldı.")
                continue
            self.close(p, p["shares"], q["price"], q["at"], f"Günlük karar: {why}.", "momentum")
            decisions.append(f"{t} {'SAT' if p['side'] == 'long' else 'KAPAT'}: {why}.")
            sources.append(src(t))

        # 2) targets (market value)
        targets = {}
        for p in self.P["positions"]:
            t = p["ticker"]
            targets[(t, p["side"])] = (bull_t if t == bull_etf else bear_t if t == bear_etf
                                       else long_t if p["side"] == "long" else short_t)
        n_long = sum(1 for p in self.P["positions"] if p["side"] == "long" and p["ticker"] not in U.LEVERAGED)
        n_short = sum(1 for p in self.P["positions"] if p["side"] == "short")
        held_now = {p["ticker"] for p in self.P["positions"]}
        long_c = [r for r in ranking if r["eligible"] and r["ticker"] not in held_now][:max(0, N_LONG - n_long)]
        short_c = [r for r in shorts if r["ticker"] not in held_now][:max(0, N_SHORT - n_short)] if eligible else []
        for r in long_c:
            targets[(r["ticker"], "long")] = long_t
        for r in short_c:
            targets[(r["ticker"], "short")] = short_t
        if bull_etf and bull_etf not in held_now:
            targets[(bull_etf, "long")] = bull_t
        if bear_etf and bear_etf not in held_now:
            targets[(bear_etf, "long")] = bear_t
        self.quotes_for([t for t, _ in targets])
        no_long = self.M.get("brakeDate") == self.today
        no_short = self.M.get("squeezeDate") == self.today
        if no_long:
            decisions.append("Bugün çöküş freni devrede: yeni long açılmadı.")
        if no_short:
            decisions.append("Bugün sıkışma freni devrede: yeni short açılmadı.")

        # 3) trims first (they free buying power), then top-ups and new positions
        for (t, side), tgt in targets.items():
            p = self._pos(t, side)
            q = self.quotes.get(t)
            if p and q and not q["stale"] and abs(p["marketValue"]) > tgt * 1.5:
                cut = (abs(p["marketValue"]) - tgt) / q["price"]
                self.close(p, cut, q["price"], q["at"],
                           f"Yeniden dengeleme: pozisyon hedef büyüklüğün ({usd(tgt)}) %50'den fazla üstüne çıktı; hedefe "
                           "indirildi.", "dengeleme")
        reason_long = lambda r: (f"Momentum lideri: sıralamada {r['rank']}. (puan {trn(r['score'])}). Getiriler: 1 ay "
                                 f"{trpct(r['r21'])}, 3 ay {trpct(r['r63'])}, 6 ay {trpct(r['r126'])}; fiyat 50 günlük "
                                 f"ortalamanın ({trn(r['sma50'])} $) üstünde. Tez bozulur: zarar-kes veya ilk "
                                 f"{KEEP_RANK_LONG}'dan çıkış.")
        reason_short = lambda r: (f"Düşüş trendi: en zayıflar listesinde {r['srank']}. (puan {trn(r['score'])}). Getiriler: 1 ay "
                                  f"{trpct(r['r21'])}, 3 ay {trpct(r['r63'])}, 6 ay {trpct(r['r126'])}; fiyat 50 günlük "
                                  f"ortalamanın ({trn(r['sma50'])} $) altında. Tez bozulur: fiyat zarar-kes seviyesine "
                                  "çıkarsa veya düşüş trendi biterse.")
        for (t, side), tgt in targets.items():
            q = self.quotes.get(t)
            p = self._pos(t, side)
            if tgt <= 0:
                continue
            if not q or q["stale"]:
                rejected.append(f"{t}: güncel 5 dakikalık fiyat alınamadı, işlem ertelendi.")
                continue
            dirn = -1 if side == "short" or U.LEVERAGED.get(t, (None, 1))[1] < 0 else 1
            if (dirn == 1 and no_long) or (dirn == -1 and no_short):
                continue
            have = abs(p["marketValue"]) if p else 0.0
            if p and have >= tgt * 0.6:
                continue
            if side == "short" and q.get("ssr"):
                rejected.append(f"{t}: SEC Rule 201 (açığa satış devre kesicisi) aktif; bugün short açılamaz.")
                self.comply("SEC Rule 201", f"{t}: açığa satış kısıtı",
                            f"{t} önceki kapanışa göre %10 veya daha fazla düştüğü için (bugün ya da dün) en iyi alış fiyatının "
                            "altında açığa satış yasak; bot bugün bu hisseyi short'lamadı.", "ssr")
                continue
            want = tgt - have
            mv, limit = self._allowed_mv(t, side, want)
            if limit == "min2000":
                rejected.append(f"{t}: özsermaye 2.000 $'ın altında ({usd(self.P['totalValue'])}), açığa satış yapılamaz "
                                "(FINRA 4210).")
                if not min2000_logged:
                    min2000_logged = True
                    self.comply("FINRA 4210(b)(4)", "Açığa satış engellendi: özsermaye 2.000 $ altında",
                                f"Özsermaye {usd(self.P['totalValue'])}. FINRA kuralına göre açığa satış ve kredili alım için "
                                "hesapta en az 2.000 $ özsermaye olmalı; aracı kurum bu emirleri kabul etmez.", "min2000")
                continue
            if limit:
                rate_txt = f"%{trn(init_rate(t, side) * 100, 0)}"
                limit_txt = {
                    "regt": f"Reg T başlangıç teminatı ({rate_txt}) özsermayeyi aşardı",
                    "cash": "özsermaye 2.000 $'ın altında olduğu için kredi kullanılamıyor ve nakit yetmiyor",
                    "econ": "ekonomik kaldıraç 3x'i aşardı",
                }[limit]
                if mv >= MIN_TRADE:
                    self.comply("Reg T" if limit == "regt" else "FINRA 4210(b)(4)" if limit == "cash" else "risk",
                                f"{t}: emir küçültüldü", f"İstenen {usd(want)} yerine {usd(mv)}; daha büyüğü için {limit_txt}. "
                                "Aracı kurum daha büyük emri reddederdi.", "regt" if limit != "cash" else "min2000")
            if mv < MIN_TRADE:
                rejected.append(f"{t}: emir verilemedi, {limit_txt if limit else 'tutar çok küçük'}.")
                continue
            r = by_t.get(t) if side == "long" else short_by_t.get(t)
            if t in U.LEVERAGED:
                und = U.LEVERAGED[t][0]
                ur = next((x for x in idx_rows if x["ticker"] == und), None)
                why = (f"Rejim dilimi: {reg['label']}. {und} momentum puanı {trn(ur['score']) if ur else '—'}; "
                       f"{U.kind_of(t)} ile {usd(mv)} karşılığı {usd(mv * U.lev_of(t))} ekonomik maruziyet. FINRA kaldıraçlı "
                       "ETF kuralı gereği teminatı %75. Zarar-kes maliyetin %15 altı, izleyen %20.")
            else:
                why = (reason_long(r) if side == "long" else reason_short(r)) if r else "Hedef büyüklüğe tamamlama."
            if p:
                why = f"Hedef büyüklüğe tamamlama ({usd(have)} → {usd(tgt)}). " + why
            fn = self.open_long if side == "long" else self.open_short
            if fn(t, mv, q["price"], q["at"], why, "momentum" if t not in U.LEVERAGED else "rejim"):
                verb = ("LONG" if side == "long" else "SHORT") + (" artırıldı" if p else "")
                decisions.append(f"{t} {verb}: {'+' if p else ''}{usd(mv)}"
                                 + (f" (×{U.lev_of(t)} = {usd(mv * U.lev_of(t))} ekonomik maruziyet)" if t in U.LEVERAGED else "")
                                 + (" — hedef büyüklüğe tamamlama." if p else "."))
                sources.append(src(t))

        for r in [x for x in ranking if x["eligible"] and not self._pos(x["ticker"])][:4]:
            watch.append({"ticker": r["ticker"], "note": f"Long adayı: sıra {r['rank']}, puan {trn(r['score'])}."})
        for r in [x for x in shorts if not self._pos(x["ticker"])][:3]:
            watch.append({"ticker": r["ticker"], "note": f"Short adayı: zayıflık sırası {r['srank']}, puan {trn(r['score'])}."})

        self.revalue()
        self.M["lastDailyDate"] = self.today
        P = self.P
        parts = [f"{i['label']} {i['change']}" for i in inds if i["ticker"] in ("SPY", "QQQ", "IWM")]
        others = [f"{i['label']} {i['value']} ({i['change']})" for i in inds if i["ticker"] in ("^VIX", "CL=F", "^TNX")]
        summary = (" · ".join(parts) + ". " + "; ".join(others) + ". " + reg["text"] +
                   f" Sonuç: net değer {usd(P['totalValue'])}, ekonomik kaldıraç {trn(P['leverage'] or 0, 2)}x, Reg T başlangıç "
                   f"teminatı {usd(P['regTInitialReq'])}, bakım teminatı {usd(P['maintenanceReq'])}, kalan alım gücü "
                   f"{usd(P['buyingPower'])}.")
        n_open = sum(1 for t in self.new_trades if t["action"] in ("BUY", "SHORT"))
        n_close = sum(1 for t in self.new_trades if t["action"] in ("SELL", "COVER"))
        tail = f"{reg['label']}, kaldıraç {trn(P['leverage'] or 0, 2)}x"
        headline = (f"{n_open} alım/açığa satış, {n_close} kapanış · {tail}" if (n_open or n_close) else f"İşlem yok · {tail}")
        uniq, seen = [], set()
        for s in sources:
            if s["url"] not in seen:
                seen.add(s["url"])
                uniq.append({**s, "publisher": "Yahoo Finance" if "yahoo" in s["url"] else "GitHub",
                             "usedFor": "Fiyat ve geçmiş veriler" if "yahoo" in s["url"] else "Veri kütüphanesi"})
        self.L["runs"].append({
            "date": self.today, "at": iso(self.now), "by": "bot", "headline": headline, "marketSummary": summary,
            "regime": self.M["regime"],
            "indicators": [{"label": i["label"], "value": i["value"], "change": i["change"]} for i in inds],
            "decisions": decisions, "rejected": rejected, "watchlist": watch, "sources": uniq,
            "ranking": [{k: v for k, v in r.items() if k != "shortOk"} for r in ranking[:15]],
            "shortRanking": [{k: v for k, v in r.items() if k not in ("shortOk", "rank")} for r in shorts[:10]],
            "nextSteps": "Zarar-kes emirleri, kâr alma, frenler ve bakım teminatı piyasa açıkken 5 dakikada bir kontrol "
                         "ediliyor. Bir sonraki rejim ve sıralama kararı yarın 10:00'dan sonraki ilk çalışmada.",
        })

    # ---------------- run
    def run(self):
        held = [p["ticker"] for p in self.P["positions"]]
        self.load_quotes(held + ["SPY"])
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
        kind = "guard"
        if (self.now.time() >= DAILY_AFTER and self.M.get("lastDailyDate") != self.today
                and not self.quotes["SPY"]["stale"]):
            self.daily()
            kind = "daily"
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
                "stopPrice": p["stopPrice"], "distancePct": round(self._dist(p), 2),
            } for p in self.P["positions"]],
            "actions": self.actions,
        }
        if self.notes:
            entry["note"] = "; ".join(self.notes)
        elif not self.actions:
            dist = [e["distancePct"] for e in entry["positions"]]
            entry["note"] = ("Normal: " + (f"pozisyonlar zarar-kese %{trn(min(dist), 1)}–%{trn(max(dist), 1)} uzakta, " if dist else "")
                             + (f"kaldıraç {trn(self.P['leverage'], 2)}x, " if self.P.get("leverage") else "")
                             + (f"SPY {trpct(spy['dayChangePct'])}; " if spy else "") + "işlem gerekmedi.")
        checks = self.L.setdefault("checks", {})
        checks.setdefault(self.today, []).append(entry)
        for d in sorted(checks)[:-CHECK_DAYS_KEPT]:
            del checks[d]
        self.M["lastRunAt"] = iso(self.now)

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
        ledger["meta"]["lastRunAt"] = iso(now)
    save_ledger(ledger)
    if traded:
        Path(".traded").write_text("1")
    print(f"{now:%H:%M} ET | durum={status} | net={ledger['portfolio']['totalValue']} | "
          f"kaldıraç={ledger['portfolio'].get('leverage')} | işlemler={actions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
