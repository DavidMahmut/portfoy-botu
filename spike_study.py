"""Research only (never trades): can the bot make money by shorting Nasdaq stocks right after a 20 % day?

Two short ideas, tested on daily bars since mid-2018 for today's Nasdaq-listed common stocks:
  fadeUp     - a stock closes 20 %+ up: short it (at that day's close, or the next morning's open), stop above the entry,
               cover after a few days. Idea: news spikes in small stocks tend to give part of the jump back.
  crashShort - a stock closes 20 %+ down: SEC Rule 201 blocks new shorts for the rest of that day and the next one, so the
               short is opened at the open two days later (skipped if the stock fell 10 % again in between, which renews
               the restriction). Idea: big crashes tend to keep falling.
  crashLong  - for comparison only: buy the next morning after a 20 % crash.

Every trade pays a 0.4 % round trip (spread + commission + slippage), 1 % extra slippage when a stop fills, and a short
borrow fee (30 %/year by default, also shown at 100 %/year, since freshly spiked small stocks are often hard to borrow).
Survivorship bias: delisted companies are missing. That makes the crash-short and fade results look somewhat worse than
they really were (bankruptcies would have been winners for a short) and crash-buying look better.

Runs on GitHub Actions (.github/workflows/spike_study.yml); writes spike_results.json.
"""
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import movers as MV

ROOT = Path(__file__).resolve().parent
START, MID = "2019-01-01", "2023-01-01"
COST, STOP_SLIP = 0.004, 0.01
BORROW, BORROW_HIGH = 0.30, 1.00
THRESH = (0.20, 0.30, 0.50)
STOPS = (0.10, 0.20, 0.30, None)
HOLDS = (1, 3, 5, 10)                 # trading days from the entry day (1 = cover at the close of the entry day)
FILTERS = {"p5_dv5": (5.0, 5e6), "p5_dv20": (5.0, 20e6), "p3_dv2": (3.0, 2e6)}
LOOK = max(HOLDS) + 3


def collect(data):
    """One record per 20 % day with the next LOOK daily bars."""
    out = []
    for t, s in data.items():
        try:
            o, h, l, c, v = (s[k].astype(float).values for k in ("Open", "High", "Low", "Close", "Volume"))
        except Exception:
            continue
        idx = [str(x.date()) for x in s.index]
        n = len(c)
        dv = pd.Series(np.nan_to_num(c * v)).rolling(20, min_periods=20).mean().values
        for i in range(21, n - 2):
            prev = c[i - 1]
            if not (np.isfinite(prev) and prev > 0 and np.isfinite(c[i]) and c[i] > 0) or idx[i] < START:
                continue
            mv = c[i] / prev - 1
            if abs(mv) < THRESH[0]:
                continue
            nxt = o[i + 1] / c[i] - 1 if o[i + 1] > 0 else np.nan
            if not np.isfinite(nxt) or not (-0.5 < nxt < 1.0):        # bad print / data error
                continue
            j = min(n, i + 1 + LOOK)
            rng = (h[i] - l[i]) / c[i] if c[i] else 0
            out.append({"t": t, "i": i, "d": idx[i], "move": float(mv), "prev": float(prev), "dv": float(dv[i - 1]),
                        "c0": float(c[i]), "gapOpen": float(o[i] / prev - 1) if o[i] > 0 else None,
                        "buyout": bool(mv > 0 and rng < 0.03 and o[i] / prev - 1 > 0.15),   # flat after a gap: a takeover price
                        "dates": idx[i + 1:j], "o": o[i + 1:j].tolist(), "h": h[i + 1:j].tolist(),
                        "l": l[i + 1:j].tolist(), "c": c[i + 1:j].tolist()})
    return out


def trade(e, side, entry, stop, hold, borrow=BORROW):
    """side -1 short / +1 long. entry: 'close' (spike-day close), 'open1' (next open), 'open2' (open two days later).
    Returns dict or None."""
    k0 = {"close": 0, "open1": 0, "open2": 1}[entry]
    if entry == "open2":
        if len(e["o"]) < 2 or e["l"][0] <= e["c0"] * 0.90:          # Rule 201 renewed on the next day
            return None
        px = e["o"][1]
    elif entry == "open1":
        px = e["o"][0]
    else:
        px = e["c0"]
    if not (px and px > 0):
        return None
    stop_px = px * (1 - side * stop) if stop else None
    last = k0 + hold - 1 if entry != "close" else hold - 1        # index of the exit-day bar
    if last >= len(e["c"]):
        return None
    exit_px, why, k = None, "time", last
    for k in range(k0, last + 1):
        if stop_px:
            first = not (entry == "close" or k > k0)                # entry bar opened at our entry price
            o_k = e["o"][k]
            if side < 0 and e["h"][k] >= stop_px:
                exit_px, why = (stop_px if first else max(stop_px, o_k)) * (1 + STOP_SLIP), "stop"
            elif side > 0 and e["l"][k] <= stop_px:
                exit_px, why = (stop_px if first else min(stop_px, o_k)) * (1 - STOP_SLIP), "stop"
            if exit_px:
                break
    if exit_px is None:
        exit_px, k = e["c"][last], last
    d_in = e["d"] if entry == "close" else e["dates"][k0]
    d_out = e["dates"][k]
    days = (pd.Timestamp(d_out) - pd.Timestamp(d_in)).days
    r = side * (exit_px / px - 1) - COST - (borrow * days / 360 if side < 0 else 0)
    risk = (stop * (1 + STOP_SLIP) + STOP_SLIP) if stop else 0.40
    return {"t": e["t"], "d": d_in, "out": d_out, "ret": float(r), "why": why, "riskPct": risk, "mag": abs(e["move"]),
            "gross": float(side * (exit_px / px - 1))}


