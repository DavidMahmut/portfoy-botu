"""Research only (never trades): trend screen of Chinese technology stocks in New York, Hong Kong, Shanghai and Shenzhen.

Runs on GitHub Actions (.github/workflows/china.yml) with Yahoo Finance daily data and writes china_screen.json.
Uptrend = price above its 200-day average, 50-day average above the 200-day and a positive 6-month return
(the same idea as the bot's own trend rules). Names are ranked by 12-1 month momentum like the bot's basket.
"""
import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ticker: (name, venue, theme)
UNIVERSE = {
    # US-listed (ADR) - buyable in any US brokerage account
    "BABA": ("Alibaba", "ABD", "bulut ve yapay zekâ, e-ticaret"), "BIDU": ("Baidu", "ABD", "yapay zekâ, otonom sürüş"),
    "PDD": ("PDD (Temu)", "ABD", "e-ticaret"), "JD": ("JD.com", "ABD", "e-ticaret, lojistik"),
    "NTES": ("NetEase", "ABD", "oyun"), "TCOM": ("Trip.com", "ABD", "online seyahat"),
    "BILI": ("Bilibili", "ABD", "video, oyun"), "TME": ("Tencent Music", "ABD", "müzik yayını"),
    "FUTU": ("Futu", "ABD", "online aracı kurum"), "XPEV": ("XPeng", "ABD", "elektrikli araç, otonom sürüş"),
    "LI": ("Li Auto", "ABD", "elektrikli araç"), "NIO": ("NIO", "ABD", "elektrikli araç"),
    "HSAI": ("Hesai", "ABD", "lidar sensörleri"), "KC": ("Kingsoft Cloud", "ABD", "bulut"),
    "GDS": ("GDS", "ABD", "veri merkezi"), "VNET": ("VNET", "ABD", "veri merkezi"),
    "PONY": ("Pony.ai", "ABD", "robotaksi"), "WRD": ("WeRide", "ABD", "robotaksi"),
    "BZ": ("Kanzhun", "ABD", "online işe alım"), "YMM": ("Full Truck Alliance", "ABD", "lojistik platformu"),
    "QFIN": ("Qifu", "ABD", "finans teknolojisi"), "TAL": ("TAL Education", "ABD", "eğitim teknolojisi"),
    "TCEHY": ("Tencent (OTC)", "ABD", "oyun, sosyal medya, bulut"),
    # Hong Kong - needs a broker with HKEX access (IBKR has it)
    "0700.HK": ("Tencent", "Hong Kong", "oyun, sosyal medya, bulut"), "9988.HK": ("Alibaba", "Hong Kong", "bulut ve yapay zekâ"),
    "1810.HK": ("Xiaomi", "Hong Kong", "telefon, elektrikli araç"), "3690.HK": ("Meituan", "Hong Kong", "yemek teslimatı"),
    "1024.HK": ("Kuaishou", "Hong Kong", "kısa video, yapay zekâ video"), "0981.HK": ("SMIC", "Hong Kong", "çip üretimi"),
    "1347.HK": ("Hua Hong", "Hong Kong", "çip üretimi"), "9660.HK": ("Horizon Robotics", "Hong Kong", "otonom sürüş çipleri"),
    "0992.HK": ("Lenovo", "Hong Kong", "bilgisayar, yapay zekâ sunucuları"), "2382.HK": ("Sunny Optical", "Hong Kong", "kamera optiği"),
    "0268.HK": ("Kingdee", "Hong Kong", "kurumsal yazılım"), "0020.HK": ("SenseTime", "Hong Kong", "yapay zekâ"),
    "1211.HK": ("BYD", "Hong Kong", "elektrikli araç, batarya"), "0285.HK": ("BYD Electronic", "Hong Kong", "elektronik üretim"),
    "0522.HK": ("ASMPT", "Hong Kong", "çip ekipmanı"), "0772.HK": ("China Literature", "Hong Kong", "dijital içerik"),
    "1357.HK": ("Meitu", "Hong Kong", "yapay zekâ uygulamaları"),
    # Shanghai / Shenzhen A-shares - foreigners reach them through Stock Connect (not every US broker offers it)
    "688256.SS": ("Cambricon", "Şanghay", "yapay zekâ çipleri"), "688981.SS": ("SMIC (A)", "Şanghay", "çip üretimi"),
    "688041.SS": ("Hygon", "Şanghay", "işlemci, yapay zekâ çipleri"), "002371.SZ": ("Naura", "Shenzhen", "çip ekipmanı"),
    "688012.SS": ("AMEC", "Şanghay", "çip ekipmanı"), "300308.SZ": ("Zhongji Innolight", "Shenzhen", "optik modüller (veri merkezi)"),
    "300502.SZ": ("Eoptolink", "Shenzhen", "optik modüller (veri merkezi)"), "002475.SZ": ("Luxshare", "Shenzhen", "elektronik üretim"),
    "300750.SZ": ("CATL", "Shenzhen", "batarya"), "601138.SS": ("Foxconn Industrial Internet", "Şanghay", "yapay zekâ sunucuları"),
    "002230.SZ": ("iFlytek", "Shenzhen", "ses ve dil yapay zekâsı"), "688111.SS": ("Kingsoft Office", "Şanghay", "ofis yazılımı"),
    "603501.SS": ("OmniVision (Will Semi)", "Şanghay", "görüntü sensörleri"), "688008.SS": ("Montage", "Şanghay", "bellek arayüz çipleri"),
    "000063.SZ": ("ZTE", "Shenzhen", "telekom ekipmanı"), "002415.SZ": ("Hikvision", "Shenzhen", "güvenlik kameraları"),
    "688521.SS": ("VeriSilicon", "Şanghay", "çip tasarım hizmeti"), "603019.SS": ("Sugon (Dawning)", "Şanghay", "süper bilgisayar"),
}


