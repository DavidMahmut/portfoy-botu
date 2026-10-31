"""Nasdaq movers watch (no trading): the biggest daily and weekly gainers and losers among Nasdaq-listed common stocks,
and an alert when a stock moves 20 % or more in a day.

Runs on GitHub Actions (.github/workflows/movers.yml) every hour during the session and once after the close. Writes
movers.json for the dashboard; new 20 % alerts are also written to alerts_new.md, which the workflow turns into a GitHub
issue (GitHub then notifies the repository owner by e-mail / app). Data: Nasdaq Trader symbol directory + Yahoo Finance.
"""
import json
import math
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")
SYMBOLS_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
MIN_PRICE, MIN_DOLLAR_VOL = 3.0, 2_000_000     # skip penny stocks and stocks too thin to trade
ALERT, TOP_N = 0.20, 25
# Daily e-mail: only the lower-risk 20 % movers. A stock is "riskli" (left out of the e-mail) if any of these fail.
LOW_RISK = {"minPrice": 10.0,        # previous close >= 10 $ (no penny / near-penny stocks)
            "minCapB": 2.0,          # market value >= 2 billion $ (mid and large companies)
            "minDollarVolM": 20.0,   # >= 20 million $ traded a day on average (easy to get in and out)
            "minDays": 250,          # listed for about a year or more (no fresh IPO / SPAC)
            "maxDailyVolPct": 5.0,   # typical daily swing over the previous 60 days <= 5 % (not a chronic gambler's stock)
            "reverseSplitDays": 365} # no reverse split in the last 12 months (a sign of a collapsing share price)
LEVELS = ((0.50, 3, "Seviye 3 · çok sert (%50 ve üzeri)"), (0.30, 2, "Seviye 2 · sert (%30–50)"),
          (0.20, 1, "Seviye 1 · dikkat (%20–30)"))
SANCTIONED_NAMES = ("Semiconductor Manufacturing International", "Hikvision", "SenseTime", "Sugon", "Dawning",
                    "China Mobile", "China Telecom", "China Unicom", "Inspur", "Megvii", "iFlytek", "Dahua")
SKIP_NAME = re.compile(r"warrant|right|unit|preferred|notes? due|debenture|depositary share.*preferred|%", re.I)
OWNER = "DavidMahmut"


