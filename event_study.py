"""Research only (never trades): can the bot profit from news shocks in both directions?

A news shock is approximated by a large opening gap (most gaps of 3 %+ in large caps come from earnings, guidance,
downgrades or other news). The bot waits for the first hour (09:30-10:30 New York time) to see which way the
market takes the news, then trades in that direction: short if the stock keeps falling (or fades a gap up),
long if it recovers (or keeps rising). Stop at the other end of the first-hour range.

Runs on GitHub Actions (.github/workflows/event.yml) with Yahoo Finance hourly bars for the last ~730 days.
Writes event_results.json. Rules modelled: SEC Rule 201 (no new short after a 10 % fall today or yesterday),
0.15 % round-trip costs, entry at the first-hour close, stops filled at the stop or a worse open.
"""
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import universe as U

ROOT = Path(__file__).resolve().parent
EXTRA = U.EVENT_EXTRA
COST = 0.0015           # round trip: slippage + commission
SSR = 0.10
GAPS = (0.03, 0.05, 0.08)
MODES = ("follow", "cont", "rev")
HOLDS = (0, 2, 4)        # extra days after the entry day (0 = close the same day)
MIN_MOVE = 0.005


def tickers():
    base = [t for t in U.UNIVERSE if t not in U.ETFS]
    return sorted(set(base) | set(EXTRA))


def load(ts):
    import yfinance as yf
    out = {}
    for i in range(0, len(ts), 40):
        chunk = ts[i:i + 40]
        for attempt in range(3):
            try:
                df = yf.download(chunk, period="730d", interval="1h", group_by="ticker", auto_adjust=False,
                                 prepost=False, progress=False, threads=True)
                break
            except Exception as e:
                print("retry", e)
                time.sleep(10)
        for t in chunk:
            try:
                s = df[t].dropna(subset=["Open", "High", "Low", "Close"])
            except Exception:
                continue
            if len(s) > 500:
                idx = pd.DatetimeIndex(s.index)
                idx = idx.tz_convert("America/New_York") if idx.tz is not None else idx.tz_localize("UTC").tz_convert("America/New_York")
                s = s.copy()
                s.index = idx
                out[t] = s[["Open", "High", "Low", "Close"]].astype(float)
        time.sleep(2)
    return out


def days_of(bars):
    """List of (date, DataFrame of that day's hourly bars) with a 09:30 first bar."""
    out = []
    for d, g in bars.groupby(bars.index.date):
        if len(g) >= 5 and g.index[0].hour == 9 and g.index[0].minute == 30:
            out.append((d, g))
    return out


def events_for(t, bars):
    days = days_of(bars)
    rows = []
    for k in range(2, len(days)):
        d, g = days[k]
        pc = float(days[k - 1][1]["Close"].iloc[-1])
        ppc = float(days[k - 2][1]["Close"].iloc[-1])
        y_low = float(days[k - 1][1]["Low"].min())
        o = float(g["Open"].iloc[0])
        gap = o / pc - 1
        if abs(gap) < min(GAPS):
            continue
        h1 = g.iloc[0]
        h1c, h1h, h1l = float(h1["Close"]), float(h1["High"]), float(h1["Low"])
        move = h1c / o - 1
        if abs(move) < MIN_MOVE:
            continue
        side = 1 if move > 0 else -1
        ssr = h1l <= pc * (1 - SSR) or y_low <= ppc * (1 - SSR)
        base = {"t": t, "d": str(d), "gap": gap, "move": move, "side": side, "cont": (side > 0) == (gap > 0),
                "ssr": ssr, "entry": h1c, "stop": h1l * 0.999 if side > 0 else h1h * 1.001}
        for hold in HOLDS:
            if k + hold >= len(days):
                continue
            seq = [g.iloc[1:]] + [days[k + j][1] for j in range(1, hold + 1)]
            path = pd.concat(seq)
            exit_px, why = None, "close"
            for _, b in path.iterrows():
                if side > 0 and b["Low"] <= base["stop"]:
                    exit_px, why = min(float(b["Open"]), base["stop"]), "stop"
                    break
                if side < 0 and b["High"] >= base["stop"]:
                    exit_px, why = max(float(b["Open"]), base["stop"]), "stop"
                    break
            if exit_px is None:
                exit_px = float(path["Close"].iloc[-1]) if len(path) else h1c
            r = side * (exit_px / h1c - 1) - COST
            rows.append({**base, "hold": hold, "ret": r, "why": why, "riskPct": abs(h1c / base["stop"] - 1)})
    return rows