def summarize(tr):
    if not tr:
        return {"trades": 0}
    r = np.array([x["ret"] for x in tr])
    w, l = r[r > 0], r[r <= 0]
    halves = [np.array([x["ret"] for x in tr if (x["d"] < MID) == first]) for first in (True, False)]
    years = {}
    for x in tr:
        years.setdefault(x["d"][:4], []).append(x["ret"])
    return {"trades": int(len(r)), "winRatePct": round(float(len(w) / len(r) * 100), 1),
            "avgPct": round(float(r.mean() * 100), 2), "medianPct": round(float(np.median(r) * 100), 2),
            "avgWinPct": round(float(w.mean() * 100), 2) if len(w) else None,
            "avgLossPct": round(float(l.mean() * 100), 2) if len(l) else None,
            "profitFactor": round(float(w.sum() / -l.sum()), 2) if len(l) and l.sum() < 0 else None,
            "stopsPct": round(sum(1 for x in tr if x["why"] == "stop") / len(tr) * 100, 1),
            "tStat": round(float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))), 2) if len(r) > 2 and r.std() > 0 else None,
            "firstHalfAvgPct": round(float(halves[0].mean() * 100), 2) if len(halves[0]) else None,
            "secondHalfAvgPct": round(float(halves[1].mean() * 100), 2) if len(halves[1]) else None,
            "byYear": {y: {"n": len(v), "avgPct": round(float(np.mean(v) * 100), 2)} for y, v in sorted(years.items())}}


def robust(s):
    if s.get("trades", 0) < 60:
        return False
    yrs = [v["avgPct"] for v in s["byYear"].values() if v["n"] >= 10]
    return (s["avgPct"] > 0 and (s["firstHalfAvgPct"] or -1) > 0 and (s["secondHalfAvgPct"] or -1) > 0
            and (s["tStat"] or 0) >= 2 and (s["profitFactor"] or 0) >= 1.15 and len(yrs) >= 4
            and sum(1 for y in yrs if y > 0) >= math.ceil(len(yrs) * 0.7))


def portfolio(tr, risk=0.01, cap=0.25, slots=4, start=1500.0):
    """Sleeve simulation on realized P&L: up to `slots` open trades and one per stock; each sized to lose about `risk` of
    equity at the stop (never more than `cap` of equity); biggest moves first on a crowded day."""
    by_day = {}
    for x in tr:
        by_day.setdefault(x["d"], []).append(x)
    eq, peak, mdd, opened, curve, taken = start, start, 0.0, [], [], 0
    days = sorted(set(by_day) | {x["out"] for x in tr})
    pending = []                                                    # (out_date, ticker, pnl)
    for d in days:
        live = [p for p in pending if p[0] >= d]
        for x in sorted(by_day.get(d, []), key=lambda z: -z["mag"]):
            if len(live) >= slots or any(p[1] == x["t"] for p in live):
                continue
            w = min(cap, risk / max(x["riskPct"], 0.002))
            p = (x["out"], x["t"], eq * w * x["ret"])
            pending.append(p)
            live.append(p)
            taken += 1
        for p in [p for p in pending if p[0] == d]:
            eq += p[2]
        pending = [p for p in pending if p[0] > d]
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        curve.append((d, round(eq, 2)))
    if not curve:
        return {"taken": 0}
    yrs = max((pd.Timestamp(curve[-1][0]) - pd.Timestamp(curve[0][0])).days / 365.25, 0.5)
    yearly = {}
    for d, v in curve:
        yearly[d[:4]] = v
    ys, prev = {}, start
    for y in sorted(yearly):
        ys[y] = round((yearly[y] / prev - 1) * 100, 1)
        prev = yearly[y]
    return {"taken": taken, "endValue": round(eq, 2), "returnPct": round((eq / start - 1) * 100, 1),
            "cagrPct": round(((eq / start) ** (1 / yrs) - 1) * 100, 2), "maxDrawdownPct": round(mdd * 100, 2),
            "byYearPct": ys, "curve": curve[::5][-300:]}