def nasdaq_symbols():
    import requests
    txt = requests.get(SYMBOLS_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    out = {}
    lines = txt.splitlines()
    head = lines[0].split("|")
    for line in lines[1:]:
        f = dict(zip(head, line.split("|")))
        sym, name = f.get("Symbol", ""), f.get("Security Name", "")
        if not sym or line.startswith("File Creation") or f.get("Test Issue") == "Y" or f.get("ETF") == "Y":
            continue
        if SKIP_NAME.search(name) or not re.fullmatch(r"[A-Z]{1,5}", sym):
            continue
        out[sym] = name.split(" - ")[0].strip()
    return out


def download(tickers, **kw):
    import yfinance as yf
    frames = {}
    for i in range(0, len(tickers), 250):
        chunk = tickers[i:i + 250]
        for attempt in range(3):
            try:
                df = yf.download(chunk, group_by="ticker", auto_adjust=False, progress=False, threads=True, **kw)
                break
            except Exception as e:
                print("retry", e)
                time.sleep(10)
        else:
            continue
        for t in chunk:
            try:
                s = df[t].dropna(subset=["Close"])
            except Exception:
                continue
            if len(s) >= 3:
                frames[t] = s
        time.sleep(1)
    return frames


def headline(t):
    try:
        import news as N
        items = N.fetch_yf(t) or N.fetch_rss(t)
        if not items:
            return None
        h = {k: items[0].get(k) for k in ("title", "url", "publisher", "at")}
        h["at"] = h["at"].isoformat() if hasattr(h["at"], "isoformat") else h["at"]     # JSON needs text, not datetime
        return h
    except Exception:
        return None


def trn(x, d=1):
    return f"{x:,.{d}f}".replace(",", " ").replace(".", ",").replace(" ", ".")


def level_of(d1pct):
    a = abs(d1pct) / 100
    return next((lv for th, lv, _ in LEVELS if a >= th), 0)


def market_cap(t):
    try:
        import yfinance as yf
        fi = yf.Ticker(t).fast_info
        for k in ("marketCap", "market_cap"):
            try:
                v = fi[k]
            except Exception:
                v = getattr(fi, k, None)
            if v and v > 0:
                return float(v)
    except Exception as e:
        print("market cap failed:", t, e)
    return None


def risk_check(alerts, cache):
    """Marks each 20 % mover as lower-risk or risky (with the reasons, in Turkish). `cache` keeps the slow lookups
    (one year of history, market value) per ticker for the day."""
    need = [r["ticker"] for r in alerts if r["ticker"] not in cache]
    hist = download(need, period="13mo", interval="1d", actions=True) if need else {}
    for t in need:
        s, c = hist.get(t), {"capB": None, "days": None, "dailyVolPct": None, "reverseSplit": None}
        cap = market_cap(t)
        c["capB"] = round(cap / 1e9, 2) if cap else None
        if s is not None and len(s):
            d_last = s.index[-1].date()
            past = s[[d < d_last for d in s.index.date]] if len(s) > 1 else s
            c["days"] = int(len(past))
            r = past["Close"].astype(float).pct_change().dropna().tail(60)
            c["dailyVolPct"] = round(float(r.std() * 100), 2) if len(r) > 10 else None
            if "Stock Splits" in s:
                sp = s["Stock Splits"].astype(float).fillna(0)
                recent = sp[[(d_last - d).days <= LOW_RISK["reverseSplitDays"] for d in s.index.date]]
                rs = recent[(recent > 0) & (recent < 1)]
                c["reverseSplit"] = str(rs.index[-1].date()) if len(rs) else None
        cache[t] = c
    L = LOW_RISK
    for r in alerts:
        c, why = cache.get(r["ticker"], {}), []
        if any(n.lower() in (r.get("name") or "").lower() for n in SANCTIONED_NAMES):
            why.append("ABD yaptırım listesinde")
        if r["prevClose"] < L["minPrice"]:
            why.append(f"fiyatı {L['minPrice']:.0f} $'ın altında")
        if c.get("capB") is None:
            why.append("piyasa değeri bulunamadı")
        elif c["capB"] < L["minCapB"]:
            why.append(f"küçük şirket (piyasa değeri {c['capB']:.1f} milyar $)".replace(".", ","))
        if r["dollarVolM"] < L["minDollarVolM"]:
            why.append(f"işlem hacmi düşük (günde {r['dollarVolM']:.1f} milyon $)".replace(".", ","))
        if c.get("days") is not None and c["days"] < L["minDays"]:
            why.append(f"borsada 1 yıldan kısa süredir ({c['days']} işlem günü)")
        if c.get("dailyVolPct") is not None and c["dailyVolPct"] > L["maxDailyVolPct"]:
            why.append(f"aşırı oynak (günde ortalama %{c['dailyVolPct']:.1f} oynuyor)".replace(".", ","))
        if c.get("reverseSplit"):
            why.append(f"son 12 ayda ters bölünme ({c['reverseSplit']})")
        if r.get("buyout"):
            why.append("satın alma fiyatına sabitlenmiş görünüyor")
        r["level"] = level_of(r["d1Pct"])
        r["capB"] = c.get("capB")
        r["risk"] = {"low": not why, "reasons": why}
    return alerts


def summary_mail(day, alerts, study, now):
    """One e-mail per trading day after the close: only the lower-risk 20 % movers, grouped by how violent the move was."""
    low = [r for r in alerts if r.get("risk", {}).get("low")]
    risky = [r for r in alerts if not r.get("risk", {}).get("low")]
    if not low:
        return None
    dt = datetime.strptime(day, "%Y-%m-%d")
    title = (f"Nasdaq günlük özet {dt:%d.%m.%Y}: {len(low)} düşük riskli %20 hareket ("
             + ", ".join(f"{r['ticker']} {'+' if r['d1Pct'] > 0 else '−'}%{abs(r['d1Pct']):.0f}" for r in low[:5]) + (" …" if len(low) > 5 else "") + ")")
    body = [title, "", f"@{OWNER} {dt:%d.%m.%Y} kapanışında %20 veya daha fazla hareket eden Nasdaq hisselerinden yalnızca "
            "düşük riskli olanlar (fiyat ≥ 10 $, piyasa değeri ≥ 2 milyar $, günde ≥ 20 milyon $ işlem, en az 1 yıldır "
            "borsada, son 60 günde aşırı oynak değil, son 12 ayda ters bölünme yok), hareketin şiddetine göre:", ""]
    for th, lv, label in LEVELS:
        grp = sorted([r for r in low if r["level"] == lv], key=lambda r: -abs(r["d1Pct"]))
        if not grp:
            continue
        body.append(f"### {label}")
        for r in grp:
            h = r.get("headline") or {}
            d = "yükseldi" if r["d1Pct"] > 0 else "düştü"
            arrow = "▲" if r["d1Pct"] > 0 else "▼"
            body.append(f"- {arrow} **{r['ticker']}** ({r['name']}): %{trn(abs(r['d1Pct']))} {d}, {trn(r['prevClose'], 2)} $ → "
                        f"{trn(r['price'], 2)} $, piyasa değeri {trn(r['capB'])} milyar $, günde {trn(r['dollarVolM'], 0)} milyon $ "
                        f"işlem, haftalık {'+' if r['w1Pct'] >= 0 else '−'}%{trn(abs(r['w1Pct']))}"
                        + (f"\n  Haber: [{h.get('title')}]({h.get('url')})" if h.get("url") else ""))
        body.append("")
    if risky:
        body.append(f"Riskli bulunduğu için listeye alınmayan {len(risky)} hisse: "
                    + "; ".join(f"{r['ticker']}: {r['risk']['reasons'][0]}" for r in risky[:12])
                    + (" …" if len(risky) > 12 else "") + ".")
        body.append("")
    if study:
        body.append("Geçmişte ne oldu (2019–2026 testi): " + study_text(study))
        body.append("")
    body.append("Düşük risk, risksiz demek değildir; bu bir bildirimdir, alım-satım önerisi değildir. Tüm liste: "
                "https://davidmahmut.github.io/portfoy-botu/#hareketliler")
    return "\n".join(body) + "\n"


def _p(x):
    return ("−" if x < 0 else "+") + f"%{abs(x):.1f}".replace(".", ",")


def study_text(st):
    """Turkish one-liner from movers_study.json's summary."""
    try:
        u, d = st["up5d"], st["down5d"]
        return (f"%20+ yükselen hisseler sonraki 5 günde ortalama {_p(u['rawMeanPct'])} (medyan {_p(u['rawMedianPct'])}), "
                f"%20+ düşenler ortalama {_p(d['rawMeanPct'])} (medyan {_p(d['rawMedianPct'])}) hareket etti. Yani sert "
                f"yükselenler çoğunlukla geri verdi, sert düşenler ise çoğunlukla düşmeye devam etti: düşüşten hemen sonra "
                f"almak 5 günde ortalama {_p(d['tradeMeanPct'])} getirdi (kazanma oranı %{d['winRatePct']:.0f}). "
                "Batan şirketler veride olmadığı için gerçek sonuç daha da kötüdür.")
    except Exception:
        return st.get("text", "")


def main():
    now = datetime.now(NY)
    try:
        names = nasdaq_symbols()
    except Exception as e:
        print("symbol list failed:", e)
        prev = json.loads((ROOT / "movers.json").read_text()) if (ROOT / "movers.json").exists() else {}
        names = prev.get("universe", {})
    data = download(sorted(names), period="1mo", interval="1d")
    rows = []
    for t, s in data.items():
        c, v = s["Close"].astype(float), s["Volume"].astype(float)
        last, prev = float(c.iloc[-1]), float(c.iloc[-2])
        if prev <= 0 or last <= 0:
            continue
        dv = float((c.iloc[:-1] * v.iloc[:-1]).tail(20).mean())     # average dollar volume before today
        if prev < MIN_PRICE or last < 1 or not math.isfinite(dv) or dv < MIN_DOLLAR_VOL:
            continue
        w = float(c.iloc[-6]) if len(c) >= 6 else float(c.iloc[0])
        o, hi, lo = (float(s[k].iloc[-1]) for k in ("Open", "High", "Low"))
        gap = o / prev - 1 if o > 0 else 0.0
        rows.append({"ticker": t, "name": names.get(t, t), "price": round(last, 2), "prevClose": round(prev, 2),
                     "d1Pct": round((last / prev - 1) * 100, 2), "w1Pct": round((last / w - 1) * 100, 2),
                     "dollarVolM": round(dv / 1e6, 1), "date": str(s.index[-1].date()), "gapPct": round(gap * 100, 2),
                     # opened 15 %+ higher and then barely moved: the typical picture of a takeover at a fixed price
                     "buyout": bool(gap > 0.15 and last > prev and (hi - lo) / last < 0.03)})
    by = lambda k, rev: sorted(rows, key=lambda r: r[k], reverse=rev)[:TOP_N]
    alerts = [r for r in rows if abs(r["d1Pct"]) >= ALERT * 100]
    alerts.sort(key=lambda r: -abs(r["d1Pct"]))
    for r in alerts[:30]:
        r["headline"] = headline(r["ticker"])
    old = json.loads((ROOT / "movers.json").read_text()) if (ROOT / "movers.json").exists() else {}
    today = now.date().isoformat()
    cache = dict((old.get("riskCache") or {}).get(today) or {})
    try:
        risk_check(alerts, cache)
    except Exception as e:                       # the watch list must not fail because of the risk lookup
        print("risk check failed:", e)
    sent = set((old.get("alerted") or {}).get(today, []))
    study = None
    if (ROOT / "movers_study.json").exists():
        try:
            study = json.loads((ROOT / "movers_study.json").read_text()).get("summary")
        except Exception:
            study = None
    closed = now.time() >= datetime.strptime("16:00", "%H:%M").time() or now.weekday() >= 5
    bar_day = max((r["date"] for r in rows), default=today)
    mail = None
    if closed and old.get("mailedDay") != bar_day:
        mail = summary_mail(bar_day, alerts, study, now)
    hist = dict(old.get("spikeHistory") or {})
    d = max((r["date"] for r in rows), default=None)  # the trader reads this the next morning (sert yükseliş short'u)
    if d and not (hist.get(d) or {}).get("final"):
        hist[d] = {"final": closed or d < today, "at": now.isoformat(timespec="minutes"),
                   "rows": [{k: r.get(k) for k in ("ticker", "name", "price", "prevClose", "d1Pct", "gapPct", "dollarVolM",
                                                   "buyout", "headline")}
                            for r in alerts if r["date"] == d]}
    hist = {d: hist[d] for d in sorted(hist)[-15:]}
    out = {"asOf": now.isoformat(timespec="minutes"), "session": "kapanış" if closed else "açık",
           "universeSize": len(names), "screened": len(rows), "minPrice": MIN_PRICE, "minDollarVolM": MIN_DOLLAR_VOL / 1e6,
           "dailyLosers": by("d1Pct", False), "dailyGainers": by("d1Pct", True),
           "weeklyLosers": by("w1Pct", False), "weeklyGainers": by("w1Pct", True), "alerts": alerts,
           "alerted": {today: sorted(sent | {r["ticker"] for r in alerts})}, "spikeHistory": hist,
           "riskCache": {today: cache}, "lowRisk": LOW_RISK,
           "mailedDay": bar_day if closed else old.get("mailedDay"),
           "study": {**study, "text": study_text(study)} if study else None,
           "notes": "Nasdaq'ta işlem gören hisse senetleri (ETF, varant, imtiyazlı hisse hariç); fiyatı 3 $'ın, son 20 günlük "
                    "ortalama işlem hacmi 2 milyon $'ın altındakiler elendi. Günlük değişim önceki kapanışa göre (seans "
                    "açıkken anlık fiyatla), haftalık değişim 5 işlem günü öncesine göre. Bot bu listeden yalnızca "
                    "\"sert yükseliş short'u\" kuralına uyanlara ertesi sabah girer."}
    (ROOT / "movers.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    md = ROOT / "alerts_new.md"
    if mail:
        md.write_text(mail, encoding="utf-8")
    elif md.exists():
        md.unlink()
    print(len(rows), "screened;", len(alerts), "alerts;", sum(1 for r in alerts if r.get("risk", {}).get("low")), "low risk;",
          "mail" if mail else "no mail")
    return 0


if __name__ == "__main__":
    sys.exit(main())
