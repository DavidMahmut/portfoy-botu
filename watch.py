"""Display-only watch of pre-market and after-hours prices for the open positions.

Runs on its own schedule outside regular trading hours (.github/workflows/watch.yml) and writes
extended.json for the dashboard. It never trades and never touches ledger.json: the bot's orders and
stop-losses work only in the regular session (09:30-16:00 New York time).
"""
import json
import os
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "extended.json"
MARKET = [("SPY", "S&P 500 (SPY)"), ("QQQ", "Nasdaq 100 (QQQ)")]
SESSIONS = {"pre": "Piyasa öncesi (04:00–09:30 ET)", "post": "Piyasa sonrası (16:00–20:00 ET)"}
NOTE = ("Yalnızca izleme: bot bu saatlerde işlem yapmaz, zarar-kes emirleri normal seansta işler. Seans dışında "
        "işlem hacmi düşüktür, fiyatlar sert oynayabilir ve ertesi açılış farklı olabilir.")


def session_of(now, holidays):
    if now.weekday() >= 5 or now.date().isoformat() in holidays:
        return None
    t = now.time()
    if dtime(4, 0) <= t < dtime(9, 30):
        return "pre"
    if dtime(16, 0) <= t <= dtime(20, 5):
        return "post"
    return None


def _iso(ts):
    return ts.tz_convert(NY).isoformat(timespec="seconds")


def move(bars):
    """(last regular-session close, its time, latest extended-hours price, its time) or None."""
    if bars is None or bars.empty:
        return None
    times = bars.index.time
    reg = bars[(times >= dtime(9, 30)) & (times < dtime(16, 0))]
    if reg.empty:
        return None
    ref_ts = reg.index[-1]
    ext = bars[bars.index > ref_ts]
    if ext.empty:
        return float(reg["Close"].iloc[-1]), ref_ts, None, None
    return float(reg["Close"].iloc[-1]), ref_ts, float(ext["Close"].iloc[-1]), ext.index[-1]


def build(ledger, bars, now, session):
    P = ledger["portfolio"]
    items, total = [], 0.0
    for p in P["positions"]:
        m = move(bars.get(p["ticker"]))
        row = {"ticker": p["ticker"], "name": p.get("name", p["ticker"]), "side": p["side"], "lev": p.get("lev", 1),
               "shares": p["shares"], "stopPrice": p.get("stopPrice")}
        if m:
            ref, ref_ts, px, px_ts = m
            row.update(refClose=round(ref, 4), refAt=_iso(ref_ts))
            if px is not None:
                sign = 1 if p["side"] == "long" else -1
                impact = p["shares"] * (px - ref) * sign
                total += impact
                stop = p.get("stopPrice")
                beyond = bool(stop) and ((p["side"] == "long" and px <= stop) or (p["side"] == "short" and px >= stop))
                row.update(price=round(px, 4), at=_iso(px_ts), changePct=round((px / ref - 1) * 100, 2),
                           impact=round(impact, 2), beyondStop=beyond)
        items.append(row)
    market = []
    for t, label in MARKET:
        m = move(bars.get(t))
        if m and m[2] is not None:
            market.append({"ticker": t, "label": label, "refClose": round(m[0], 2), "price": round(m[2], 2),
                           "at": _iso(m[3]), "changePct": round((m[2] / m[0] - 1) * 100, 2)})
    eq = P.get("totalValue") or 0
    return {"asOf": now.isoformat(timespec="seconds"), "session": session, "sessionLabel": SESSIONS.get(session, ""),
            "equity": eq, "totalImpact": round(total, 2), "totalImpactPct": round(total / eq * 100, 2) if eq else None,
            "items": items, "market": market, "note": NOTE,
            "source": {"title": "Yahoo Finance 5 dakikalık seans dışı mumlar (yfinance, prepost)",
                       "url": "https://github.com/ranaroussi/yfinance"}}


def main():
    import trader
    now = datetime.fromisoformat(os.environ["BOT_NOW"]).astimezone(NY) if os.environ.get("BOT_NOW") else datetime.now(NY)
    session = session_of(now, trader.HOLIDAYS)
    if not session:
        if os.environ.get("BOT_FORCE") != "1":
            print(f"{now:%Y-%m-%d %H:%M} ET: seans dışı izleme saati değil.")
            return 0
        session = "post" if now.time() >= dtime(12, 0) else "pre"
    import data
    ledger = json.loads((ROOT / "ledger.json").read_text(encoding="utf-8"))
    tickers = sorted({p["ticker"] for p in ledger["portfolio"]["positions"]} | {t for t, _ in MARKET})
    out = build(ledger, data.extended(tickers), now, session)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{now:%H:%M} ET {session}: tahmini etki {out['totalImpact']} $ ({len(out['items'])} pozisyon)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
