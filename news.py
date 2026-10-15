"""Headline collector for the dashboard's news section (reading only, never trades).

For every stock the bot holds or watches, plus the broad market, it collects recent Yahoo Finance headlines
(yfinance, with the Yahoo RSS feed as a fallback), tags them by theme (Fed and rates, politics, earnings,
analysts, energy, AI) and writes news.json. Runs from .github/workflows/watch.yml.
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
MAX_AGE_H, PER_TICKER, MARKET_N = 72, 4, 10
MARKET = ["SPY", "QQQ", "^GSPC", "^TNX", "CL=F"]
TAGS = [
    ("Fed ve faiz", r"\bfed\b|federal reserve|powell|rate (hike|cut)|interest rate|inflation|\bcpi\b|\bpce\b|treasury|yield"),
    ("Politika", r"tariff|trump|white house|congress|senate|sanction|election|export (control|ban)|beijing|china|government|regulat|antitrust|lawsuit|doj|\bsec\b"),
    ("Bilanço", r"earnings|revenue|guidance|quarter|results|profit|\beps\b|outlook|forecast"),
    ("Analist", r"downgrade|upgrade|price target|rating|analyst|overweight|underweight|neutral|buy rating"),
    ("Enerji", r"\boil\b|crude|opec|brent|natural gas|energy prices|houthi"),
    ("Yapay zekâ", r"\bai\b|artificial intelligence|data center|nvidia|\bgpu|chip|semiconductor|openai"),
    ("Şirket", r"acqui|merger|deal|buyback|layoff|ceo|insider|partnership|contract"),
]


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
    market.sort(key=lambda n: n["at"] or "", reverse=True)
    out = {"asOf": now.astimezone(NY).isoformat(timespec="seconds"), "sections": sections, "market": market[:MARKET_N],
           "source": {"title": "Yahoo Finance haber akışı (yfinance / RSS)", "url": "https://finance.yahoo.com/news/"},
           "note": "Başlıklar haber kaynaklarının kendi diliyle (İngilizce) ve özetleriyle gösterilir; etiketler otomatik "
                   "anahtar kelime eşleşmesidir. Yatırım tavsiyesi değildir."}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n = sum(len(s["items"]) for s in sections) + len(out["market"])
    print(f"{len(sections)} hisse, {n} başlık")
    return 0


if __name__ == "__main__":
    sys.exit(main())