def main():
    names = MV.nasdaq_symbols()
    data = MV.download(sorted(names), start="2018-06-01", interval="1d")
    ev = collect(data)
    print("tickers", len(data), "events", len(ev))
    grid, keep = [], {}
    plans = [("fadeUp", -1, "close"), ("fadeUp", -1, "open1"), ("crashShort", -1, "open2"), ("crashLong", 1, "open1")]
    for fname, (pmin, dvmin) in FILTERS.items():
        base = [e for e in ev if e["prev"] >= pmin and e["dv"] >= dvmin]
        for kind, side, entry in plans:
            for th in THRESH:
                sel = [e for e in base if (e["move"] >= th if kind == "fadeUp" else e["move"] <= -th) and not e["buyout"]]
                for stop in STOPS:
                    for hold in HOLDS:
                        tr = [x for x in (trade(e, side, entry, stop, hold) for e in sel) if x]
                        s = summarize(tr)
                        key = f"{kind}|{entry}|{fname}|t{int(th * 100)}|s{int(stop * 100) if stop else 0}|h{hold}"
                        row = {"key": key, "kind": kind, "entry": entry, "filter": fname, "thresholdPct": int(th * 100),
                               "stopPct": int(stop * 100) if stop else None, "hold": hold,
                               **{k: s.get(k) for k in ("trades", "winRatePct", "avgPct", "medianPct", "profitFactor",
                                                         "stopsPct", "tStat", "firstHalfAvgPct", "secondHalfAvgPct")},
                               "robust": robust(s)}
                        grid.append(row)
                        keep[key] = (fname, kind, th, side, entry, stop, hold)
    rob = sorted([g for g in grid if g["robust"] and g["kind"] != "crashLong"], key=lambda g: -(g["tStat"] or 0))
    best = {}
    for kind in ("fadeUp", "crashShort", "crashLong"):
        cand = [g for g in grid if g["kind"] == kind and (g["trades"] or 0) >= 60]
        cand.sort(key=lambda g: (not g["robust"], -(g["tStat"] or -99)))
        if not cand:
            continue
        g = cand[0]
        fname, kind_, th, side, entry, stop, hold = keep[g["key"]]
        pmin, dvmin = FILTERS[fname]
        sel = [e for e in ev if e["prev"] >= pmin and e["dv"] >= dvmin and not e["buyout"]
               and (e["move"] >= th if kind_ == "fadeUp" else e["move"] <= -th)]
        tr = [x for x in (trade(e, side, entry, stop, hold) for e in sel) if x]
        s = summarize(tr)
        hi = summarize([x for x in (trade(e, side, entry, stop, hold, borrow=BORROW_HIGH) for e in sel) if x])
        best[kind] = {"variant": g, "detail": s, "borrow100": {k: hi.get(k) for k in ("avgPct", "winRatePct", "tStat",
                                                                                       "firstHalfAvgPct", "secondHalfAvgPct")},
                      "portfolio": {f"r{int(r * 100)}_s{sl}": portfolio(tr, risk=r, slots=sl)
                                    for r in (0.01, 0.02) for sl in (4, 8)},
                      "examples": sorted(tr, key=lambda x: x["d"])[-12:]}
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "tickers": len(data), "events": len(ev),
           "from": min((e["d"] for e in ev), default=None), "to": max((e["d"] for e in ev), default=None),
           "assumptions": {"costPct": COST * 100, "stopSlipPct": STOP_SLIP * 100, "borrowPctYear": BORROW * 100,
                           "borrowHighPctYear": BORROW_HIGH * 100, "split": MID, "sleeveStart": 1500},
           "robustCount": len(rob), "robustTop": rob[:25], "best": best, "grid": grid,
           "notes": "Günlük veriler; bugün Nasdaq'ta işlem gören hisseler (hayatta kalma yanılgısı var). Satın alma "
                    "haberiyle yükselip yatay kalan hisseler hariç. Sağlam = 60+ işlem, iki yarıda da pozitif, t ≥ 2, "
                    "kâr faktörü ≥ 1,15 ve yılların %70'inde pozitif."}
    for x in out["best"].values():
        for e in x["examples"]:
            e["ret"] = round(e["ret"] * 100, 2)
            e["gross"] = round(e["gross"] * 100, 2)
    (ROOT / "spike_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("robust:", len(rob))
    for g in rob[:15]:
        print(g["key"], g["trades"], g["avgPct"], g["winRatePct"], g["tStat"], g["firstHalfAvgPct"], g["secondHalfAvgPct"])
    for k, v in best.items():
        print(k, v["variant"]["key"], v["detail"]["avgPct"], v["borrow100"], v["portfolio"]["r1_s4"].get("cagrPct"),
              v["portfolio"]["r1_s4"].get("maxDrawdownPct"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