def load(tickers):
    import yfinance as yf
    df = yf.download(sorted(tickers), period="2y", interval="1d", auto_adjust=True, group_by="ticker", progress=False,
                     threads=True)
    out = {}
    for t in tickers:
        try:
            s = df[t]["Close"].dropna()
        except KeyError:
            continue
        if len(s) > 60:
            out[t] = s
    return out


def stats(c):
    p = float(c.iloc[-1])
    ret = lambda n: (p / float(c.iloc[-1 - n]) - 1) * 100 if len(c) > n else None
    sma50 = float(c.iloc[-50:].mean())
    sma200 = float(c.iloc[-200:].mean()) if len(c) >= 200 else None
    hi52 = float(c.iloc[-252:].max())
    mom = (float(c.iloc[-22]) / float(c.iloc[-253]) - 1) * 100 if len(c) > 253 else None
    vol = float(c.pct_change().iloc[-20:].std() * math.sqrt(252) * 100)
    r6 = ret(126)
    up = sma200 is not None and p > sma200 and sma50 > sma200 and (r6 or 0) > 0
    return {"price": round(p, 2), "date": str(c.index[-1].date()), "r1m": ret(21), "r3m": ret(63), "r6m": r6,
            "r12m": ret(252), "mom121": mom, "sma50": round(sma50, 2), "sma200": round(sma200, 2) if sma200 else None,
            "aboveSma200Pct": round((p / sma200 - 1) * 100, 2) if sma200 else None,
            "fromHighPct": round((p / hi52 - 1) * 100, 2), "vol20Pct": round(vol, 1), "uptrend": up}


def fundamentals(t):
    import yfinance as yf
    try:
        i = yf.Ticker(t).info or {}
    except Exception:
        return {}
    keep = {"marketCap": "marketCap", "currency": "currency", "forwardPE": "forwardPE", "trailingPE": "trailingPE",
            "revenueGrowth": "revenueGrowth", "earningsGrowth": "earningsGrowth", "profitMargins": "profitMargins"}
    return {k: i.get(v) for k, v in keep.items() if i.get(v) is not None}


def main():
    data = load(list(UNIVERSE))
    rows = []
    for t, c in data.items():
        name, venue, theme = UNIVERSE[t]
        s = stats(c)
        s = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()}
        rows.append({"ticker": t, "name": name, "venue": venue, "theme": theme, **s})
    rows.sort(key=lambda r: (r["mom121"] is None, -(r["mom121"] or 0)))
    for r in [x for x in rows if x["uptrend"]][:25]:
        r["fundamentals"] = fundamentals(r["ticker"])
    bench = {}
    for t in ("KWEB", "FXI", "QQQ", "SPY"):
        try:
            b = load([t]).get(t)
            if b is not None:
                bench[t] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in stats(b).items()}
        except Exception:
            pass
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "count": len(rows),
           "missing": sorted(set(UNIVERSE) - set(data)), "uptrendCount": sum(1 for r in rows if r["uptrend"]),
           "benchmarks": bench, "rows": rows,
           "notes": "Yahoo Finance günlük kapanışları (temettü düzeltmeli). Yükseliş trendi: fiyat 200 günlük ortalamanın "
                    "üstünde, 50 günlük ortalama 200 günlüğün üstünde ve 6 aylık getiri pozitif. Sıralama: 12-1 ay getirisi."}
    (ROOT / "china_screen.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(rows), "rows,", out["uptrendCount"], "in uptrend; missing:", out["missing"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
