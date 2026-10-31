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
# "Fell a lot and calmed down" alert (rebound_study.py rule crash20_s3), only for lower-risk companies
REBOUND = {"crash": 0.20, "minAge": 10, "maxAge": 60, "calm": 0.70, "minPrice": 10.0, "minCapB": 2.0, "minDollarVolM": 20.0}
LEVELS = ((0.50, 3, "Seviye 3 · çok sert (%50 ve üzeri)"), (0.30, 2, "Seviye 2 · sert (%30–50)"),
          (0.20, 1, "Seviye 1 · dikkat (%20–30)"))
SANCTIONED_NAMES = ("Semiconductor Manufacturing International", "Hikvision", "SenseTime", "Sugon", "Dawning",
                    "China Mobile", "China Telecom", "China Unicom", "Inspur", "Megvii", "iFlytek", "Dahua")
SKIP_NAME = re.compile(r"warrant|right|unit|preferred|notes? due|debenture|depositary share.*preferred|%", re.I)
OWNER = "DavidMahmut"
FUND = ROOT / "fundamentals.json"          # once a day: shares, EPS, market value (Yahoo screener) + index members
CATEGORY = {}                             # Nasdaq market tier: Q Global Select, G Global Market, S Capital Market
SCREEN_COLS = ["t", "n", "p", "d1", "w1", "dv", "cap", "pe", "s30", "s200", "atr", "idx", "tier", "lo52", "hi52"]


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
        CATEGORY[sym] = f.get("Market Category", "")
    return out


UA = {"User-Agent": "portfoy-botu/1.0 (https://github.com/DavidMahmut/portfoy-botu)"}


def _wiki_list(url, cols):
    import io
    import requests
    r = requests.get(url, timeout=30, headers=UA)
    r.raise_for_status()
    for tb in pd.read_html(io.StringIO(r.text)):
        for c in cols:
            if c in tb.columns:
                vals = [str(x).strip().replace(".", "-") for x in tb[c] if isinstance(x, str)]
                if len(vals) >= 90:
                    return sorted(set(vals))
    return []


