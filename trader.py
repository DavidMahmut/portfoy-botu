"""Rule-based paper-trading bot, "x10" account (v7, from 16 Nov 2026): a deliberately high-risk SIMULATION.

It is not a model of a real US brokerage account any more: no US broker gives a small account 10x on stocks. The
account works like a 10 % margin (CFD-style) account so that the owner can study what high leverage does:
  - total exposure may be opened up to 10x the account's value (10 % initial margin on every position),
  - the broker force-closes positions when the account's value falls below 5 % of the open exposure,
  - when the account falls below 300 $ it is written off ("batış"), archived and restarted with a fresh 3.000 $.
Costs stay realistic (IBKR Pro commissions, SEC/FINRA fees, slippage, margin interest on borrowed money, borrow fees
on shorts, SEC Rule 201). Runs every 5 minutes during US market hours (a GitHub Actions job loops until the close):
  1. marks the portfolio to market with Yahoo Finance 5-minute bars, accrues interest and borrow fees,
  2. replays the bars since the last run against each trade's stop, checks the close-out level and the bust level,
  3. once a day after 10:00 New York time - index sleeve: direction from SPY's 200/50-day trend, size = 50 % /
     QQQ's realised 20-day volatility, up to 10x, held as QQQ on margin (long) or QQQ sold short (lev_study.py),
  4. 09:35 - spike short: stocks that closed 30 %+ higher the day before (spike_study.py),
  5. 10:30, 12:00, 13:30, 15:00 - opportunity scan over the whole US market (Yahoo screener: Nasdaq, NYSE, NYSE
     American): big movers are read with catalyst.py (rule-based headline classification, no AI) and traded by a
     small set of rules; every candidate is logged with the reason it was taken or skipped.
Everything is written to ledger.json; a finished account is copied to archive/.

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

from review import ReviewMixin, load_review, save_review

import pandas as pd

import universe as U

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / "ledger.json"
DEPOSITS = ROOT / "deposits.json"
RESTORE = ROOT / "v3_restore.json"
ENGINE = "x10 v7"
ARCHIVE = ROOT / "archive"

# ---- broker model: IBKR Pro (Tiered), US stocks/ETFs
SLIP = 0.0005                            # 0.05 % slippage on every fill
COMM_PER_SHARE, COMM_MIN, COMM_MAX_PCT = 0.0035, 0.35, 0.01
SEC_FEE_RATE = 20.60 / 1_000_000         # SEC Section 31 fee on sales (since 2026-04-04)
TAF_PER_SHARE, TAF_MAX = 0.000195, 9.79  # FINRA trading activity fee on sales (since 2026-01-01)
MARGIN_RATE = 0.0538                     # IBKR USD margin loan rate, < 100k (Sept 2026)
BORROW_RATE = 0.005                      # assumed fee for easy-to-borrow large caps
MIN_TRADE = 10.0

# ---- x10 account (simulation; no real US broker gives a small account this much on stocks)
MAX_GROSS = 10.0                         # total exposure is never opened above 10x the account's value
INIT_MARGIN = 0.10                       # 10 % initial margin on every position (x leverage for leveraged funds)
CLOSEOUT = 0.05                          # forced close-out when the account's value < 5 % of the open exposure
BUST_BELOW = 300.0                       # below this the account is written off and restarted with RESET_CAPITAL
MIN_MARGIN_EQUITY = 0.0                  # (the real-world 2.000 $ minimum of FINRA 4210 is not applied here)
REGT_INIT = INIT_MARGIN
MAINT_LONG, MAINT_SHORT = CLOSEOUT, CLOSEOUT
BUDGET_USE = 0.95
SSR_DROP = 0.10                          # SEC Rule 201 trigger (10 % below prior close) - a market rule, still applied
GROSS_ECON_CAP = MAX_GROSS

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

# ---- strategy v7 "x10" (from 16 Nov 2026, fresh 3.000 $ account). Both books are measured against the whole account.
INDEX_ETF, INDEX_BULL, INDEX_BEAR = "QQQ", "TQQQ", "SQQQ"   # the sleeve holds QQQ on margin; the 3x funds are legacy
INDEX_SLEEVE = (INDEX_ETF, INDEX_BULL, INDEX_BEAR)
INDEX_SHARE, BASKET_SHARE, EVENT_SHARE = 1.0, 0.0, 1.0
DIR_SRC = "SPY"                           # direction from SPY's 200/50-day trend (QQQ's own trend tested worse)
MAX_SHORT_EXPO = 3.0                      # short side at most -3x (lev_study.py: a higher short cap added nothing)
VOL_TARGET, VOL_FLOOR, MAX_EXPO = 0.50, 0.05, 10.0   # exposure = 50 % / realised 20-day vol, at most 10x
VT_BAND, VT_BAND_REL = 0.30, 0.10         # trade only when the exposure is >= 0.3x and >= 10 % of the target off
BASKET_SHORTS = False
BASKET_N, BASKET_KEEP, GROUP_CAP = 10, 20, 3
DISASTER, COOLDOWN_DAYS = 0.25, 30
INVEST_USE = 0.98
CORE_ETF = INDEX_ETF
# ---- opportunity book: whole-market scan (Yahoo screener) + rule-based headline reading (catalyst.py)
OPP_TIMES = (dtime(10, 30), dtime(12, 0), dtime(13, 30), dtime(15, 0))   # a scan runs within 40 minutes after each
OPP_WINDOW_MIN = 40
OPP_MIN_MOVE = 4.0                        # % change today (either way) to be looked at
OPP_MIN_PRICE, OPP_MIN_DV, OPP_MIN_CAP = 5.0, 20.0, 0.3   # price $, average M$ traded a day, market value B$
OPP_LOOK, OPP_NEWS_TOP = 60, 16           # candidates looked at per scan; headlines are read for the top ones
OPP_BOOK_MAX = 6.0                        # all opportunity trades together: at most 6x the account
EVENT_GAP = 0.08                          # "haber devamı": the stock opens at least 8 % above the previous close ...
EVENT_MIN_MOVE = 0.005                    # ... and the first hour adds at least 0.5 % in the same direction
EVENT_HOLD_DAYS = 2                       # exit at the close two trading days after entry
DRIFT_MIN_CAP, DRIFT_MIN_DV = 2.0, 50.0   # B$ market value, M$ traded a day: below these the wide test lost money
EVENT_EXIT_AFTER = dtime(15, 50)
# play -> (risk per trade as a share of the account at the stop, largest position x account, max open trades)
PLAYS = {
    # event_study.py EVENT_WIDE=1 (1.966 stocks, Dec 2023 - Nov 2026), 8 % gap + first hour in the same direction,
    # 2 B$+ and 50 M$+/day: 1.403 trades, +0.97 % a trade, profit factor 1.36, both halves positive. Sizing table of that
    # test: risk 2 %/cap 1x +198 % (worst fall -59 %), 3 %/1x +283 % (-72 %), 3 %/1.5x +295 % (-75 %), 5 %/1.5x +332 %
    # (-90 %): beyond 3 %/1x more risk buys almost no extra growth. The opposite trade (first hour against the gap, the
    # v6 "haber şoku" rule) lost 0.17 % a trade over the same market and is no longer traded.
    "drift": {"label": "Haber devamı", "risk": 0.03, "cap": 1.0, "slots": 6, "hold": 2, "tested": True},
    # no headline archive exists, so this play cannot be back-tested: kept small until its own scorecard says more
    "news": {"label": "Haber takibi", "risk": 0.01, "cap": 0.5, "slots": 4, "hold": 3, "tested": False},
}
# price-move plays: a stock that is up or down a lot at a scan, traded with or against the move (intraday_study.py).
# PLAYS entries with a "move" block are such plays: dir +1 = risers, -1 = fallers; min = % move from the previous close;
# pos = where the price must sit in the day's range (near the high for risers / the low for fallers); slots = scan
# indexes (0 = 10:30, 1 = 12:00, 2 = 13:30, 3 = 15:00); regime "up"/"down" = S&P 500 above / below its 200-day line.
MOVE_STOP_PAD = 0.001
NEWS_MIN_MOVE = 5.0                       # "haber takibi": the stock moved at least 5 % on hard news ...
NEWS_RANGE_POS = 0.60                     # ... and still trades in the top (long) / bottom (short) 40 % of the day's range
NEWS_MIN_SCORE = 4.0                      # catalyst.py score: at least one clear headline match
NEWS_STOP_MIN, NEWS_STOP_MAX = 0.02, 0.08  # stop at the day's other extreme, kept between 2 % and 8 % from entry
SPIKE_MOVE = 0.30                         # sert yükseliş short'u: previous day's close at least 30 % above the day before
SPIKE_MIN_PRICE, SPIKE_MIN_DV = 5.0, 50.0  # real price >= 5 $ the day before, 20-day average >= 50 M $ traded a day
SPIKE_HOLD_DAYS = 10                      # trading days including the entry day; cover at the close of the last one
SPIKE_STOP = 0.50                         # stop 50 % above the entry price
SPIKE_SIZE, SPIKE_SLOTS = 0.10, 6         # each trade 10 % of the account (5 % of the account lost at the stop); at most 6
SPIKE_BORROW = 0.30                       # assumed borrow fee for freshly spiked small stocks (hard to borrow), per year
SPIKE_SLIP = 0.0015                       # wider spread than large caps: 0.15 % per fill
SPIKE_AFTER, SPIKE_LAST_ENTRY = dtime(9, 35), dtime(10, 30)
SPIKE_MIN_CAP = 0.3                       # B$: smaller companies can rarely be borrowed in reality (spike_study.py: the
                                          # 300 M$+ bucket still made +4.9 % a trade in 399 trades, 1 B$+ made +3.9 %)
# ---- rebound rule (v7.1, intraday_study.py part C, QQQ 1999-2026): while the index rule is in cash, a sharply oversold
# Nasdaq 100 (2-day RSI below 5 at the close) is bought 3x at the next open and sold at the open after a close with the
# RSI above 50, or after 3 days. Test (entry and exit at the open): growth 16,5 % -> 31,7 % a year (1999-2026), 28,5 ->
# 36,7 % (2010-2026), 25,2 -> 40,5 % (2018-2026), worst fall unchanged (-77 %); 88 trades, 73 % won, +4,8 % a trade.
# Held as QQQM (the same index, another fund) so it never mixes with the index sleeve's QQQ.
REB_TICKER, REB_LEV = "QQQM", 3.0
REB_ENTRY, REB_EXIT, REB_MAXD = 5.0, 50.0, 3
REB_AFTER, REB_ENTRY_BY = dtime(9, 35), dtime(10, 0)   # trade at the open: entries only in the first half hour
REB_RULE_TEXT = (
    "Tepki kuralı (test edildi): endeks kolu nakitteyken (yön yukarı değilken) Nasdaq 100'ün 2 günlük RSI'ı kapanışta 5'in "
    "altına inerse ertesi sabah açılışta (09:35) hesabın 3 katı kadar Nasdaq 100 fonu alınır (QQQM; endeks kolunun QQQ'su "
    "ile karışmasın diye ayrı fon). RSI kapanışta 50'yi geçince ya da 3 işlem günü dolunca ertesi açılışta satılır; endeks "
    "kolu yeniden işleme girerse hemen satılır. 1999–2026 testinde (giriş ve çıkış açılışta) yıllık büyüme %16,5'ten %31,7'ye "
    "çıktı, en kötü düşüş değişmedi (%−77); 88 işlemin %73'ü kazandı, işlem başına ortalama +%4,8. 2000–2002 çöküşünde bu "
    "dönemler +%28, 2007–2009'da +%31 getirdi (kural tek başına −%60 ve −%35'teydi). Fiyat zarar-kesi yok; risk, 3x "
    "kaldıraçla birkaç günlük düşüşün devamıdır.")
SANCTIONED_NAMES = ("SMIC", "Semiconductor Manufacturing International", "Hikvision", "SenseTime", "Sugon", "Dawning",
                    "China Mobile", "China Telecom", "China Unicom", "Inspur", "DJI", "Megvii", "iFlytek", "Dahua")
MOVERS = ROOT / "movers.json"
SPIKE_TEST_KEY = "fadeUp|r5_dv50|t30|s50|h10"   # spike_results.json candidate that matches these settings
SPIKE_RULE_TEXT = (
    "Sert yükseliş short'u (test edildi): bir Nasdaq hissesi bir günde en az %30 yükselerek kapanırsa ertesi sabah açılışta "
    "(09:35 ET) short açılır. Şartlar: önceki gün fiyatı en az 5 $, son 20 günde günde ortalama en az 50 milyon $ işlem, piyasa "
    "değeri en az 300 milyon $ (daha küçük şirketlerde ödünç hisse gerçekte çoğu zaman bulunmaz), satın alma haberiyle yatay "
    "seyretmiyor, ABD yaptırım listesinde değil, o sabah %10'dan fazla düşmemiş (SEC Rule 201). Büyüklük "
    "hesabın %10'u, aynı anda en fazla 6 işlem; zarar-kes girişin %50 üstü (çalışırsa hesabın %5'i gider); çıkış 10. işlem "
    "gününün kapanışında. Ödünç ücreti yıllık %30 varsayılır. Gerekçe: 2019–2026 testinde bu tür sıçramalar sonraki iki "
    "haftada çoğunlukla geri verildi (300 milyon $ üstü şirketlerde 399 işlem, işlem başına ortalama +%4,9).")
OPP_RULE_TEXT = (
    "Fırsat taraması günde dört kez (10:30, 12:00, 13:30, 15:00 ET) Nasdaq, NYSE ve NYSE American'daki bütün hisselerden o gün "
    "en az %4 hareket edenlere bakar (fiyat ≥ 5 $, günde ≥ 20 milyon $ işlem, piyasa değeri ≥ 300 milyon $). En büyük "
    "hareketlerin haber başlıkları kurallarla sınıflandırılır ve iki kural uygulanır: haber devamı (test edildi) ve haber "
    "takibi (deneysel). Satın alma haberleri, söylentiler, zayıf haberler, habersiz hareketler ve ilk saatte tersine dönen "
    "açılış boşlukları kaydedilir ama işlem açılmaz.")
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

SCHEDULE_TEXT = ("Piyasa açıkken 5 dakikada bir (GitHub Actions döngüsü). Endeks kolu 10:00'dan (New York) sonraki ilk "
                 "çalışmada; sert yükseliş short'u 09:35'te; fırsat taraması 10:30, 12:00, 13:30 ve 15:00'te.")
CONFIG_TEXT = {
    "strategyName": "x10 simülasyonu: 10x'e kadar endeks kolu + tüm piyasada fırsat taraması (v7, yeni 3.000 $ hesap)",
    "strategySummary": (
        "Bu hesap gerçek bir aracı kurum hesabının kopyası değil, yüksek kaldıracın nasıl davrandığını izlemek için kurulmuş "
        "bir simülasyondur: toplam maruziyet hesabın 10 katına kadar açılabilir (ABD'de küçük bir hesaba hissede bu kadar "
        "kaldıraç verilmez). Endeks kolu Nasdaq 100 fonu QQQ'yu S&P 500 trendinin yönünde, krediyle tutar; büyüklük = %50 ÷ "
        "QQQ'nun 20 günlük oynaklığı (oynaklık %5 iken 10x, %25 iken 2x, %50 iken 1x). Fırsat taraması günde dört kez Nasdaq, "
        "NYSE ve NYSE American'daki bütün hisselerin en çok hareket edenlerine bakar, haber başlıklarını kurallarla "
        "sınıflandırır (soruşturma, dolandırıcılık iddiası, açığa satış raporu, beklenti değişikliği, FDA kararı, satın alma, "
        "söylenti) ve üç kuralla işlem açar: haber devamı, haber takibi (deneysel) ve sert yükseliş short'u. Hesap 300 $'ın "
        "altına inerse batmış sayılır, arşivlenir ve yeni 3.000 $ ile yeniden başlar. 1999–2026 kaldıraç testinde oynaklık "
        "hedefi %50'nin üstüne çıkınca uzun vadeli büyüme düştü; sürekli 10x tutmak aynı dönemde hesabı 12 kez batırdı. "
        "Yapay zekâ kullanılmaz; bütün kararlar aşağıdaki sabit kurallardan çıkar."),
    "execution": (
        "Yahoo Finance verileri (yfinance): yön ve oynaklık bir önceki günün kapanışıyla, işlemler 5 dakikalık güncel fiyatla. "
        "Kesirli hisse. Her emirde IBKR Pro Tiered komisyonu: hisse başına 0,0035 $, en az 0,35 $, en fazla işlem tutarının "
        "%1'i. Satışlarda SEC ücreti (milyon $'da 20,60 $) ve FINRA TAF. Fiyat kayması %0,05 (küçük şirketlerde %0,15). "
        "Hesabın kendi parasını aşan alımlar krediyle yapılır: kredi borcuna yıllık %5,38 faiz işler. Short'ta ödünç ücreti: "
        "büyük şirket %0,5, orta %1, küçük %5, sert yükseliş short'u %30 (yıllık). Temettüler hesaba geçer."),
    "broker": "Simülasyon: %10 teminatlı, 10x'e kadar kaldıraçlı hesap (masraflar IBKR Pro'ya göre)",
    "rules": [
        "Hesap modeli (simülasyon): her pozisyon için %10 başlangıç teminatı; toplam maruziyet hesabın 10 katını aşacak emir "
        "açılmaz. Hesap değeri açık maruziyetin %5'inin altına inerse zorunlu tasfiye çalışır: en çok zararda olan pozisyondan "
        "başlanarak maruziyet 10x'in altına inene kadar kapatılır. Hesap değeri 300 $'ın altına inerse hesap batmış sayılır: "
        "her şey kapatılır, hesap arşivlenir ve ertesi işlem günü yeni 3.000 $ ile yeniden başlar.",
        "Yön kararı (her gün 10:00'dan sonra, dünkü kapanışla): S&P 500 (SPY) 200 günlük ortalamasının üstündeyse yön YUKARI "
        "(long). Altındaysa ve 50 günlük ortalaması da 200 günlüğün altına inmişse yön AŞAĞI (short). Arada kalırsa kararsız: "
        "endeks kolu nakitte bekler.",
        "Endeks kolu: hedef maruziyet = %50 ÷ QQQ'nun son 20 gündeki yıllık oynaklığı, long'da en fazla 10x, short'ta en fazla "
        "3x; hesabın tamamı üzerinden ölçülür. Long'da QQQ krediyle alınır, short'ta QQQ açığa satılır. Hedefle mevcut "
        "maruziyet arasındaki fark 0,3x'ten (ve hedefin %10'undan) küçükse işlem yapılmaz. 1999–2026 testi: %25 hedefle yıllık "
        "%13,7 ve en kötü düşüş %−46; %50 hedefle %16,1 ve %−81; %100 hedefle %5,6 ve %−99. Yani %50, büyümenin en yüksek "
        "olduğu risk düzeyi; ötesi kârı azaltıyor.",
        "Nasdaq düşüş kuralı: Nasdaq 100 (QQQ) zirvesinden %20 veya daha fazla düşmüşse endeks kolu short açmaz, yeni zirveye "
        "kadar nakitte bekler (yön yukarıysa long kuralı aynen çalışır).",
        REB_RULE_TEXT,
        OPP_RULE_TEXT,
        "Haber okuma (kural tabanlı, yapay zekâ yok): başlıklardaki kalıplar şu türlere ayrılır. Sert ve olumsuz: dolandırıcılık "
        "ya da muhasebe iddiası, soruşturma ve dava, açığa satış raporu, beklenti düşürme ya da kötü bilanço, başarısız klinik "
        "deneme, finansal sıkıntı ve sulandırma. Sert ve olumlu: beklenti yükseltme ya da güçlü bilanço, FDA onayı, büyük "
        "sözleşme. Zayıf: analist notu, yönetici değişikliği, geri alım. Söylenti: doğrulanmamış haberler ve kalabalık "
        "hareketleri. Satın alma: fiyat teklife sabitlenir. Avukat bürolarının 'yatırımcı uyarısı' duyuruları sayılmaz.",
        "Haber devamı (test edildi; yalnızca 10:30 taramasında): önceki kapanışın en az %8 üstünde açılan ve ilk saatte en az "
        "%0,5 daha yükselen hisse alınır (long). Şartlar: piyasa değeri en az 2 milyar $, günde en az 50 milyon $ işlem. "
        "Zarar-kes ilk saatin en düşüğünün hemen altı; çıkış 2 işlem günü sonra kapanışta. Büyüklük: zarar-kes çalışırsa "
        "hesabın %3'ü gidecek kadar, tek işlem en fazla hesabın 1 katı; aynı anda en fazla 6 işlem. Gerekçe: 1.966 hissede "
        "Aralık 2023–Kasım 2026 testinde bu durumda 1.403 işlem ortalama +%0,97 kazandırdı (kazanç/kayıp oranı 1,36; dönemin "
        "iki yarısı da artıda). Tersi, yani ilk saatte boşluğun aksine dönen hisseye girmek (eski haber şoku kuralı) aynı "
        "testte işlem başına %0,17 kaybettirdi ve artık uygulanmıyor. Düşüşle açılıp düşmeye devam edenlerde short "
        "açılmaz: çoğunda SEC Rule 201 devreye girer, kalan 34 işlem de zarar etti.",
        "Haber takibi (deneysel; geçmiş haber arşivi olmadığı için test edilemedi): hisse sert bir şirket haberiyle en az %5 "
        "hareket etmişse ve fiyat hâlâ günün aralığının haber yönündeki ucundaysa (piyasa haberi geri almıyorsa) haber yönünde "
        "girilir: olumsuz haberde short, olumluda long. Zarar-kes günün öbür ucu (girişe %2–8 uzaklıkta); çıkış 3 işlem günü "
        "sonra kapanışta. Büyüklük: zarar-keste hesabın %1'i gidecek kadar, en fazla hesabın yarısı; en fazla 4 işlem "
        "(sınanamadığı için küçük tutulur; kendi karnesi oluştukça yeniden değerlendirilir).",
        SPIKE_RULE_TEXT,
        "Sınırlar: fırsat işlemlerinin toplamı hesabın en fazla 6 katı; endeks koluyla birlikte hepsi en fazla 10 katı. SEC "
        "Rule 201 gereği önceki kapanışa göre %10'dan fazla düşmüş hissede short açılmaz. ABD yaptırım listesindeki "
        "şirketlerde hiçbir zaman işlem yapılmaz. Aynı gün zarar-kesle kapanan hisseye o gün yeniden girilmez.",
        "Vergi (öğrenme amaçlı, basitleştirilmiş): 1 yıl veya daha kısa tutulan pozisyonların kazancı kısa vadelidir ve normal "
        "gelir gibi vergilenir. Zararla satılan hisse 30 gün içinde yeniden alınırsa (wash sale) zarar o yıl düşülemez. Vergi "
        "tavsiyesi değildir.",
        "Veri 35 dakikadan eskiyse o varlıkta işlem yapılmaz. Her işlem fiyatı, saati, kaynağı, komisyonu, ücretleri ve "
        "tetikleyen kuralla kaydedilir; her taramada bakılan hisseler, okunan haber ve alınmama nedeni de kaydedilir.",
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
    """Initial margin rate of the x10 account: 10 % of the exposure (a 3x fund counts three times)."""
    return min(1.0, INIT_MARGIN * U.lev_of(t))


def maint_req(t, side, shares, price):
    """Close-out level: 5 % of the exposure."""
    return shares * price * min(1.0, CLOSEOUT * U.lev_of(t))


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
class Engine(ReviewMixin):
    def __init__(self, ledger, data, now, deposits=None, restore=None, review=None):
        self.R = review if review is not None else {}
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
        self.names = {}                      # ticker -> company name for stocks outside universe.py (whole-market scan)
        self.migrate()
        self.C["schedule"] = SCHEDULE_TEXT
        self.C.update(CONFIG_TEXT)
        self.C["maxLeverage"] = MAX_EXPO
        self.C["dipRule"] = DIP_RULE_TEXT
        self.C["oppRule"] = OPP_RULE_TEXT
        self.C["reboundRule"] = REB_RULE_TEXT
        self.C["spikeRule"] = {"text": SPIKE_RULE_TEXT, "testKey": SPIKE_TEST_KEY, "testPortfolio": "f10_s6",
                               "move": SPIKE_MOVE, "minPrice": SPIKE_MIN_PRICE, "minDollarVolM": SPIKE_MIN_DV,
                               "stop": SPIKE_STOP, "holdDays": SPIKE_HOLD_DAYS, "size": SPIKE_SIZE, "slots": SPIKE_SLOTS,
                               "borrow": SPIKE_BORROW}
        # the x10 account is a simulation: Reg T and FINRA margin rules are not modelled, market rules and costs are
        self.C["legalSources"] = [LEGAL[k] for k in ("ssr", "secfee", "ibkr_comm", "ibkr_margin", "ibkr_frac", "irs409", "irs550")]
        self.C["notApplied"] = ("Bu hesap bir simülasyondur: gerçek ABD kredili hesap kuralları (Reg T %50 başlangıç teminatı, "
                                "FINRA 4210 bakım teminatı ve 2.000 $ asgari özsermaye, kaldıraçlı ETF teminatı) uygulanmaz. "
                                "Piyasa kuralları (SEC Rule 201), ABD yaptırım listesi ve gerçek masraflar uygulanır.")
        self.C["brokerModel"] = {
            "name": "Simülasyon: 10x kaldıraçlı hesap (gerçek ABD aracı kurumlarında böyle bir hesap yok)",
            "commission": "0,0035 $/hisse, en az 0,35 $, en fazla %1",
            "marginRate": MARGIN_RATE, "borrowRate": BORROW_RATE, "minMarginEquity": MIN_MARGIN_EQUITY,
            "regTInitial": REGT_INIT, "maintLong": MAINT_LONG, "maintShort": MAINT_SHORT,
            "maxGross": MAX_GROSS, "initMargin": INIT_MARGIN, "closeOut": CLOSEOUT, "bustBelow": BUST_BELOW}
        self.C["x10"] = {"maxGross": MAX_GROSS, "initMargin": INIT_MARGIN, "closeOut": CLOSEOUT, "bustBelow": BUST_BELOW,
                         "volTarget": VOL_TARGET, "maxShort": MAX_SHORT_EXPO, "oppBookMax": OPP_BOOK_MAX,
                         "plays": PLAYS, "oppTimes": [f"{t:%H:%M}" for t in OPP_TIMES],
                         "spike": {"size": SPIKE_SIZE, "slots": SPIKE_SLOTS, "stop": SPIKE_STOP, "holdDays": SPIKE_HOLD_DAYS}}

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
        if eng in ("trend-vol v5", "trend-event v6"):
            self._to_v7(eng)
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

    def _to_v7(self, eng):
        """Fresh 3.000 $ account for the x10 simulation; the previous account ends here and is archived."""
        sc = self.L.get("scorecard")
        label = {"trend-vol v5": "v5: iki yönlü endeks + momentum sepeti",
                 "trend-event v6": "v6: endeks kolu + haber şoku kolu (gerçek hesap kuralları)"}[eng]
        prev = self._fresh_account("v7", label if sc and sc.get("start") else None)
        self.comply("yeni hesap", "Hesap 3.000 $ ile sıfırdan başladı (x10, v7)",
                    f"Önceki hesap {prev['start']} – {prev['end']}: {usd(prev['invested'] or 0)} yatırıldı, son değer "
                    f"{usd(prev['endValue'] or 0)} (zaman ağırlıklı %{trn(prev['twrPct'] or 0)}; SPY %{trn(prev['spyTwrPct'] or 0)}). "
                    "Yeni hesap bir simülasyondur ve gerçek bir ABD hesabının kurallarıyla sınırlı değildir: toplam maruziyet "
                    f"hesabın {trn(MAX_GROSS, 0)} katına kadar açılabilir (%{trn(INIT_MARGIN * 100, 0)} başlangıç teminatı), hesap "
                    f"değeri açık maruziyetin %{trn(CLOSEOUT * 100, 0)}'inin altına inerse pozisyonlar zorla kapatılır, "
                    f"{usd(BUST_BELOW)} altına inerse hesap batmış sayılır ve yeni {usd(RESET_CAPITAL)} ile yeniden başlar.", None)
        self.notes.append("v7: yeni x10 hesabı")

    def _fresh_account(self, strategy, eval_label=None, bust=None):
        """Replace the ledger with a new RESET_CAPITAL account. The old account is summarised in `previousAccounts`
        and its full ledger is written to archive/ (the workflow commits it). Returns the summary."""
        L, P, C, M = self.L, self.P, self.C, self.M
        sc = L.get("scorecard")
        hist = list(L.get("evalHistory", []))
        if sc and sc.get("start") and (eval_label or bust):
            hist.append({"strategy": eval_label or f"x10 (v7), {bust['n']}. batış", "start": sc.get("start"), "end": self.today,
                         "returnPct": sc.get("returnPct"), "spyReturnPct": sc.get("spyReturnPct"),
                         "excessPct": sc.get("excessPct"), "maxDrawdownPct": sc.get("maxDrawdownPct"),
                         "trades": sc.get("trades"), "daysElapsed": sc.get("daysElapsed")})
        n_prev = len(L.get("previousAccounts", []))
        name = f"ledger_{self.today}" + (f"_batis{bust['n']}" if bust else "") + ".json"
        costs = P.get("costs", {})
        prev = {"name": f"Hesap {n_prev + 1}", "start": C.get("startDate"), "end": self.today,
                "engine": M.get("engine"), "bust": bool(bust),
                "invested": P.get("netDeposits"), "endValue": P.get("totalValue"), "twrPct": P.get("twrPct"),
                "spyTwrPct": P.get("benchmarkTwrPct"), "realizedPL": P.get("realizedPL"),
                "trades": sum(1 for t in L.get("trades", []) if t.get("action") in ("BUY", "SELL", "SHORT", "COVER")
                              and not t.get("cancelled")),
                "costs": round(sum(num(v) for k, v in costs.items() if k != "dividendsReceived"), 2),
                "positionsAtEnd": [{"ticker": p["ticker"], "side": p["side"], "value": p.get("marketValue"),
                                    "unrealizedPct": p.get("unrealizedPct")} for p in P.get("positions", [])],
                "archive": f"archive/{name}"}
        try:                                   # the full old ledger, for later study
            ARCHIVE.mkdir(parents=True, exist_ok=True)
            (ARCHIVE / name).write_text(json.dumps(L, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            prev["archive"] = None
            self.notes.append(f"Arşiv dosyası yazılamadı: {type(e).__name__}")
        d0 = date.fromisoformat(self.today)
        m = d0.month + 3
        end = date(d0.year + (m - 1) // 12, (m - 1) % 12 + 1, min(d0.day, 28))
        newC = {k: v for k, v in C.items() if k not in ("deposits", "benchmark")}
        newC.update(startCapital=RESET_CAPITAL, startDate=self.today,
                    benchmark={"ticker": "SPY", "startPrice": None, "startAt": None, "shares": 0.0})
        newM = {"engine": ENGINE, "units": RESET_CAPITAL, "appliedDeposits": list(M.get("appliedDeposits", [])),
                "lastAccrualDate": self.today, "cooldown": {}, "nasdaq": M.get("nasdaq") or {}, "lastDailyDate": None,
                "lastRebalance": None, "trendUp": None,
                "evaluation": {"start": self.today, "end": end.isoformat(), "startTs": iso(self.now), "strategy": strategy}}
        newP = {"cash": RESET_CAPITAL, "positions": [], "realizedPL": 0.0, "netDeposits": RESET_CAPITAL,
                "costs": {"slippage": 0.0, "commission": 0.0, "fees": 0.0, "interest": 0.0, "borrow": 0.0,
                          "dividendsPaid": 0.0, "dividendsReceived": 0.0}}
        keep = {"evalHistory": hist, "previousAccounts": list(L.get("previousAccounts", [])) + [prev],
                "busts": list(L.get("busts", [])) + ([bust] if bust else []),
                "playStats": L.get("playStats", {}) if bust else {}}
        L.clear()
        L.update({"config": newC, "portfolio": newP, "meta": newM, "trades": [], "snapshots": [], "checks": {},
                  "runs": [], "compliance": [], "tax": {}, **keep})
        self.P, self.C, self.M = L["portfolio"], L["config"], L["meta"]
        return prev

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

    def _name(self, t):
        return self.names.get(t) or U.name_of(t)

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
            "ticker": t, "name": self._name(t), "kind": U.kind_of(t), "shares": shares,
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
            "ticker": t, "name": self._name(t), "kind": U.kind_of(t), "side": side, "lev": lev,
            "shares": shares, "avgCost": ex, "costBasis": value, "lastPrice": quote, "lastPriceAt": iso(quote_at),
            "priceSource": {"title": "Yahoo Finance", "url": U.quote_url(t)},
            "marketValue": round(shares * quote * (1 if side == "long" else -1), 2), "exposure": round(shares * quote * lev, 2),
            "unrealized": 0.0, "unrealizedPct": 0.0, "openedAt": self.today, "openedTs": iso(quote_at),
            "lastAddDate": self.today, "stopPrice": stop, "extremeSinceEntry": quote, "tookProfit": False, "thesis": reason,
        }

    # ---------------- margin checks (x10 account)
    def _init_req(self):
        return sum(init_rate(p["ticker"], p["side"]) * p["shares"] * p["lastPrice"] for p in self.P["positions"])

    def _allowed_mv(self, t, side, want_mv):
        """Largest new market value the simulated broker accepts for this order: 10 % initial margin on everything
        and never more than MAX_GROSS x the account in total. Returns (amount, None) or (smaller amount, reason)."""
        self.revalue()
        eq = self.P["totalValue"]
        if eq <= 0:
            return 0.0, "regt"
        rooms = {"regt": (eq * 0.99 - self._init_req()) / init_rate(t, side),
                 "econ": (GROSS_ECON_CAP * eq * 0.99 - self.P["grossExposure"]) / U.lev_of(t)}
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
        play = p.get("play") or ("index" if p["ticker"] in INDEX_SLEEVE else "other")
        extra["play"] = play
        st = self.L.setdefault("playStats", {}).setdefault(play, {"closed": 0, "wins": 0, "pl": 0.0, "best": None, "worst": None})
        st["closed"] += 1
        st["wins"] += 1 if realized > 0 else 0
        st["pl"] = round(st["pl"] + realized, 2)
        st["best"] = realized if st["best"] is None else max(st["best"], realized)
        st["worst"] = realized if st["worst"] is None else min(st["worst"], realized)
        st["last"] = self.today
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
            buyingPower=round(max(0.0, min(excess / INIT_MARGIN, MAX_GROSS * equity - gross)), 2),
            marginEligible=equity > 0,
            # how far the whole book may move against the account before the forced close-out (5 % of exposure)
            closeoutMovePct=round((equity - CLOSEOUT * gross) / gross * 100, 2) if gross > 0 else None,
            bustCount=len(self.L.get("busts", [])),
            paidInTotal=round(RESET_CAPITAL * (len(self.L.get("busts", [])) + 1), 2),
            lostToBusts=round(sum(num(b.get("lost")) for b in self.L.get("busts", [])), 2),
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
                        (d.get("note") or "Sermaye eklendi") + f". Yeni hesap değeri {usd(eq)}.", None)

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
                        (f"{p.get('playLabel') or 'Fırsat işlemi'} zarar-kesi tetiklendi: ") if event else
                        (f"Felaket zarar-kesi (maliyetin %25 {'altı' if p['side'] == 'long' else 'üstü'}) tetiklendi: "
                         if fixed else "Bekleyen zarar-kes emri tetiklendi: "))
                reason = (head
                          + f"{hm(ts)} ET mumunda fiyat {trn(extreme)} $'a {word}, zarar-kes seviyesi {trn(eff)} $. "
                          + (f"Mum seviyenin ötesinde açıldığı için açılış fiyatından ({trn(fill)} $) kapatıldı."
                             if fill != eff else f"Emir {trn(fill)} $'dan gerçekleşti."))
                self.close(p, p["shares"], fill, ts,
                           reason + (f" {COOLDOWN_DAYS} gün yeniden alınmayacak." if fixed and not event else ""),
                           "sert yükseliş zarar-kesi" if p.get("spike") else
                           f"{(p.get('playLabel') or 'Fırsat işlemi').lower()} zarar-kesi" if event else
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
        """Forced close-out of the x10 account: when the account's value is below 5 % of the open exposure the broker
        closes positions (largest loss first) until the exposure is back inside the 10x limit."""
        self.revalue()
        eq = self.P["totalValue"]
        if eq <= 0:
            for p in list(self.P["positions"]):
                q = self.quotes.get(p["ticker"], {"price": p["lastPrice"], "at": pd.Timestamp(self.now)})
                self.close(p, p["shares"], q["price"], q["at"], "Hesap değeri sıfırın altına indi; tüm pozisyonlar "
                           "kapatıldı.", "zorunlu tasfiye")
            self.status = "alarm"
            return
        if self.P["totalValue"] >= self.P["maintenanceReq"]:
            return
        self.status = "alarm"
        why = (f"Zorunlu tasfiye: hesap değeri {usd(self.P['totalValue'])}, açık maruziyet {usd(self.P['grossExposure'])} "
               f"({trn(self.P['leverage'] or 0, 1)}x). Değer maruziyetin %{trn(CLOSEOUT * 100, 0)}'inin "
               f"({usd(self.P['maintenanceReq'])}) altına indiği için aracı kurum beklemeden pozisyon kapatır.")
        self.comply("zorunlu tasfiye", "Zorunlu tasfiye (close-out)", why, None)
        self.M["closeouts"] = self.M.get("closeouts", 0) + 1
        for p in sorted(list(self.P["positions"]), key=lambda p: p["unrealized"]):
            self.revalue()
            if (self.P["leverage"] or 0) <= MAX_GROSS:
                break
            q = self.quotes.get(p["ticker"])
            if not q or q["stale"]:
                continue
            self.close(p, p["shares"], q["price"], q["at"], why + " En çok zararda olan pozisyondan başlanarak kapatılıyor.",
                       "zorunlu tasfiye")

    def bust_check(self):
        """The account is written off when its value falls below BUST_BELOW: everything is closed, the account is
        archived and a fresh RESET_CAPITAL account starts (it trades again from the next trading day)."""
        self.revalue()
        eq = self.P["totalValue"]
        if eq >= BUST_BELOW:
            return False
        losers = sorted(self.P["positions"], key=lambda p: p["unrealized"])
        held = ", ".join(f"{p['ticker']} {'long' if p['side'] == 'long' else 'short'} {usd(p['unrealized'])}" for p in losers[:4])
        for p in list(self.P["positions"]):
            q = self.quotes.get(p["ticker"], {"price": p["lastPrice"], "at": pd.Timestamp(self.now)})
            self.close(p, p["shares"], q["price"], q["at"],
                       f"Hesap battı: değer {usd(eq)} ile {usd(BUST_BELOW)} sınırının altına indi; kalan pozisyonlar kapatıldı.",
                       "batış")
        self.revalue()
        closed = [t for t in self.L["trades"] if t.get("action") in ("SELL", "COVER") and not t.get("cancelled")]
        worst = sorted(closed, key=lambda t: num(t.get("realizedPL")))[:3]
        peak = max([num(sn.get("totalValue")) for sn in self.L.get("snapshots", [])] + [RESET_CAPITAL])
        n = len(self.L.get("busts", [])) + 1
        start = self.C.get("startDate") or self.today
        bust = {"n": n, "date": self.today, "at": iso(self.now), "start": start,
                "days": len(trading_days(start, self.today)), "startCapital": RESET_CAPITAL,
                "endValue": self.P["totalValue"], "lost": round(RESET_CAPITAL - self.P["totalValue"], 2),
                "peakValue": round(peak, 2), "trades": len(closed), "closeouts": self.M.get("closeouts", 0),
                "leverageBefore": self.L["snapshots"][-1].get("leverage") if self.L.get("snapshots") else None,
                "openAtBust": held or None,
                "worstTrades": [{"ticker": t["ticker"], "rule": t.get("rule"), "pl": t.get("realizedPL"), "date": t.get("date")}
                                for t in worst],
                "text": (f"{n}. batış: {start} tarihinde {usd(RESET_CAPITAL)} ile açılan hesap {self.today} günü {usd(eq)} "
                         f"değerine düştü ({usd(BUST_BELOW)} sınırının altı). "
                         + (f"O anda açık pozisyonlar: {held}. " if held else "")
                         + ("En çok kaybettiren işlemler: "
                            + ", ".join(f"{t['ticker']} ({t.get('rule')}, {usd(num(t.get('realizedPL')))})" for t in worst) + ". "
                            if worst else "")
                         + f"Hesap arşivlendi; yeni {usd(RESET_CAPITAL)} ile yeniden başladı.")}
        self._fresh_account("v7", bust=bust)
        self.M["pausedOn"] = self.today          # no new trades on the day of the bust
        self.comply("batış", f"Hesap battı ({n}. kez) ve yeniden başladı", bust["text"], None)
        self.notes.append(bust["text"])
        self.status = "alarm"
        return True

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
            tail = ""
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

    def index_state(self):
        """Account value and the index sleeve's present exposure as a signed multiple of it."""
        self.revalue()
        eq = max(0.0, self.P["totalValue"])
        exp = 0.0
        for p in self.P["positions"]:
            if p["ticker"] in INDEX_SLEEVE:
                exp += abs(p["marketValue"]) * p.get("lev", 1) * direction(p)
        return eq, (exp / eq if eq > 0 else 0.0)

    def _index_reason(self, side, sig, have, want, eq):
        return (f"Endeks kolu, {INDEX_ETF} {('long (krediyle)' if want > eq else 'long') if side == 'long' else 'short'}: "
                f"{usd(have)} → {usd(want)} "
                f"(hesabın {trn(want / eq if eq else 0, 2)} katı). {sig['text']}")

    def index_rebalance(self, sig, decisions, rejected, sources):
        """Bring the sleeve to the target exposure with QQQ: bought on margin when the target is long, sold short when
        it is short. No trade while the gap to the target is inside the band."""
        eq, now = self.index_state()
        target = sig["target"]
        self.M["index"] = {**{k: sig[k] for k in ("dir", "word", "vol", "magnitude", "target", "src")},
                           "now": round(now, 2), "date": self.today}
        idx_pos = [p for p in self.P["positions"] if p["ticker"] in INDEX_SLEEVE]
        band = max(VT_BAND, VT_BAND_REL * abs(target))
        if abs(target - now) < band and (target == 0) == (not idx_pos) and target * now >= 0:
            decisions.append(f"Endeks kolu: hedef {trn(target, 2)}x, mevcut {trn(now, 2)}x; fark {trn(band, 2)}x'ten "
                             "küçük olduğu için işlem yapılmadı.")
            return False
        decisions.append(f"Endeks kolu: maruziyet {trn(now, 2)}x → {trn(target, 2)}x.")
        self.quotes_for(list({p["ticker"] for p in idx_pos} | {INDEX_ETF}))
        q = self.quotes.get(INDEX_ETF)
        if not q or q["stale"]:
            rejected.append(f"{INDEX_ETF}: güncel fiyat yok, endeks kolu bugün değiştirilemedi.")
            return False
        side = "long" if target > 0 else "short"
        for p in idx_pos:                      # the wrong side (and any old 3x fund) goes first
            if target != 0 and p["ticker"] == INDEX_ETF and p["side"] == side:
                continue
            pq = self.quotes.get(p["ticker"])
            if not pq or pq["stale"]:
                rejected.append(f"{p['ticker']}: güncel fiyat yok, kapatılamadı.")
                continue
            have = abs(p["marketValue"])
            if self.close(p, p["shares"], pq["price"], pq["at"], self._index_reason(p["side"], sig, have, 0.0, eq), "endeks kolu"):
                decisions.append(f"{p['ticker']} {'SAT' if p['side'] == 'long' else 'KAPAT (short)'}: {usd(have)} → {usd(0)}.")
                sources.append(src(p["ticker"]))
        if target == 0:
            return True
        eq, _ = self.index_state()
        want = abs(target) * eq
        p = self._pos(INDEX_ETF, side)
        have = abs(p["marketValue"]) if p else 0.0
        if want - have >= MIN_TRADE:
            amount, limit = self._allowed_mv(INDEX_ETF, side, want - have)
            if amount < MIN_TRADE:
                rejected.append(f"{INDEX_ETF}: endeks kolu işlemi yapılamadı (teminat ya da {trn(MAX_GROSS, 0)}x toplam sınırı dolu).")
                return True
            if limit:
                self.comply("teminat", f"{INDEX_ETF}: emir küçültüldü",
                            f"İstenen {usd(want - have)} yerine {usd(amount)}; daha büyüğü %{trn(INIT_MARGIN * 100, 0)} başlangıç "
                            f"teminatını ya da {trn(MAX_GROSS, 0)}x toplam maruziyet sınırını aşardı.", None)
            reason = self._index_reason(side, sig, have, have + amount, eq)
            tr = (self.open_long(INDEX_ETF, amount, q["price"], q["at"], reason, "endeks kolu") if side == "long" else
                  self.open_short(INDEX_ETF, amount, q["price"], q["at"], reason, "endeks kolu"))
            if tr:
                debit = max(0.0, -self.P["cash"])
                decisions.append(f"{INDEX_ETF} {'AL' if side == 'long' else 'SHORT'}: {usd(amount)}"
                                 + (f" (kredi borcu {usd(debit)}, yıllık %{trn(MARGIN_RATE * 100, 2)} faiz)" if side == "long" and debit > 1 else "")
                                 + ".")
                sources.append(src(INDEX_ETF))
        elif have - want >= MIN_TRADE and p:
            if self.close(p, (have - want) / q["price"], q["price"], q["at"], self._index_reason(side, sig, have, want, eq),
                          "endeks kolu"):
                decisions.append(f"{INDEX_ETF} AZALT: {usd(have)} → {usd(want)}.")
                sources.append(src(INDEX_ETF))
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
        try:
            self.shadow_fill()
            decisions.extend(self.play_health())
        except Exception as e:                     # evaluation must never block the daily decision
            self.notes.append(f"Kural değerlendirmesi yapılamadı: {type(e).__name__}: {e}")
        sources = [YF_SOURCE] + [src(t) for t, _ in U.INDICATORS] + [src(INDEX_ETF)]
        basket_word = "long" if trend["up"] else ("short" if BASKET_SHORTS else "nakit")
        idx_word = (f"endeks {'+' if sig['target'] > 0 else '−' if sig['target'] < 0 else ''}{trn(abs(sig['target']), 2)}x"
                    if sig else "endeks verisi yok")
        if (self.M.get("rebound") or {}).get("text"):
            decisions.append(self.M["rebound"]["text"])
        self.M["regime"] = {"name": "up" if sig and sig["dir"] > 0 else "down" if sig and sig["dir"] < 0 else "neutral",
                            "label": f"Yön {sig['wordLow'] if sig else '—'}: {idx_word}, "
                                     + (f"sepet {basket_word}" if BASKET_SHARE > 0 else "fırsat taraması açık"),
                            "budget": 1.0}
        if sig:
            self.index_rebalance(sig, decisions, rejected, sources)
        if BASKET_SHARE <= 0:
            due = False
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
            "nextSteps": "Endeks kolunun yönü ve büyüklüğü her gün 10:00'dan sonra; tüm piyasada fırsat taraması 10:30, "
                         "12:00, 13:30 ve 15:00'te; zarar-kesler ve tasfiye seviyesi piyasa açıkken 5 dakikada bir "
                         "kontrol ediliyor.",
        })

    # ---------------- news-shock sleeve (v6)
    def _trading_day_after(self, d, n):
        days = trading_days(d, (date.fromisoformat(d) + timedelta(days=n * 2 + 10)).isoformat())
        return days[min(n, len(days) - 1)]

    def _headline(self, t):
        """Latest Yahoo Finance headline for the ticker (best effort; the trade never depends on it)."""
        try:
            import news as N
            items = N.fetch_yf(t) or N.fetch_rss(t)
        except Exception:
            items = []
        at = lambda n: n.get("at").isoformat() if hasattr(n.get("at"), "isoformat") else str(n.get("at") or "")
        cutoff = (date.fromisoformat(self.today) - timedelta(days=3)).isoformat()
        fresh = [n for n in items if at(n)[:10] >= cutoff]
        n = (fresh or items or [None])[0]
        return {"title": n["title"], "url": n["url"], "publisher": n.get("publisher"), "at": at(n) or None} if n else None

    def _news_items(self, t):
        try:
            import news as N
            return N.fetch_yf(t) or N.fetch_rss(t)
        except Exception:
            return []

    def _movers(self):
        """Today's big movers in the whole US market: Yahoo's screener (live), or the hourly movers.json scan when the
        screener gives nothing. Returns (rows, source text)."""
        rows, source = [], None
        try:
            rows = self.data.screen_movers(OPP_MIN_MOVE, OPP_MIN_PRICE) or []
            source = "Yahoo tarayıcısı: Nasdaq, NYSE ve NYSE American'daki tüm hisseler (anlık)"
        except Exception as e:
            self.notes.append(f"Yahoo tarayıcısı çalışmadı ({type(e).__name__}); saatlik tarama denendi")
        if not rows:
            try:
                mv = json.loads(MOVERS.read_text(encoding="utf-8"))
                at = datetime.fromisoformat(mv["asOf"])
                if mv.get("barDay") == self.today and (self.now - at).total_seconds() <= 90 * 60:
                    cols = mv["screen"]["cols"]
                    for r in mv["screen"]["rows"]:
                        x = dict(zip(cols, r))
                        if x.get("d1") is None or abs(x["d1"]) < OPP_MIN_MOVE:
                            continue
                        rows.append({"ticker": x["t"], "name": x.get("n"), "price": x.get("p"), "pct": x["d1"],
                                     "dvM": x.get("dv"), "capB": x.get("cap"), "ex": x.get("ex")})
                    source = f"saatlik hareketliler taraması ({at:%H:%M} ET)"
            except Exception:
                pass
        return rows, source

    def _opp_slot(self):
        done = (self.M.get("oppDone") or {}).get(self.today, [])
        for i, t0 in enumerate(OPP_TIMES):
            start = self.now.replace(hour=t0.hour, minute=t0.minute, second=0, microsecond=0)
            if start <= self.now < start + timedelta(minutes=OPP_WINDOW_MIN) and i not in done:
                return i
        return None

    def opportunity_scan(self, slot):
        """Whole-market scan (v7), four times a day. Looks at today's biggest movers among all US stocks, reads the
        headlines of the most interesting ones with catalyst.py and applies the plays in PLAYS:
          drift     (tested, event_study.py EVENT_WIDE=1) first scan only: the stock opened 8 %+ above yesterday's close
                    and the first hour added to the gap, 2 B$+ and 50 M$+/day -> long, stop below the first-hour low,
                    2 days. The opposite (first hour against the gap, the v6 rule) lost in the wide test: logged only;
          news      (experimental, cannot be back-tested without a headline archive): 5 %+ move on hard company news
                    that the price confirms (still near the day's extreme) -> trade with the news, stop at the day's
                    other extreme (2-8 %), 3 days.
        Takeovers, rumours, weak news and moves without news are logged and left alone."""
        import catalyst as CAT
        self.M["oppDone"] = {self.today: sorted(set((self.M.get("oppDone") or {}).get(self.today, [])) | {slot})}
        first = slot == 0
        rows, source = self._movers()
        scan = {"date": self.today, "at": iso(self.now), "slot": slot, "slotTime": f"{OPP_TIMES[slot]:%H:%M}",
                "source": source, "movers": len(rows), "looked": 0, "read": 0, "rows": [], "taken": []}
        cands = []
        for r in rows:
            if not r.get("ticker") or not r.get("price") or r.get("pct") is None:
                continue
            if (r["price"] < OPP_MIN_PRICE or (r.get("dvM") or 0) < OPP_MIN_DV or (r.get("capB") or 0) < OPP_MIN_CAP
                    or any(n.lower() in (r.get("name") or "").lower() for n in SANCTIONED_NAMES)):
                continue
            cands.append(r)
        scan["liquid"] = len(cands)
        cands = sorted(cands, key=lambda r: -abs(r["pct"]))[:OPP_LOOK]
        ts = [r["ticker"] for r in cands]
        bars = self.data.intraday(ts) if ts else {}
        hist = self.data.daily(ts, period="5d") if ts else {}
        today = self.now.date()
        feats = []
        for r in cands:
            t = r["ticker"]
            b, h = bars.get(t), hist.get(t)
            if b is None or h is None:
                continue
            b = b[b.index.date == today]
            past = h[h.index.date < today]
            if len(b) < 6 or len(past) < 2:
                continue
            t0 = b.index[0]
            pc, o, price = float(past["Close"].iloc[-1]), float(b["Open"].iloc[0]), float(b["Close"].iloc[-1])
            if t0.hour != 9 or t0.minute != 30 or pc <= 0 or o <= 0:
                continue
            if (pd.Timestamp(self.now) - b.index[-1]).total_seconds() / 60 > STALE_MIN:
                continue
            hi, lo = float(b["High"].max()), float(b["Low"].min())
            h1 = b[b.index < t0.replace(hour=10, minute=30)]
            f = {"ticker": t, "name": r.get("name") or t, "ex": r.get("ex"), "price": price, "prevClose": pc, "open": o,
                 "movePct": (price / pc - 1) * 100, "gapPct": (o / pc - 1) * 100, "high": hi, "low": lo,
                 "capB": r.get("capB"), "dvM": r.get("dvM"), "pos": (price - lo) / (hi - lo) if hi > lo else 0.5,
                 "h1Pct": (float(h1["Close"].iloc[-1]) / o - 1) * 100 if len(h1) >= 10 else None,
                 "h1High": float(h1["High"].max()) if len(h1) else hi, "h1Low": float(h1["Low"].min()) if len(h1) else lo,
                 "ssr": lo <= pc * (1 - SSR_DROP) or float(past["Low"].iloc[-1]) <= float(past["Close"].iloc[-2]) * (1 - SSR_DROP)}
            gapped = bool(first and abs(f["gapPct"]) >= EVENT_GAP * 100 and f["h1Pct"] is not None
                          and abs(f["h1Pct"]) >= EVENT_MIN_MOVE * 100)
            f["gapTurn"] = gapped and (f["h1Pct"] > 0) != (f["gapPct"] > 0)
            f["gapSmall"] = (gapped and f["gapPct"] > 0 and f["h1Pct"] > 0
                             and ((r.get("capB") or 0) < DRIFT_MIN_CAP or (r.get("dvM") or 0) < DRIFT_MIN_DV))
            f["contSetup"] = gapped and f["gapPct"] > 0 and f["h1Pct"] > 0 and not f["gapSmall"]
            up = f["movePct"] > 0
            f["confirmed"] = bool(abs(f["movePct"]) >= NEWS_MIN_MOVE
                                  and (f["pos"] >= NEWS_RANGE_POS if up else f["pos"] <= 1 - NEWS_RANGE_POS))
            feats.append(f)
        scan["looked"] = len(feats)
        for f in feats:
            f["moveKey"] = None if (f["contSetup"] or f["gapTurn"]) else self._move_play(f, slot)
        # headlines: the setups first, then the biggest moves
        order = sorted(feats, key=lambda f: (not f["contSetup"], not f["moveKey"], not f["confirmed"], -abs(f["movePct"])))
        now_utc = self.now.astimezone(ZoneInfo("UTC"))
        for f in order[:OPP_NEWS_TOP]:
            try:
                f["cat"] = CAT.classify(f["ticker"], f["name"], self._news_items(f["ticker"]), f["movePct"], now_utc)
                scan["read"] += 1
            except Exception as e:
                f["cat"] = None
                self.notes.append(f"{f['ticker']}: haber okunamadı ({type(e).__name__})")
        self.revalue()
        eq = self.P["totalValue"]
        opp = [p for p in self.P["positions"] if p.get("sleeve") == "event"]
        used = sum(abs(p["marketValue"]) for p in opp)
        open_n = {k: sum(1 for p in opp if p.get("play") == k) for k in PLAYS}
        held = {p["ticker"] for p in self.P["positions"]}
        closed_today = {tr["ticker"] for tr in self.L["trades"] if tr.get("date") == self.today
                        and tr.get("action") in ("SELL", "COVER") and not tr.get("cancelled")}
        lines = []
        for f in order:
            t, cat = f["ticker"], f.get("cat")
            kind = cat["kind"] if cat and cat.get("cat") else None
            play = side = stop = None
            if kind == "pinned":
                why = "satın alma haberi: fiyat teklife sabitlenir, işlem yapılmaz"
            elif f["contSetup"]:
                play, side = "drift", "long"
                stop = round(f["h1Low"] * 0.999, 2)
                why = None
            elif f["gapTurn"]:
                why = ("açılış boşluğu ilk saatte tersine döndü: geniş testte bu durumda işlem açmak zarar ettirdi "
                       "(işlem başına −%0,17), işlem yok")
            elif f.get("moveKey"):
                play, side = f["moveKey"], PLAYS[f["moveKey"]]["move"]["side"]
                stop = (round(f["low"] * (1 - MOVE_STOP_PAD), 2) if side == "long" else round(f["high"] * (1 + MOVE_STOP_PAD), 2))
                why = None
            elif "cat" not in f:
                why = f"hareket büyük ama bu taramada yalnızca en öndeki {OPP_NEWS_TOP} hissenin haberi okundu"
            elif cat is None or not cat.get("cat"):
                why = ("güncel haberler bilinen bir haber türüne uymuyor: işlem yok" if cat and cat.get("relevant") else
                       "şirketle ilgili güncel bir haber bulunamadı: nedeni belirsiz hareket, işlem yok")
            elif kind == "rumor":
                why = "söylenti ya da kalabalık hareketi: doğrulanmamış, işlem yok"
            elif kind == "soft":
                why = "zayıf haber (analist notu, yönetici değişikliği vb.): tek başına işlem nedeni sayılmaz"
            elif cat.get("conflict"):
                why = "haberin yönü ile fiyatın yönü birbirini tutmuyor: işlem yok"
            elif cat["score"] < NEWS_MIN_SCORE:
                why = "haber eşleşmesi zayıf (başlıkta net değil): işlem yok"
            elif not f["confirmed"]:
                why = (f"haber sert ama hareket %{trn(NEWS_MIN_MOVE, 0)}'in altında ya da fiyat günün aralığında geri "
                       "çekilmiş: piyasa haberi onaylamıyor, işlem yok")
            else:
                play, side = "news", ("long" if f["movePct"] > 0 else "short")
                raw = f["low"] * 0.999 if side == "long" else f["high"] * 1.001
                lo_s, hi_s = ((f["price"] * (1 - NEWS_STOP_MAX), f["price"] * (1 - NEWS_STOP_MIN)) if side == "long" else
                              (f["price"] * (1 + NEWS_STOP_MIN), f["price"] * (1 + NEWS_STOP_MAX)))
                stop = round(min(max(raw, lo_s), hi_s), 2)
                why = None
            if play is None and f["gapSmall"]:
                why = (f"açılış boşluğu sürüyor ama şirket {trn(DRIFT_MIN_CAP, 0)} milyar $'dan küçük ya da günlük hacmi "
                       f"{trn(DRIFT_MIN_DV, 0)} milyon $'ın altında (testte bu grup zarar etti); " + why)
            setup = ("pinned" if kind == "pinned" else "drift" if f["contSetup"] else "gapTurn" if f["gapTurn"]
                     else "gapSmall" if f["gapSmall"] else play if play
                     else "up" if f["movePct"] > 0 else "down")
            row = {"t": t, "n": f["name"], "ex": f.get("ex"), "p": round(f["price"], 2), "mv": round(f["movePct"], 2),
                   "gap": round(f["gapPct"], 2), "h1": round(f["h1Pct"], 2) if f["h1Pct"] is not None else None,
                   "cap": f.get("capB"), "dv": f.get("dvM"),
                   "cat": cat.get("label") if cat else None, "kind": kind,
                   "hl": cat.get("headline") if cat else None, "play": play, "side": side, "setup": setup}
            if play:
                P_ = PLAYS[play]
                self.quotes_for([t])
                q = self.quotes.get(t)
                price = q["price"] if q else f["price"]
                risk = (price / stop - 1) if side == "long" else (stop / price - 1)
                health = (self.M.get("health") or {}).get(play) or {}
                reg_need = P_.get("regime") or (P_.get("move") or {}).get("regime")
                reg_now = (self.M.get("regime") or {}).get("name")
                if reg_need and ((reg_need == "up") != (reg_now == "up")):
                    why = (f"{P_['label']} yalnızca piyasa yönü {'yukarıyken' if reg_need == 'up' else 'aşağıyken'} "
                           f"çalışır (S&P 500 200 günlük ortalamanın {'üstünde' if reg_need == 'up' else 'altında'}); "
                           f"testte öbür durumda kaybettirdi. Sinyal yalnızca kaydedildi")
                elif self.play_paused(play):
                    why = (f"{P_['label']} kuralı şu an durdurulmuş ({health.get('reason') or 'son sinyaller kaybettiriyor'}); "
                           "sinyal yalnızca kaydedildi")
                elif t in held:
                    why = "bu hisse zaten portföyde"
                elif t in closed_today:
                    why = "bugün bu hissede bir işlem kapandı; aynı gün yeniden girilmez"
                elif open_n[play] >= P_["slots"]:
                    why = f"aynı anda en fazla {P_['slots']} açık {P_['label'].lower()} işlemi"
                elif side == "short" and (f["ssr"] or (q and q["ssr"])):
                    why = "SEC Rule 201: hisse önceki kapanışa göre %10 düştüğü için short açılamaz"
                    self.comply("SEC Rule 201", f"{t}: açığa satış kısıtı",
                                f"{t} bugün ya da dün önceki kapanışa göre %10 veya daha fazla düştü; en iyi alış fiyatının "
                                f"altında açığa satış yasak. {P_['label']} kuralı bu hissede short açmadı.", "ssr")
                elif not q or q["stale"]:
                    why = "güncel fiyat yok"
                elif risk <= 0.002:
                    why = "fiyat zarar-kes seviyesine çok yakın"
                else:
                    share = min(P_["cap"], P_["risk"] / risk)
                    want = min(eq * share, max(0.0, OPP_BOOK_MAX * eq - used))
                    amount, limit = self._allowed_mv(t, side, want)
                    if amount < MIN_TRADE:
                        why = (f"fırsat işlemleri toplamı hesabın {trn(OPP_BOOK_MAX, 0)} katı sınırında" if want < MIN_TRADE
                               else f"teminat ya da {trn(MAX_GROSS, 0)}x toplam maruziyet sınırı dolu")
                    else:
                        self.names[t] = f["name"]
                        exit_day = self._trading_day_after(self.today, P_["hold"])
                        small = (f.get("capB") or 0) < 2
                        borrow = 0.05 if small else 0.01 if (f.get("capB") or 0) < 10 else BORROW_RATE
                        slip = SPIKE_SLIP if small else SLIP
                        news_txt = f" {cat['text']}" if cat and cat.get("text") else ""
                        size_txt = (f"Büyüklük: hesabın %{trn(amount / eq * 100, 0)}'i ({usd(amount)}); zarar-kes çalışırsa kayıp "
                                    f"hesabın yaklaşık %{trn(amount * risk / eq * 100, 1)}'i.")
                        if play == "drift":
                            reason = (f"Haber devamı: {t} ({f['name']}) önceki kapanışın ({trn(f['prevClose'])} $) "
                                      f"{trpct(f['gapPct'])} üstünde açıldı ve ilk saatte {trpct(f['h1Pct'])} daha yükseldi: "
                                      f"alıcılar haberi satmıyor, üstüne alıyor. Kural gereği long açıldı. Zarar-kes {trn(stop)} $ "
                                      f"(ilk saatin en düşüğünün hemen altı, risk %{trn(risk * 100, 1)}); çıkış {exit_day} "
                                      f"kapanışında. {size_txt}{news_txt}")
                        elif P_.get("move"):
                            mv_ = P_["move"]
                            reason = (f"{P_['label']} (test edildi): {t} ({f['name']}) {scan['slotTime']} taramasında önceki "
                                      f"kapanışa göre {trpct(f['movePct'])}; fiyat günün aralığının "
                                      f"{'üst' if mv_['dir'] > 0 else 'alt'} kısmında. {P_.get('why', '')} Kural gereği "
                                      f"{'long' if side == 'long' else 'short'} açıldı. Zarar-kes {trn(stop)} $ (günün "
                                      f"{'en düşüğünün altı' if side == 'long' else 'en yükseğinin üstü'}, risk %{trn(risk * 100, 1)}); "
                                      f"çıkış {exit_day} kapanışında. {size_txt}{news_txt}")
                        else:
                            reason = (f"Haber takibi (deneysel kural): {t} ({f['name']}) bugün {trpct(f['movePct'])}.{news_txt} "
                                      f"Fiyat günün aralığının {'üst' if side == 'long' else 'alt'} kısmında, yani piyasa haberi "
                                      f"geri almıyor; haber yönünde {'long' if side == 'long' else 'short'} açıldı. Zarar-kes "
                                      f"{trn(stop)} $ (risk %{trn(risk * 100, 1)}); çıkış {exit_day} kapanışında. {size_txt} "
                                      "Bu kural geçmiş veride sınanamadı (geçmiş haber arşivi yok); sonucu kural karnesinde izlenir.")
                        rule = P_["label"].lower()
                        tr = (self.open_long(t, amount, price, q["at"], reason, rule) if side == "long" else
                              self.open_short(t, amount, price, q["at"], reason, rule, slip=slip,
                                              locate=f"Ödünç ücreti varsayımı: yıllık %{trn(borrow * 100, 1)}"))
                        if not tr:
                            why = "emir gerçekleşmedi"
                        else:
                            hl = cat.get("headline") if cat else None
                            if hl and hl.get("url"):
                                tr["sources"].insert(0, {"title": hl["title"], "url": hl["url"]})
                            pos = self._pos(t, side)
                            pos.update(sleeve="event", play=play, playLabel=P_["label"], stopMode="event", stopPrice=stop,
                                       extremeSinceEntry=price, exitDate=exit_day, holdDays=P_["hold"], name=f["name"],
                                       headline=hl, borrowRate=borrow, slip=slip,
                                       catalyst={"cat": cat.get("cat"), "label": cat.get("label"), "kind": kind} if cat else None,
                                       event={"gapPct": round(f["gapPct"], 2), "firstHourPct": row["h1"],
                                              "movePct": round(f["movePct"], 2), "prevClose": round(f["prevClose"], 2)})
                            used += amount
                            open_n[play] += 1
                            held.add(t)
                            row.update(dec="al", amt=round(amount, 2), stop=stop, exit=exit_day)
                            scan["taken"].append({k: row.get(k) for k in ("t", "n", "play", "side", "mv", "amt", "stop", "exit", "cat")})
                            lines.append(f"{P_['label']}: {t} {'LONG' if side == 'long' else 'SHORT'} {usd(amount)} "
                                         f"({trpct(f['movePct'])}" + (f", {cat['label']}" if cat and cat.get("label") else "") + ").")
            if row.get("dec") != "al":
                row.update(dec="atla", why=why)
            scan["rows"].append(row)
        try:
            self.shadow_add(scan["rows"][:OPP_LOOK], scan["slotTime"], feats)
        except Exception as e:                 # the shadow book is for evaluation only
            self.notes.append(f"Gölge kayıt yazılamadı: {type(e).__name__}")
        scan["rows"] = scan["rows"][:40]
        n_take = len(scan["taken"])
        head = f"Fırsat taraması {scan['slotTime']}: " + (
            f"{scan['movers']} hareketli hisseden {scan['looked']} tanesine bakıldı, {scan['read']} tanesinin haberi okundu; "
            + (f"{n_take} işlem açıldı." if n_take else "kurala uyan işlem çıkmadı.")
            if rows else "hareketli hisse listesi alınamadı; işlem yapılmadı.")
        lines.insert(0, head)
        self.M["oppScan"] = scan
        day = [x for x in (self.M.get("oppScans") or []) if x.get("date") == self.today and x.get("slot") != slot]
        self.M["oppScans"] = day + [scan]
        run = next((x for x in reversed(self.L["runs"]) if x.get("date") == self.today), None)
        if run is not None:
            run.setdefault("decisions", []).extend(lines)
            run.setdefault("opps", []).append({k: scan[k] for k in ("slotTime", "movers", "looked", "read", "taken")})
            if n_take:
                run["headline"] = f"{n_take} fırsat işlemi · " + run.get("headline", "")
        else:
            self.notes.extend(lines)

    @staticmethod
    def rsi2(closes):
        """Wilder RSI over 2 days (the study's definition) of a list of closes; the last value."""
        up = dn = None
        for a, b in zip(closes[:-1], closes[1:]):
            u, d = max(b - a, 0.0), max(a - b, 0.0)
            up = u if up is None else up + (u - up) / 2
            dn = d if dn is None else dn + (d - dn) / 2
        if up is None:
            return None
        return 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)

    def rebound(self):
        """Once a day after 09:35: the rebound rule on yesterday's close (REB_* above). Returns True if it traded."""
        hist = self.data.daily([INDEX_ETF, DIR_SRC], period="2y")
        today = self.now.date()
        done = {t: h[h.index.date < today] for t, h in hist.items() if h is not None and len(h)}
        q = done.get(INDEX_ETF)
        if q is None or len(q) < 30:
            return False
        sig = self.index_signal(done)
        closes = [float(x) for x in q["Close"].iloc[-60:]]
        r = self.rsi2(closes)
        last_day = q.index[-1].date().isoformat()
        st = self.M.get("rebound") or {}
        if st.get("lastClose") == last_day:      # this close was already judged
            self.M["rebDate"] = self.today
            return False
        pos = self._pos(REB_TICKER, "long")
        base_on = bool(sig and sig["target"] != 0)
        self.M["rebDate"] = self.today
        st.update(lastClose=last_day, rsi2=round(r, 1) if r is not None else None, baseOn=base_on)
        traded = False
        if pos:
            st["n"] = st.get("n", 1) + 1
            why = ("endeks kolu yeniden işleme giriyor" if base_on else
                   f"RSI(2) {trn(r, 1)} ile 50'nin üstüne çıktı" if r is not None and r > REB_EXIT else
                   f"{REB_MAXD} günlük süre doldu" if st["n"] > REB_MAXD else None)
            if why:
                self.quotes_for([REB_TICKER])
                qq = self.quotes.get(REB_TICKER)
                if qq and not qq["stale"]:
                    self.close(pos, pos["shares"], qq["price"], qq["at"],
                               f"Tepki işlemi kapatıldı: {why}. Plan gereği açılışta satıldı.", "tepki çıkış")
                    st.update(n=0, text=f"Tepki işlemi kapandı ({why}).")
                    traded = True
                else:
                    self.M["rebDate"] = None      # try again on the next run
                    st["n"] -= 1
            else:
                st["text"] = f"Tepki işlemi sürüyor: RSI(2) {trn(r, 1)}, {st['n']}. gün."
        elif r is not None and r < REB_ENTRY and not base_on:
            if self.now.time() >= REB_ENTRY_BY:
                st["text"] = (f"Tepki sinyali vardı (RSI(2) {trn(r, 1)}), ama açılış penceresi (09:35–10:00) kaçtı; "
                              "kural gereği bugün girilmedi.")
            else:
                self.quotes_for([REB_TICKER])
                qq = self.quotes.get(REB_TICKER)
                eq = self.P["totalValue"]
                if qq and not qq["stale"] and eq > 0:
                    amount, limit = self._allowed_mv(REB_TICKER, "long", eq * REB_LEV)
                    if amount >= MIN_TRADE:
                        self.names[REB_TICKER] = "Invesco NASDAQ 100 ETF"
                        reason = (f"Tepki kuralı: Nasdaq 100 (QQQ) dünkü kapanışta aşırı satılmış, 2 günlük RSI {trn(r, 1)} "
                                  f"(eşik {trn(REB_ENTRY, 0)}). Endeks kolu nakitte ({sig['wordLow'] if sig else 'yön verisi yok'}). "
                                  f"Kural gereği açılışta hesabın {trn(amount / eq, 1)} katı ({usd(amount)}) Nasdaq 100 fonu "
                                  f"(QQQM) alındı; RSI(2) 50'yi geçince ya da {REB_MAXD} gün sonra açılışta satılacak. "
                                  "1999–2026 testinde bu işlemlerin %73'ü kazandı (ortalama +%4,8).")
                        tr = self.open_long(REB_TICKER, amount, qq["price"], qq["at"], reason, "tepki kuralı")
                        if tr:
                            p_ = self._pos(REB_TICKER, "long")
                            p_.update(stopMode="managed", stopPrice=None, play="rebound", sleeve="overlay",
                                      playLabel="Tepki kuralı", name="Invesco NASDAQ 100 ETF")
                            st.update(n=1, since=self.today, text=f"Tepki işlemi açıldı: RSI(2) {trn(r, 1)}, {usd(amount)}.")
                            traded = True
                    else:
                        st["text"] = "Tepki sinyali vardı ama teminat/10x sınırı dolu."
        else:
            st["text"] = (f"Tepki kuralı beklemede: RSI(2) {trn(r, 1) if r is not None else '—'}"
                          + (", endeks kolu işlemde (kural yalnızca nakit dönemlerinde çalışır)." if base_on else
                             f" (giriş için {trn(REB_ENTRY, 0)}'in altı gerekir)."))
        self.M["rebound"] = st
        if traded or st.get("text"):
            self.notes.append(st["text"])
        return traded

    @staticmethod
    def _move_play(f, slot):
        """The first price-move play (PLAYS entries with a "move" block) whose conditions the stock meets."""
        for key, P_ in PLAYS.items():
            mv = P_.get("move")
            if not mv or not P_.get("enabled", True) or slot not in mv["slots"]:
                continue
            m, pos = f["movePct"], f["pos"]
            if mv["dir"] > 0 and (m < mv["min"] or pos < mv["pos"]):
                continue
            if mv["dir"] < 0 and (m > -mv["min"] or pos > 1 - mv["pos"]):
                continue
            if (f.get("capB") or 0) < mv.get("minCap", 2.0) or (f.get("dvM") or 0) < mv.get("minDv", 50.0):
                continue
            return key
        return None

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
        caps = {}
        try:
            ci = {c: i for i, c in enumerate(mv["screen"]["cols"])}
            caps = {x[ci["t"]]: x[ci["cap"]] for x in mv["screen"]["rows"]}
        except Exception:
            pass
        lines = []
        for r in rows:
            t = r["ticker"]
            base = {k: r.get(k) for k in ("ticker", "name", "d1Pct", "prevClose", "price", "dollarVolM")}
            base["capB"] = caps.get(t)
            why = None
            if any(n.lower() in (r.get("name") or "").lower() for n in SANCTIONED_NAMES):
                why = "ABD yaptırım listesindeki şirket; asla işlem yapılmaz"
            elif r.get("buyout"):
                why = "satın alma fiyatı gibi yatay seyrediyor; short anlamsız"
            elif (r.get("prevClose") or 0) < SPIKE_MIN_PRICE:
                why = f"önceki kapanış {trn(SPIKE_MIN_PRICE, 0)} $'ın altında (çok küçük hisse)"
            elif (r.get("dollarVolM") or 0) < SPIKE_MIN_DV:
                why = f"günlük işlem hacmi {trn(SPIKE_MIN_DV, 0)} milyon $'ın altında"
            elif SPIKE_MIN_CAP and caps.get(t) is not None and caps[t] < SPIKE_MIN_CAP:
                why = (f"piyasa değeri {trn(SPIKE_MIN_CAP * 1000, 0)} milyon $'ın altında: bu kadar küçük şirketlerde ödünç "
                       "hisse bulmak gerçekte çoğu zaman mümkün değil")
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
            want = min(sleeve * SPIKE_SIZE, max(0.0, OPP_BOOK_MAX * sleeve - used))
            amount, limit = self._allowed_mv(t, "short", want)
            if amount < MIN_TRADE:
                scan["skipped"].append({**base, "why": f"teminat ya da {trn(MAX_GROSS, 0)}x toplam maruziyet sınırı dolu"})
                continue
            self.names[t] = r.get("name") or t
            stop = round(price * (1 + SPIKE_STOP), 2)
            exit_day = self._trading_day_after(self.today, SPIKE_HOLD_DAYS - 1)
            news = r.get("headline") or self._headline(t)
            back = (price / r["price"] - 1) * 100 if r.get("price") else None
            reason = (f"Sert yükseliş short'u: {t} ({r.get('name') or t}) {prev_day} günü {trpct(r['d1Pct'])} yükseldi "
                      f"({trn(r['prevClose'])} $ → {trn(r['price'])} $)"
                      + (f"; bugün {trn(price)} $'dan ({trpct(back)}) işlem görüyor" if back is not None else "")
                      + f". 2019–2026 testinde bu tür sıçramalar sonraki iki haftada genellikle geri verildi. Short açıldı: "
                      f"hesabın %{trn(SPIKE_SIZE * 100, 0)}'u, zarar-kes {trn(stop)} $ (girişin %{trn(SPIKE_STOP * 100, 0)} "
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
            pos.update(sleeve="event", spike=True, play="spike", playLabel="Sert yükseliş short'u", stopMode="event",
                       stopPrice=stop, extremeSinceEntry=price,
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
            label = p.get("playLabel") or "Fırsat işlemi"
            self.close(p, p["shares"], q["price"], q["at"],
                       f"{label} işleminin süresi doldu: {p.get('openedAt')} tarihinde açıldı; plan gereği "
                       f"{p.get('holdDays', EVENT_HOLD_DAYS)} işlem günü sonra kapanışta kapatıldı.", f"{label.lower()} çıkış")

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
        if self.bust_check():                  # the account was written off and restarted: nothing else today
            self.revalue()
            self.log_check("batış")
            self.snapshot()
            self.scorecard()
            self._reviews()
            return
        self.nasdaq_update()
        self.event_exits()
        kind = "guard"
        fresh = not self.quotes["SPY"]["stale"] and self.M.get("pausedOn") != self.today
        if fresh and self.now.time() >= REB_AFTER and self.M.get("rebDate") != self.today:
            try:
                if self.rebound():
                    kind = "tepki"
            except Exception as e:             # one rule; never block the rest of the run
                self.M["rebDate"] = self.today
                self.notes.append(f"Tepki kuralı çalışmadı: {type(e).__name__}: {e}")
            self.margin_check()
        if self.now.time() >= DAILY_AFTER and self.M.get("lastDailyDate") != self.today and fresh:
            self.daily()
            kind = "daily"
            self.margin_check()
        if (SPIKE_AFTER <= self.now.time() < SPIKE_LAST_ENTRY and self.M.get("spikeScanDate") != self.today and fresh):
            try:
                self.spike_scan()
                kind = "spike" if kind == "guard" else kind
            except Exception as e:             # one rule; never block the rest of the run
                self.M["spikeScanDate"] = self.today
                self.notes.append(f"Sert yükseliş taraması yapılamadı: {type(e).__name__}: {e}")
            self.margin_check()
        slot = self._opp_slot() if fresh else None
        if slot is not None:
            try:
                self.opportunity_scan(slot)
                kind = "fırsat"
            except Exception as e:             # the scan is one book; never block the rest of the run
                self.M["oppDone"] = {self.today: sorted(set((self.M.get("oppDone") or {}).get(self.today, [])) | {slot})}
                self.notes.append(f"Fırsat taraması yapılamadı: {type(e).__name__}: {e}")
            self.margin_check()
        self.revalue()
        if self.status == "normal":
            for p in self.P["positions"]:
                d = self._dist(p)
                if d is not None and d < 3:
                    self.status = "uyari"
                    self.notes.append(f"{p['ticker']} zarar-kes seviyesine %3'ten yakın")
                    break
            room = self.P.get("closeoutMovePct")
            if room is not None and room < 3:
                self.status = "uyari"
                self.notes.append(f"Zorunlu tasfiyeye %{trn(room, 1)} kaldı (pozisyonların toplamı bu kadar aleyhe giderse)")
        self.log_check(kind)
        self.snapshot()
        self.scorecard()
        self._reviews()

    def _reviews(self):
        try:
            self.save_marks()
            self.build_reviews()
        except Exception as e:                     # never let the evaluation break a run
            print("review failed:", type(e).__name__, e)

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
            "qqqChangePct": round(self.quotes[INDEX_ETF]["dayChangePct"], 2) if INDEX_ETF in self.quotes else None,
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
        margin_calls = sum(1 for t in self.L["trades"] if t.get("rule") in ("teminat çağrısı", "zorunlu tasfiye") and t.get("ts", "") >= E["startAt"]
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
                {"key": "margin", "label": "Zorunlu tasfiye yok", "ok": margin_calls == 0, "value": margin_calls, "unit": "count"},
                {"key": "bust", "label": "Hesap hiç batmadı", "ok": not self.L.get("busts"), "value": len(self.L.get("busts", [])),
                 "unit": "count"},
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
                      "qqqPrice": round(self.quotes[INDEX_ETF]["price"], 4) if INDEX_ETF in self.quotes else None,
                      "holdings": [{"ticker": p["ticker"], "side": p["side"], "value": p["marketValue"],
                                    "u": p.get("unrealized"),
                                    "play": p.get("play") or ("index" if p["ticker"] in INDEX_SLEEVE else "other")}
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
    review = load_review()
    status, actions, traded = "hata", [], False
    try:
        eng = Engine(ledger, data, now, load_json(DEPOSITS, []), load_json(RESTORE), review)
        eng.run()
        save_review(eng.R)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # stage it right away: a loop that started with an older bot.yml only adds ledger.json, and an
            # unstaged review.json would make its "git pull --rebase" (and so every push) fail
            try:
                import subprocess
                subprocess.run(["git", "add", "review.json"], cwd=ROOT, timeout=30, capture_output=True)
            except Exception as e:
                print("git add review.json failed:", e)
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
