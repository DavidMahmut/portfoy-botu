"""News feed and automatic daily brief for the dashboard (reading only, never trades).

For every stock the bot holds or watches, plus the broad market, it collects recent Yahoo Finance headlines
(yfinance, with the Yahoo RSS feed as a fallback) and tags them by theme. It also writes a short English daily
brief built from market data, the bot's ledger and the headlines - rule-based, no AI. Output: news.json.
Runs from .github/workflows/watch.yml every 30 minutes, 04:00-20:00 New York time.
"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "news.json"
MAX_AGE_H, PER_TICKER, MARKET_N = 72, 4, 12
MARKET = ["SPY", "QQQ", "^GSPC", "^TNX", "CL=F"]
TAGS = [
    ("Fed & rates", r"\bfed\b|federal reserve|powell|rate (hike|cut)s?|interest rates?|inflation|\bcpi\b|\bpce\b|treasur|bond yields?|yields? (rose|fell|jump|climb|drop|surge)|fomc|jobs report|payroll"),
    ("Policy & politics", r"tariff|trump|white house|congress|senate|sanction|election|export (control|ban)|beijing|china|government|regulat|antitrust|lawsuit|doj|\bsec\b"),
    ("Earnings", r"earnings|revenue|guidance|quarter|results|profit|\beps\b|outlook|forecast"),
    ("Analysts", r"downgrade|upgrade|price target|rating|analyst|overweight|underweight|neutral|buy rating"),
    ("Energy", r"\boil\b|crude|opec|brent|natural gas|energy prices|houthi"),
    ("AI & chips", r"\bai\b|artificial intelligence|data center|nvidia|\bgpu|chip|semiconductor|openai"),
    ("Corporate", r"acqui|merger|deal|buyback|layoff|ceo|insider|partnership|contract"),
]


MACRO_TAGS = {"Fed & rates", "Policy & politics", "Energy"}
SNAPSHOT = [("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("DIA", "Dow"), ("IWM", "Russell 2000"), ("^VIX", "VIX"),
            ("^TNX", "10-yr Treasury yield"), ("CL=F", "WTI crude"), ("GC=F", "Gold")]


def tags_for(text):
    t = text.lower()
    return [name for name, rx in TAGS if re.search(rx, t)]


def _parse_time(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(str(v))
        except Exception:
            return None


def normalize(item):
    """yfinance has used two news formats; return {title, summary, publisher, url, at} or None."""
    c = item.get("content") if isinstance(item.get("content"), dict) else item
    title = c.get("title")
    url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
           or c.get("link") or item.get("link"))
    if not title or not url or not str(url).startswith("http"):
        return None
    pub = (c.get("provider") or {}).get("displayName") or c.get("publisher") or item.get("publisher") or "Yahoo Finance"
    at = _parse_time(c.get("pubDate") or c.get("displayTime") or c.get("providerPublishTime") or item.get("providerPublishTime"))
    summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c.get("summary") or c.get("description") or "")).strip()
    return {"title": title.strip(), "summary": summary[:260] + ("…" if len(summary) > 260 else ""), "publisher": pub,
            "url": url, "at": at}


def fetch_yf(t):
    try:
        import yfinance as yf
        return [n for n in (normalize(x) for x in (yf.Ticker(t).news or [])) if n]
    except Exception as e:
        print("yfinance news failed:", t, e)
        return []


def fetch_rss(t):
    try:
        import requests
        r = requests.get("https://feeds.finance.yahoo.com/rss/2.0/headline",
                         params={"s": t, "region": "US", "lang": "en-US"}, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0 portfoy-botu"})
        root = ET.fromstring(r.content)
        out = []
        for it in root.iter("item"):
            n = normalize({"title": it.findtext("title"), "link": it.findtext("link"),
                           "description": it.findtext("description"), "pubDate": it.findtext("pubDate"),
                           "publisher": "Yahoo Finance"})
            if n:
                out.append(n)
        return out
    except Exception as e:
        print("rss failed:", t, e)
        return []


def collect(t, now, limit, seen):
    items = fetch_yf(t) or fetch_rss(t)
    fresh = []
    for n in sorted(items, key=lambda x: x["at"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True):
        key = n["url"].split("?")[0]
        if key in seen or (n["at"] and now - n["at"] > timedelta(hours=MAX_AGE_H)):
            continue
        seen.add(key)
        n["tags"] = tags_for(n["title"] + " " + n["summary"])
        n["at"] = n["at"].astimezone(NY).isoformat(timespec="minutes") if n["at"] else None
        fresh.append(n)
        if len(fresh) >= limit:
            break
    return fresh


def subjects(ledger):
    """(ticker, name, role, dayChangePct) for held positions first, then the daily watchlist."""
    out, seen = [], set()
    checks = ledger.get("checks", {})
    last = checks[sorted(checks)[-1]][-1] if checks else {}
    day = {p["ticker"]: p.get("dayChangePct") for p in last.get("positions", [])}
    for p in ledger["portfolio"]["positions"]:
        if p["ticker"] not in seen:
            seen.add(p["ticker"])
            out.append((p["ticker"], p.get("name", p["ticker"]), p["side"], day.get(p["ticker"]), p.get("unrealizedPct")))
    runs = [r for r in ledger.get("runs", []) if not r.get("cancelled")]
    if runs:
        for w in runs[-1].get("watchlist", []):
            t = w["ticker"]
            if t not in seen:
                seen.add(t)
                role = "watch-short" if "Short" in w.get("note", "") else "watch-long"
                out.append((t, t, role, None, None))
    return out


def pc(x):
    return f"{x:+.2f}%".replace("+-", "-")


def first_sentence(text, n=200):
    t = re.split(r"(?<=[.!?])\s", text or "", maxsplit=1)[0]
    return t[:n] + ("…" if len(t) > n else "")


def market_snapshot(data_mod):
    try:
        d = data_mod.daily([t for t, _ in SNAPSHOT], period="1y")
    except Exception as e:
        print("snapshot failed:", e)
        return {}, None
    snap = {}
    for t, label in SNAPSHOT:
        h = d.get(t)
        if h is not None and len(h) >= 2:
            snap[t] = (label, float(h["Close"].iloc[-1]), float(h["Close"].iloc[-2]))
    spy = d.get("SPY")
    sma200 = float(spy["Close"].iloc[-201:-1].mean()) if spy is not None and len(spy) > 201 else None
    return snap, sma200


def upcoming_earnings(tickers, today, days=21):
    out = []
    try:
        import yfinance as yf
    except Exception:
        return out
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            for d in dates or []:
                d = d.date() if hasattr(d, "date") else d
                if today <= d <= today + timedelta(days=days):
                    out.append({"ticker": t, "date": d.isoformat()})
                    break
        except Exception as e:
            print("calendar failed:", t, e)
    return sorted(out, key=lambda x: x["date"])


def build_digest(ledger, sections, market, snap, sma200, earnings, now):
    ny = now.astimezone(NY)
    today = ny.date().isoformat()
    bullets = []
    if snap:
        def chg(t):
            label, last, prev = snap[t]
            return (last / prev - 1) * 100
        idx = [f"{snap[t][0]} {pc(chg(t))}" for t in ("SPY", "QQQ", "DIA", "IWM") if t in snap]
        extra = []
        if "^VIX" in snap:
            extra.append(f"VIX {snap['^VIX'][1]:.1f}")
        if "^TNX" in snap:
            bp = (snap["^TNX"][1] - snap["^TNX"][2]) * 100
            extra.append(f"10-yr yield {snap['^TNX'][1]:.2f}% ({bp:+.0f} bp)")
        if "CL=F" in snap:
            extra.append(f"WTI ${snap['CL=F'][1]:.2f} ({pc(chg('CL=F'))})")
        if "GC=F" in snap:
            extra.append(f"gold ${snap['GC=F'][1]:,.0f} ({pc(chg('GC=F'))})")
        notes = []
        if "QQQ" in snap and "DIA" in snap:
            gap = chg("QQQ") - chg("DIA")
            if gap <= -1:
                notes.append("Tech lagged the Dow: money rotated out of technology.")
            elif gap >= 1:
                notes.append("Tech led the market.")
        if "^VIX" in snap and snap["^VIX"][1] >= 20:
            notes.append("Volatility is " + ("high" if snap["^VIX"][1] >= 25 else "elevated") + " (VIX above 20).")
        if "^TNX" in snap and abs((snap["^TNX"][1] - snap["^TNX"][2]) * 100) >= 8:
            up = snap["^TNX"][1] > snap["^TNX"][2]
            notes.append("Bond yields jumped, which usually weighs on growth stocks." if up else
                         "Bond yields fell sharply, which usually helps growth stocks.")
        bullets.append({"head": "Markets (latest session)", "text": ", ".join(idx) + ". " + "; ".join(extra) + ". " + " ".join(notes)})
        if sma200 and "SPY" in snap:
            spy = snap["SPY"][1]
            gap = (spy / sma200 - 1) * 100
            bullets.append({"head": "Trend filter", "text": (
                f"The S&P 500 (SPY ${spy:.2f}) is {abs(gap):.1f}% {'above' if gap >= 0 else 'below'} its 200-day average "
                f"(${sma200:.2f}). " + ("Rule: the bot stays invested." if gap >= 0 else
                                         "Rule: the bot moves to cash until SPY is back above the average."))})
    P = ledger["portfolio"]
    prev = [x for x in ledger.get("snapshots", []) if x["date"] < today]
    day_txt = ""
    if prev and prev[-1].get("totalValue"):
        dep_change = (P.get("netDeposits") or 0) - (prev[-1].get("netDeposits") or P.get("netDeposits") or 0)
        base = prev[-1]["totalValue"] + dep_change
        day_txt = f" ({pc((P['totalValue'] / base - 1) * 100)} vs the previous close)" if base else ""
    held = [x for x in sections if x["role"] in ("long", "short") and x.get("dayChangePct") is not None]
    mv = ""
    if held:
        best = max(held, key=lambda x: x["dayChangePct"])
        worst = min(held, key=lambda x: x["dayChangePct"])
        mv = f" Best holding today: {best['ticker']} {pc(best['dayChangePct'])}; weakest: {worst['ticker']} {pc(worst['dayChangePct'])}."
    bullets.append({"head": "Our portfolio", "text": f"Net value ${P['totalValue']:,.2f}{day_txt}; cash ${P['cash']:,.2f}; "
                    f"{len(P['positions'])} positions, leverage {P.get('leverage') or 0:.2f}x.{mv}"})
    counts = {}
    todays = [n for s_ in sections for n in s_["items"]] + market
    for n in todays:
        if (n.get("at") or "")[:10] == today:
            for t in n["tags"]:
                counts[t] = counts.get(t, 0) + 1
    if counts:
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
        bullets.append({"head": "Themes in today's headlines", "text": ", ".join(f"{k} ({v})" for k, v in top) + "."})
    if earnings:
        bullets.append({"head": "Upcoming earnings (next 3 weeks)", "text": ", ".join(
            f"{e['ticker']} ({datetime.fromisoformat(e['date']).strftime('%b %-d')})" for e in earnings) + "."})
    macro = [n for n in market if set(n["tags"]) & MACRO_TAGS][:4] or market[:3]
    headlines = [{"title": n["title"], "url": n["url"], "publisher": n["publisher"], "at": n["at"],
                  "summary": first_sentence(n["summary"]), "tags": n["tags"]} for n in macro]
    movers = []
    for x in sorted(held, key=lambda x: -abs(x["dayChangePct"]))[:3]:
        n = x["items"][0] if x["items"] else None
        movers.append({"ticker": x["ticker"], "changePct": x["dayChangePct"], "title": n["title"] if n else None,
                       "url": n["url"] if n else None, "publisher": n["publisher"] if n else None})
    return {"date": today, "title": "Daily brief — " + ny.strftime("%A, %B %-d, %Y"), "generatedAt": ny.isoformat(timespec="minutes"),
            "bullets": bullets, "headlines": headlines, "movers": movers,
            "note": "Generated automatically from market data, the bot's ledger and the headline feeds (rule-based, no AI). "
                    "Not investment advice."}


def main():
    now = datetime.now(timezone.utc)
    ledger = json.loads((ROOT / "ledger.json").read_text(encoding="utf-8"))
    import universe as U
    seen, sections = set(), []
    for t, name, role, chg, upct in subjects(ledger):
        sections.append({"ticker": t, "name": U.name_of(t) if name == t else name, "role": role, "dayChangePct": chg,
                         "unrealizedPct": upct, "items": collect(t, now, PER_TICKER, seen)})
    market = []
    for t in MARKET:
        market += collect(t, now, MARKET_N, seen)
    # economy and policy headlines first, then the rest, newest first within each
    market.sort(key=lambda n: (bool(set(n["tags"]) & MACRO_TAGS), n["at"] or ""), reverse=True)
    market = market[:MARKET_N]
    import data
    snap, sma200 = market_snapshot(data)
    held = [s_["ticker"] for s_ in sections if s_["role"] in ("long", "short") and s_["ticker"] not in U.ETFS]
    earnings = upcoming_earnings(held, now.astimezone(NY).date())
    out = {"asOf": now.astimezone(NY).isoformat(timespec="seconds"), "sections": sections, "market": market,
           "digest": build_digest(ledger, sections, market, snap, sma200, earnings, now),
           "source": {"title": "Yahoo Finance news feed (yfinance / RSS)", "url": "https://finance.yahoo.com/news/"},
           "note": "Headlines and summaries come straight from the publishers. Theme tags are automatic keyword matches. "
                   "Not investment advice."}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n = sum(len(s["items"]) for s in sections) + len(out["market"])
    print(f"{len(sections)} tickers, {n} headlines, digest bullets: {len(out['digest']['bullets'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