def index_lists():
    """Nasdaq-100 and S&P 500 members. Wikipedia first; fallbacks: Nasdaq's own list API and the open
    datasets/s-and-p-500-companies CSV on GitHub. Returns (ndx, spx, errors)."""
    import requests
    errs, ndx, spx = [], [], []
    try:
        ndx = _wiki_list("https://en.wikipedia.org/wiki/Nasdaq-100", ("Ticker", "Symbol"))
    except Exception as e:
        errs.append(f"wiki ndx: {type(e).__name__}: {str(e)[:120]}")
    if len(ndx) < 90:
        try:
            r = requests.get("https://api.nasdaq.com/api/quote/list-type/nasdaq100", timeout=30,
                             headers={**UA, "Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            rows = (((r.json() or {}).get("data") or {}).get("data") or {}).get("rows") or []
            ndx = sorted({x["symbol"].strip() for x in rows if x.get("symbol")})
        except Exception as e:
            errs.append(f"nasdaq api: {type(e).__name__}: {str(e)[:120]}")
    try:
        spx = _wiki_list("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", ("Symbol", "Ticker"))
    except Exception as e:
        errs.append(f"wiki spx: {type(e).__name__}: {str(e)[:120]}")
    if len(spx) < 450:
        try:
            import io
            r = requests.get("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
                             timeout=30, headers=UA)
            spx = sorted({str(x).strip().replace(".", "-") for x in pd.read_csv(io.StringIO(r.text))["Symbol"]})
        except Exception as e:
            errs.append(f"spx csv: {type(e).__name__}: {str(e)[:120]}")
    return ndx, spx, errs


def fundamentals(today):
    """Shares outstanding, trailing EPS and market value for every Nasdaq stock (Yahoo screener, 250 per page), once a
    day; Nasdaq-100 and S&P 500 members (Wikipedia), once a week. Kept in fundamentals.json."""
    F = json.loads(FUND.read_text(encoding="utf-8")) if FUND.exists() else {}
    changed = False
    idx = F.get("index") or {}
    if not idx.get("date") or (datetime.fromisoformat(today) - datetime.fromisoformat(idx["date"])).days >= 7:
        try:
            ndx, spx, errs = index_lists()
            F["indexError"] = "; ".join(errs + [f"ndx {len(ndx)}, spx {len(spx)}"])
            changed = True
            if len(ndx) >= 90 and len(spx) >= 450:
                F["index"] = {"date": today, "ndx": ndx, "spx": spx}
        except Exception as e:
            F["indexError"] = f"{type(e).__name__}: {e}"
            changed = True
    if F.get("date") != today:
        rows = {}
        try:
            import yfinance as yf
            from yfinance import EquityQuery as Q
            q = Q("and", [Q("eq", ["region", "us"]), Q("is-in", ["exchange", "NMS", "NGM", "NCM"])])
            off, total = 0, 1
            while off < total and off < 9000:
                r = yf.screen(q, offset=off, size=250, sortField="intradaymarketcap", sortAsc=False)
                quotes = r.get("quotes") or []
                total = int(r.get("total") or 0)
                for x in quotes:
                    t = x.get("symbol")
                    if not t:
                        continue
                    rows[t] = {"sh": x.get("sharesOutstanding"), "eps": x.get("epsTrailingTwelveMonths"),
                               "cap": x.get("marketCap"), "pe": x.get("trailingPE")}
                if not quotes:
                    break
                off += len(quotes)
                time.sleep(0.5)
        except Exception as e:
            print("fundamentals failed:", e)
        print("fundamentals:", len(rows))
        if len(rows) >= 500:
            F.update(date=today, rows=rows)
            changed = True
    if changed:
        FUND.write_text(json.dumps(F, separators=(",", ":")), encoding="utf-8")
    return F


def cap_pe(t, price, F):
    f = (F.get("rows") or {}).get(t) or {}
    sh, eps = f.get("sh"), f.get("eps")
    cap = sh * price if sh else f.get("cap")
    pe = price / eps if eps and eps > 0 else None
    return (cap / 1e9 if cap else None), pe


def indicators(s):
    """30- and 200-day simple averages (with the latest price), the 14-day average true range of the completed
    days before today in % of the previous close, and the 52-week (252 trading days, today included) low and high."""
    c = s["Close"].astype(float)
    s30 = float(c.tail(30).mean()) if len(c) >= 30 else None
    s200 = float(c.tail(200).mean()) if len(c) >= 200 else None
    p = s.iloc[:-1]
    atr = None
    if len(p) >= 15:
        h, l, pc = p["High"].astype(float), p["Low"].astype(float), p["Close"].astype(float).shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1).dropna().tail(14)
        prev = float(p["Close"].iloc[-1])
        atr = float(tr.mean() / prev * 100) if len(tr) and prev > 0 else None
    y = s.tail(252)
    lo52, hi52 = float(y["Low"].astype(float).min()), float(y["High"].astype(float).max())
    return s30, s200, atr, (lo52 if lo52 > 0 else None), (hi52 if hi52 > 0 else None)


def fix_splits(s):
    """Yahoo sometimes records a split but leaves the older prices unadjusted for a few days, which shows up as a fake
    +2.000 % week after a 1-for-50 reverse split. When the price jump across the split day matches the split ratio
    (rather than "no change"), the earlier prices are adjusted here."""
    if "Stock Splits" not in s:
        return s
    sp = s["Stock Splits"].astype(float).fillna(0)
    ev = sp[(sp > 0) & (sp != 1)]
    if ev.empty:
        return s
    s = s.copy()
    for ts, r in ev.items():
        i = s.index.get_loc(ts)
        if not isinstance(i, int) or i == 0:
            continue
        before = float(s["Close"].iloc[i - 1])
        after = float(s["Open"].iloc[i]) if float(s["Open"].iloc[i]) > 0 else float(s["Close"].iloc[i])
        if not (before > 0 and after > 0 and math.isfinite(before) and math.isfinite(after)):
            continue
        jump = after / before                       # about 1 / ratio when the history is not adjusted yet
        if abs(math.log(jump * r)) < abs(math.log(jump)):
            k = 1 / r
            for c in ("Open", "High", "Low", "Close", "Adj Close"):
                if c in s.columns:
                    s.iloc[:i, s.columns.get_loc(c)] = s[c].iloc[:i].astype(float) * k
            if "Volume" in s.columns:
                s.iloc[:i, s.columns.get_loc("Volume")] = s["Volume"].iloc[:i].astype(float) / k
    return s


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
                try:
                    frames[t] = fix_splits(s)
                except Exception as e:
                    print("split check failed:", t, e)
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


def risk_check(alerts, cache, data=None, F=None):
    """Marks each 20 % mover as lower-risk or risky (with the reasons, in Turkish). `cache` keeps the slow lookups
    (one year of history, market value) per ticker for the day."""
    data, F = data or {}, F or {}
    need = [r["ticker"] for r in alerts if r["ticker"] not in cache]
    miss = [t for t in need if t not in data or len(data[t]) < 200]
    hist = {**{t: data[t] for t in need if t in data}, **(download(miss, period="13mo", interval="1d", actions=True) if miss else {})}
    price = {r["ticker"]: r["price"] for r in alerts}
    for t in need:
        s, c = hist.get(t), {"capB": None, "days": None, "dailyVolPct": None, "reverseSplit": None}
        cap = (cap_pe(t, price[t], F)[0] or 0) * 1e9 or market_cap(t)
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


def gh_text(lines):
    """Issue text: the first line is the title; in the body every $ is escaped, since GitHub shows $...$ as a formula."""
    return "\n".join([lines[0]] + [x.replace("$", "\\$") for x in lines[1:]]) + "\n"


def rebound_signal(s):
    """A one-day fall of 20 %+ 10-60 trading days ago (and none since), then calm: no new 20-day closing low in the last
    10 days, price above its 10- and 20-day averages, last 10 days at least 30 % less volatile than the 10 before."""
    c = s["Close"].astype(float)
    r = c.pct_change()
    n = len(c)
    if n < 80:
        return None
    if (r.iloc[-REBOUND["minAge"]:] <= -REBOUND["crash"]).any():
        return None
    hits = [i for i in range(max(1, n - 1 - REBOUND["maxAge"]), n - REBOUND["minAge"]) if r.iloc[i] <= -REBOUND["crash"]]
    if not hits:
        return None
    i = hits[-1]
    last10, prev10 = c.iloc[-10:], c.iloc[-20:-10]
    v_now, v_prev = float(r.iloc[-10:].std()), float(r.iloc[-20:-10].std())
    price = float(c.iloc[-1])
    if not (last10.min() > prev10.min() and price > c.tail(10).mean() and price > c.tail(20).mean()
            and v_prev > 0 and v_now < v_prev * REBOUND["calm"]):
        return None
    sp = s["Stock Splits"].astype(float).fillna(0).tail(252) if "Stock Splits" in s else pd.Series(dtype=float)
    return {"crashDate": str(s.index[i].date()), "crashPct": round(float(r.iloc[i]) * 100, 1), "ageDays": int(n - 1 - i),
            "preCrash": round(float(c.iloc[i - 1]), 2), "crashClose": round(float(c.iloc[i]), 2),
            "baseLow": round(float(c.tail(10).min()), 2), "calmerPct": round((1 - v_now / v_prev) * 100),
            "reverseSplit": bool(((sp > 0) & (sp < 1)).any())}


def rebound_mail(day, picks, now):
    dt = datetime.strptime(day, "%Y-%m-%d")
    title = ("ÖNEMLİ — Düşüp dengelenen hisse kriterine uydu: "
             + ", ".join(f"{p['ticker']}" for p in picks[:6]) + (" …" if len(picks) > 6 else "") + " (alım adayı)")
    st = {}
    try:
        R = json.loads((ROOT / "rebound_results.json").read_text(encoding="utf-8"))["results"]
        st = {h: R.get(f"crash20_s3|h{h}") or {} for h in (20, 60)}
    except Exception:
        pass
    body = [title, "", f"@{OWNER} {dt:%d.%m.%Y} kapanışına göre şu hisse(ler) senin kriterine uydu: son 10–60 işlem günü "
            "içinde bir günde %20+ düştü, sonra dengelendi (10 gündür yeni dip yok, fiyat 10 ve 20 günlük ortalamanın "
            "üstünde, oynaklık azaldı) ve düşük riskli şirket (fiyat ≥ 10 $, piyasa değeri ≥ 2 milyar $, günde ≥ 20 milyon "
            "$ işlem, son 12 ayda ters bölünme yok).", ""]
    for p in picks:
        h = p.get("headline") or {}
        body.append(f"### {p['ticker']} — {p['name']}")
        dt_c = datetime.strptime(p["crashDate"], "%Y-%m-%d")
        body.append(f"- Düşüş: {dt_c:%d.%m.%Y} günü −%{trn(abs(p['crashPct']))} ({trn(p['preCrash'], 2)} $ → "
                    f"{trn(p['crashClose'], 2)} $), {p['ageDays']} işlem günü önce")
        body.append(f"- Şimdi: {trn(p['price'], 2)} $; 52 haftalık zirveden "
                    + (f"−%{trn(abs(p['ddPct']))}" if p.get("ddPct") is not None else "–")
                    + f"; son 10 günün dibi {trn(p['baseLow'], 2)} $; oynaklık %{p['calmerPct']} azaldı")
        body.append(f"- Piyasa değeri {p['capB']:.1f} milyar $".replace(".", ",")
                    + (f", F/K {p['pe']:.1f}".replace(".", ",") if p.get("pe") else ", şirket zararda (F/K yok)")
                    + f", günde {p['dollarVolM']:.0f} milyon $ işlem")
        stop = p["baseLow"] * 0.97
        body.append(f"- Olası plan: zarar-kes son 10 günün dibinin (%{trn(abs(p['price'] / p['baseLow'] - 1) * 100)} aşağıda, "
                    f"{trn(p['baseLow'], 2)} $) biraz altı: yaklaşık {trn(stop, 2)} $, yani fiyattan "
                    f"−%{trn((1 - stop / p['price']) * 100)}")
        body.append(f"- Robinhood: https://robinhood.com/stocks/{p['ticker']}")
        if h.get("url"):
            body.append(f"- Son haber: [{h.get('title')}]({h.get('url')})")
        body.append("")
    if st.get(20).get("trades") if st.get(20) else False:
        a, b = st[20], st.get(60) or {}
        sp = lambda x: ("+" if x >= 0 else "−") + "%" + trn(abs(x))
        body.append("Bu kuralın geçmişi (2019–2026, aynı filtrelerle, ertesi açılışta alım): "
                    f"20 gün tutulan {a['trades']} işlemde ortalama {sp(a['avgPct'])}, kârlı işlem oranı %{a['winRatePct']:.0f}, "
                    f"aynı günlerde QQQ'ya göre {sp(a['avgExcessPct'])}; 60 günde ortalama {sp(b.get('avgPct', 0))}, "
                    f"QQQ'ya göre {sp(b.get('avgExcessPct', 0))}. Yani bu kural geçmişte piyasayı geçmedi; batan "
                    "şirketler veride olmadığı için gerçek sonuç daha da kötü olabilir.")
        body.append("")
    body.append("Bu bir bildirimdir; yatırım tavsiyesi değildir, karar senin. Kendi araştırmanı yap ve kaybetmeyi göze "
                "alabileceğin kadarla gir. Tüm liste: https://davidmahmut.github.io/portfoy-botu/#hareketliler")
    return gh_text(body)


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
    return gh_text(body)


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
    data = download(sorted(names), period="13mo", interval="1d", actions=True)
    today = now.date().isoformat()
    try:
        F = fundamentals(today)
    except Exception as e:
        print("fundamentals step failed:", e)
        F = {}
    ndx, spx = set((F.get("index") or {}).get("ndx") or []), set((F.get("index") or {}).get("spx") or [])
    rows, screen, rebound = [], [], []
    for t, s in data.items():
        c, v = s["Close"].astype(float), s["Volume"].astype(float)
        last, prev = float(c.iloc[-1]), float(c.iloc[-2])
        if prev <= 0 or last <= 0:
            continue
        dv = float((c.iloc[:-1] * v.iloc[:-1]).tail(20).mean())     # average dollar volume before today
        if prev < MIN_PRICE or last < 1 or not math.isfinite(dv) or dv < MIN_DOLLAR_VOL:
            continue
        w = float(c.iloc[-6]) if len(c) >= 6 else float(c.iloc[0])
        s30, s200, atr, lo52, hi52 = indicators(s)
        if lo52 is not None and hi52 is not None:      # the latest price can sit outside a bar range by rounding
            lo52, hi52 = min(lo52, last), max(hi52, last)
        capb, pe = cap_pe(t, last, F)
        r2 = lambda x, d=2: round(x, d) if x is not None and math.isfinite(x) else None
        screen.append([t, names.get(t, t), r2(last), r2((last / prev - 1) * 100), r2((last / w - 1) * 100), r2(dv / 1e6, 1),
                       r2(capb), r2(pe, 1), r2(s30), r2(s200), r2(atr), ("N" if t in ndx else "") + ("S" if t in spx else ""),
                       CATEGORY.get(t, ""), r2(lo52, 4 if lo52 and lo52 < 1 else 2), r2(hi52, 4 if hi52 and hi52 < 1 else 2)])
        o, hi, lo = (float(s[k].iloc[-1]) for k in ("Open", "High", "Low"))
        gap = o / prev - 1 if o > 0 else 0.0
        if last >= REBOUND["minPrice"] and dv >= REBOUND["minDollarVolM"] * 1e6 and (capb or 0) >= REBOUND["minCapB"]:
            try:
                sig = rebound_signal(s)
            except Exception:
                sig = None
            if sig and not sig.pop("reverseSplit") and not any(x.lower() in names.get(t, "").lower() for x in SANCTIONED_NAMES):
                rebound.append({"ticker": t, "name": names.get(t, t), "price": round(last, 2), "date": str(s.index[-1].date()),
                                "capB": round(capb, 2), "pe": round(pe, 1) if pe else None, "dollarVolM": round(dv / 1e6, 1),
                                "ddPct": round((last / hi52 - 1) * 100, 1) if hi52 else None, **sig})
        rows.append({"ticker": t, "name": names.get(t, t), "price": round(last, 2), "prevClose": round(prev, 2),
                     "d1Pct": round((last / prev - 1) * 100, 2), "w1Pct": round((last / w - 1) * 100, 2),
                     "dollarVolM": round(dv / 1e6, 1), "date": str(s.index[-1].date()), "gapPct": round(gap * 100, 2),
                     # opened 15 %+ higher and then barely moved: the typical picture of a takeover at a fixed price
                     "buyout": bool(gap > 0.15 and last > prev and (hi - lo) / last < 0.03)})
    # Yahoo sometimes has no bar yet for the latest session in some tickers: their "daily" change would be yesterday's,
    # so only stocks with a bar for the latest session count for the daily lists, the 20 % alerts and the e-mail.
    bar_day = max((r["date"] for r in rows), default=now.date().isoformat())
    fresh = [r for r in rows if r["date"] == bar_day]
    stale = {r["ticker"] for r in rows if r["date"] != bar_day}
    for row in screen:
        if row[0] in stale:
            row[3] = None
    by = lambda k, rev, src=rows: sorted(src, key=lambda r: r[k], reverse=rev)[:TOP_N]
    alerts = [r for r in fresh if abs(r["d1Pct"]) >= ALERT * 100]
    alerts.sort(key=lambda r: -abs(r["d1Pct"]))
    for r in alerts[:30]:
        r["headline"] = headline(r["ticker"])
    old = json.loads((ROOT / "movers.json").read_text()) if (ROOT / "movers.json").exists() else {}
    cache = dict((old.get("riskCache") or {}).get(today) or {})
    try:
        risk_check(alerts, cache, data, F)
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
    rebound = [p for p in rebound if p["date"] == bar_day]
    reb_sent = {k: v for k, v in (old.get("reboundAlerted") or {}).items()
                if (datetime.fromisoformat(today) - datetime.fromisoformat(v)).days <= 150}
    reb_new = [p for p in rebound if reb_sent.get(p["ticker"]) != p["crashDate"]] if closed else []
    for p in reb_new:
        p["headline"] = headline(p["ticker"])
        reb_sent[p["ticker"]] = p["crashDate"]
    reb_mail = rebound_mail(bar_day, reb_new, now) if reb_new else None
    mail = None
    if closed and old.get("mailedDay") != bar_day:
        mail = summary_mail(bar_day, alerts, study, now)
    hist = dict(old.get("spikeHistory") or {})
    d = max((r["date"] for r in rows), default=None)  # the trader reads this the next morning (sert yükseliş short'u)
    if d and (closed or not (hist.get(d) or {}).get("final")):   # an after-close run always has the final word
        hist[d] = {"final": closed or d < today, "at": now.isoformat(timespec="minutes"),
                   "rows": [{k: r.get(k) for k in ("ticker", "name", "price", "prevClose", "d1Pct", "gapPct", "dollarVolM",
                                                   "buyout", "headline")}
                            for r in alerts if r["date"] == d]}
    hist = {d: hist[d] for d in sorted(hist)[-15:]}
    out = {"asOf": now.isoformat(timespec="minutes"), "session": "kapanış" if closed else "açık",
           "universeSize": len(names), "screened": len(rows), "minPrice": MIN_PRICE, "minDollarVolM": MIN_DOLLAR_VOL / 1e6,
           "barDay": bar_day, "staleTickers": len(stale),
           "dailyLosers": by("d1Pct", False, fresh), "dailyGainers": by("d1Pct", True, fresh),
           "weeklyLosers": by("w1Pct", False), "weeklyGainers": by("w1Pct", True), "alerts": alerts,
           "alerted": {today: sorted(sent | {r["ticker"] for r in alerts})}, "spikeHistory": hist,
           "riskCache": {today: cache}, "lowRisk": LOW_RISK,
           "mailedDay": bar_day if closed else old.get("mailedDay"),
           "rebound": rebound, "reboundAlerted": reb_sent, "reboundRule": REBOUND,
           "study": {**study, "text": study_text(study)} if study else None,
           "screen": {"cols": SCREEN_COLS, "rows": screen, "fundamentalsDate": F.get("date"),
                      "indexDate": (F.get("index") or {}).get("date")},
           "notes": "Nasdaq'ta işlem gören hisse senetleri (ETF, varant, imtiyazlı hisse hariç); fiyatı 3 $'ın, son 20 günlük "
                    "ortalama işlem hacmi 2 milyon $'ın altındakiler elendi. Günlük değişim önceki kapanışa göre (seans "
                    "açıkken anlık fiyatla), haftalık değişim 5 işlem günü öncesine göre. Bot bu listeden yalnızca "
                    "\"sert yükseliş short'u\" kuralına uyanlara ertesi sabah girer."}
    (ROOT / "movers.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    rm = ROOT / "rebound_new.md"
    if reb_mail:
        rm.write_text(reb_mail, encoding="utf-8")
    elif rm.exists():
        rm.unlink()
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