def summarize(tr):
    if not tr:
        return {"trades": 0}
    r = np.array([x["ret"] for x in tr])
    w, l = r[r > 0], r[r <= 0]
    return {"trades": int(len(r)), "winRatePct": round(float(len(w) / len(r) * 100), 1),
            "avgPct": round(float(r.mean() * 100), 3), "medianPct": round(float(np.median(r) * 100), 3),
            "avgWinPct": round(float(w.mean() * 100), 2) if len(w) else None,
            "avgLossPct": round(float(l.mean() * 100), 2) if len(l) else None,
            "profitFactor": round(float(w.sum() / -l.sum()), 2) if len(l) and l.sum() < 0 else None,
            "stopsPct": round(sum(1 for x in tr if x["why"] == "stop") / len(tr) * 100, 1),
            "tStat": round(float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))), 2) if len(r) > 2 and r.std() > 0 else None}


def portfolio(tr, risk=0.01, cap=0.25, slots=4, start=3000.0):
    """Sleeve simulation: up to `slots` open trades, each sized to lose about `risk` of equity at the stop
    (never more than `cap` of equity); trades taken in order of gap size each day."""
    by_day = {}
    for x in tr:
        by_day.setdefault(x["d"], []).append(x)
    eq, peak, mdd, open_until, curve = start, start, 0.0, [], []
    for d in sorted(by_day):
        open_until = [u for u in open_until if u > d]
        for x in sorted(by_day[d], key=lambda z: -abs(z["gap"])):
            if len(open_until) >= slots:
                break
            w = min(cap, risk / max(x["riskPct"], 0.002))
            eq += eq * w * x["ret"]
            exit_day = (pd.Timestamp(d) + pd.tseries.offsets.BDay(x["hold"])).date().isoformat()
            open_until.append(exit_day if x["hold"] else d + "~")
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        curve.append((d, round(eq, 2)))
    yrs = max((pd.Timestamp(curve[-1][0]) - pd.Timestamp(curve[0][0])).days / 365.25, 0.5) if curve else 1
    return {"endValue": round(eq, 2), "returnPct": round((eq / start - 1) * 100, 2),
            "cagrPct": round(((eq / start) ** (1 / yrs) - 1) * 100, 2), "maxDrawdownPct": round(mdd * 100, 2),
            "curve": curve[-260:]}


def main():
    ts = tickers()
    data = load(ts)
    print("loaded", len(data), "of", len(ts))
    allrows = []
    for t, b in data.items():
        try:
            allrows += events_for(t, b)
        except Exception as e:
            print("skip", t, e)
    dates = sorted({x["d"] for x in allrows})
    mid = dates[len(dates) // 2] if dates else "2025-10-30"
    res = []
    for g in GAPS:
        for mode in MODES:
            for hold in HOLDS:
                sel = [x for x in allrows if abs(x["gap"]) >= g and x["hold"] == hold
                       and (mode == "follow" or (mode == "cont") == x["cont"])]
                legal = [x for x in sel if not (x["side"] < 0 and x["ssr"])]
                item = {"gap": g, "mode": mode, "hold": hold, "all": summarize(legal),
                        "firstHalf": summarize([x for x in legal if x["d"] < mid]),
                        "secondHalf": summarize([x for x in legal if x["d"] >= mid]),
                        "long": summarize([x for x in legal if x["side"] > 0]),
                        "short": summarize([x for x in legal if x["side"] < 0]),
                        "gapDown": summarize([x for x in legal if x["gap"] < 0]),
                        "gapUp": summarize([x for x in legal if x["gap"] > 0]),
                        "blockedBySSR": len(sel) - len(legal)}
                p = portfolio(legal)
                item["sleeve"] = {k: v for k, v in p.items() if k != "curve"}
                item["sleeveFirstHalf"] = {k: v for k, v in portfolio([x for x in legal if x["d"] < mid]).items() if k != "curve"}
                item["sleeveSecondHalf"] = {k: v for k, v in portfolio([x for x in legal if x["d"] >= mid]).items() if k != "curve"}
                res.append(item)
                print(g, mode, hold, item["all"].get("trades"), item["all"].get("avgPct"), item["all"].get("profitFactor"),
                      item["sleeve"]["returnPct"], item["sleeve"]["maxDrawdownPct"])
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "tickers": len(data),
           "from": dates[0] if dates else None, "to": dates[-1] if dates else None, "split": mid,
           "eventsRaw": len({(x["t"], x["d"]) for x in allrows}), "variants": res,
           "recent": sorted([x for x in allrows if x["hold"] == 0], key=lambda z: z["d"])[-25:],
           "notes": "Saatlik Yahoo Finance verisi. Haber şoku = açılışta en az %3/5/8 boşluk. İlk saatin yönünde işlem "
                    "(follow), yalnızca boşluk yönünde devam (cont) ya da yalnızca tersine dönüş (rev). Zarar-kes ilk saatin "
                    "öbür ucu. SEC Rule 201 nedeniyle açılamayan short'lar çıkarıldı. Gidiş-dönüş maliyet %0,15."}
    (ROOT / "event_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
