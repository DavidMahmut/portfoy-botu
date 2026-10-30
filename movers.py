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
        items = N.fetch_yf(t)
        if not items:
            return None
        h = {k: items[0].get(k) for k in ("title", "url", "publisher", "at")}
        h["at"] = h["at"].isoformat() if hasattr(h["at"], "isoformat") else h["at"]     # JSON needs text, not datetime
        return h
    except Exception:
        return None


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
    sent = set((old.get("alerted") or {}).get(today, []))
    new = [r for r in alerts if r["ticker"] not in sent]
    study = None
    if (ROOT / "movers_study.json").exists():
        try:
            study = json.loads((ROOT / "movers_study.json").read_text()).get("summary")
        except Exception:
            study = None
    closed = now.time() >= datetime.strptime("16:00", "%H:%M").time() or now.weekday() >= 5
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
           "study": {**study, "text": study_text(study)} if study else None,
           "notes": "Nasdaq'ta işlem gören hisse senetleri (ETF, varant, imtiyazlı hisse hariç); fiyatı 3 $'ın, son 20 günlük "
                    "ortalama işlem hacmi 2 milyon $'ın altındakiler elendi. Günlük değişim önceki kapanışa göre (seans "
                    "açıkken anlık fiyatla), haftalık değişim 5 işlem günü öncesine göre. Yalnızca izleme; bot bu listeyle "
                    "işlem yapmaz."}
    (ROOT / "movers.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    md = ROOT / "alerts_new.md"
    if new:
        def line(r):
            h = r.get("headline") or {}
            d = "yükseldi" if r["d1Pct"] > 0 else "düştü"
            return (f"- **{r['ticker']}** ({r['name']}): bugün %{abs(r['d1Pct']):.1f} {d}, fiyat {r['price']} $ "
                    f"(önceki kapanış {r['prevClose']} $, haftalık {r['w1Pct']:+.1f}%)"
                    + (f"\n  Haber: [{h.get('title')}]({h.get('url')})" if h.get("url") else ""))
        title = "Nasdaq %20 hareket: " + ", ".join(f"{r['ticker']} {r['d1Pct']:+.0f}%" for r in new[:6]) + \
                (f" ve {len(new) - 6} hisse daha" if len(new) > 6 else "")
        body = [title, "", f"@{OWNER} {now:%d.%m.%Y %H:%M} ET itibarıyla günlük %20 veya daha fazla hareket eden Nasdaq hisseleri:", ""]
        body += [line(r) for r in new]
        if study:
            body += ["", "Geçmişte ne oldu (2019–2026 testi, aynı filtrelerle): " + study_text(study)]
        body += ["", "Bu bir bildirimdir, alım-satım önerisi değildir. Tüm liste: https://davidmahmut.github.io/portfoy-botu/#hareketliler"]
        md.write_text("\n".join(body) + "\n", encoding="utf-8")
    elif md.exists():
        md.unlink()
    print(len(rows), "screened;", len(alerts), "alerts;", len(new), "new")
    return 0


if __name__ == "__main__":
    sys.exit(main())
