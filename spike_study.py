"""Research only (never trades): can the bot make money by shorting Nasdaq stocks right after a 20 % day?

Ideas tested on daily bars since mid-2018 for today's Nasdaq-listed common stocks:
  fadeUp     - a stock closes 20 %+ up: short it (at that day's close, or the next morning's open), cover after a few days,
               with or without a stop. Idea: news spikes in small stocks tend to give the jump back.
  crashShort - a stock closes 20 %+ down: SEC Rule 201 blocks new shorts for the rest of that day and the next one, so the
               short is opened at the open two days later (skipped if the stock fell 10 % again in between, which renews
               the restriction). Idea: big crashes tend to keep falling.

Costs: 0.4 % round trip (spread + commission + slippage), 1 % extra slippage when a stop fills, and a short borrow fee
(30 %/year by default, also shown at 100 % and 200 %/year, since freshly spiked small stocks are often hard to borrow).
Prices are filtered on the price the stock really traded at that day (Yahoo's history is adjusted for later reverse splits,
which would otherwise let yesterday's penny stocks pass a 5 $ filter). Survivorship bias remains: delisted companies are
missing, which works against the short side (a bankruptcy would have been a winner for a short).

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
BORROW = 0.30
THRESH = (0.20, 0.30, 0.50)
STOPS = (0.30, 0.50, 0.75, None)
HOLDS = (3, 5, 10, 15, 20)              # trading days held (open entries: 1 = cover at the close of the entry day)
FILTERS = {"r5_dv50": (5.0, 50e6, True), "r5_dv20": (5.0, 20e6, True), "r5_dv5": (5.0, 5e6, True),
           "r10_dv20": (10.0, 20e6, True), "r3_dv2": (3.0, 2e6, True), "adj5_dv20": (5.0, 20e6, False)}
LIVE = "fadeUp|r5_dv50|t30|s50|h10"     # the rule the trader uses (sert yükseliş short'u)
LOOK = max(HOLDS) + 3


def collect(data):
    """One record per 20 % day with the next LOOK daily bars."""
    out = []
    for t, s in data.items():
        try:
            o, h, l, c, v = (s[k].astype(float).values for k in ("Open", "High", "Low", "Close", "Volume"))
        except Exception:
            continue
        sp = s["Stock Splits"].astype(float).fillna(0).values if "Stock Splits" in s else np.zeros(len(c))
        sp = np.where(sp > 0, sp, 1.0)
        after = np.ones(len(c))                       # product of splits strictly after each day -> real traded price
        acc = 1.0
        for k in range(len(c) - 1, -1, -1):
            after[k] = acc
            acc *= sp[k]
        idx = [str(x.date()) for x in s.index]
        n = len(c)
        dv = pd.Series(np.nan_to_num(c * v)).rolling(20, min_periods=20).mean().values
        rs_days = [idx[k] for k in range(n) if sp[k] < 1]
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
            d0 = pd.Timestamp(idx[i])
            out.append({"t": t, "d": idx[i], "move": float(mv), "prev": float(prev), "prevRaw": float(prev * after[i - 1]),
                        "dv": float(dv[i - 1]), "c0": float(c[i]),
                        "gapOpen": float(o[i] / prev - 1) if o[i] > 0 else 0.0,
                        "rsBefore": any(0 <= (d0 - pd.Timestamp(x)).days <= 365 for x in rs_days),
                        "buyout": bool(mv > 0 and rng < 0.03 and o[i] / prev - 1 > 0.15),   # flat after a gap: a takeover price
                        "dates": idx[i + 1:j], "o": o[i + 1:j].tolist(), "h": h[i + 1:j].tolist(),
                        "l": l[i + 1:j].tolist(), "c": c[i + 1:j].tolist()})
    return out


def pick(ev, fname, kind, th):
    pmin, dvmin, raw = FILTERS[fname]
    return [e for e in ev if (e["prevRaw"] if raw else e["prev"]) >= pmin and e["dv"] >= dvmin and not e["buyout"]
            and (e["move"] >= th if kind == "fadeUp" else e["move"] <= -th)]


def trade(e, side, entry, stop, hold, borrow=BORROW):
    """side -1 short / +1 long. entry: 'close' (spike-day close), 'open1' (next open), 'open2' (open two days later)."""
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
    exit_px, why, mae, path = None, "time", 0.0, {}
    for k in range(k0, last + 1):
        adverse = (e["h"][k] / px - 1) if side < 0 else (1 - e["l"][k] / px)
        if stop_px:
            first = entry != "close" and k == k0                    # entry bar opened at our entry price
            o_k = e["o"][k]
            if side < 0 and e["h"][k] >= stop_px:
                exit_px, why = (stop_px if first else max(stop_px, o_k)) * (1 + STOP_SLIP), "stop"
            elif side > 0 and e["l"][k] <= stop_px:
                exit_px, why = (stop_px if first else min(stop_px, o_k)) * (1 - STOP_SLIP), "stop"
        mae = max(mae, min(adverse, stop * (1 + STOP_SLIP) if exit_px else adverse))
        if exit_px:
            break
        path[e["dates"][k]] = side * (e["c"][k] / px - 1)
    if exit_px is None:
        exit_px, k = e["c"][last], last
    d_in = e["d"] if entry == "close" else e["dates"][k0]
    d_out = e["dates"][k]
    days = (pd.Timestamp(d_out) - pd.Timestamp(d_in)).days
    r = side * (exit_px / px - 1) - COST - (borrow * days / 360 if side < 0 else 0)
    return {"t": e["t"], "d": d_in, "out": d_out, "ret": float(r), "why": why, "mag": abs(e["move"]), "mae": float(mae),
            "gross": float(side * (exit_px / px - 1)), "path": path, "px": px}


def summarize(tr):
    if not tr:
        return {"trades": 0}
    r = np.array([x["ret"] for x in tr])
    w, l = r[r > 0], r[r <= 0]
    mae = np.array([x["mae"] for x in tr])
    halves = [np.array([x["ret"] for x in tr if (x["d"] < MID) == first]) for first in (True, False)]
    years = {}
    for x in tr:
        years.setdefault(x["d"][:4], []).append(x["ret"])
    return {"trades": int(len(r)), "winRatePct": round(float(len(w) / len(r) * 100), 1),
            "avgPct": round(float(r.mean() * 100), 2), "medianPct": round(float(np.median(r) * 100), 2),
            "avgWinPct": round(float(w.mean() * 100), 2) if len(w) else None,
            "avgLossPct": round(float(l.mean() * 100), 2) if len(l) else None,
            "worstPct": round(float(r.min() * 100), 1), "p5Pct": round(float(np.percentile(r, 5) * 100), 1),
            "maeP90Pct": round(float(np.percentile(mae, 90) * 100), 1), "maeMaxPct": round(float(mae.max() * 100), 1),
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


def portfolio(tr, size=0.05, slots=4, start=1500.0):
    """Sleeve simulation, marked to market every day: up to `slots` open trades and one per stock, each `size` of the
    sleeve's value at entry; biggest moves first on a crowded day."""
    by_day = {}
    for x in tr:
        by_day.setdefault(x["d"], []).append(x)
    days = sorted(set(by_day) | {d for x in tr for d in x["path"]} | {x["out"] for x in tr})
    realized, peak, mdd, opened, curve, taken, eq = start, start, 0.0, [], [], 0, start
    worst_day = 0.0
    for d in days:
        for x in sorted(by_day.get(d, []), key=lambda z: -z["mag"]):
            if len(opened) >= slots or any(p["t"] == x["t"] for p in opened):
                continue
            opened.append({"t": x["t"], "out": x["out"], "usd": eq * size, "x": x, "mark": 0.0})
            taken += 1
        for p in opened:
            if d in p["x"]["path"]:
                p["mark"] = p["x"]["path"][d]
        for p in [p for p in opened if p["out"] == d]:
            realized += p["usd"] * p["x"]["ret"]
        opened = [p for p in opened if p["out"] != d]
        new_eq = realized + sum(p["usd"] * p["mark"] for p in opened)
        worst_day = min(worst_day, new_eq / eq - 1) if eq > 0 else worst_day
        eq = new_eq
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        curve.append((d, round(eq, 2)))
    if not curve:
        return {"taken": 0}
    yrs = max((pd.Timestamp(curve[-1][0]) - pd.Timestamp(curve[0][0])).days / 365.25, 0.5)
    yearly, ys, prev = {}, {}, start
    for d, v in curve:
        yearly[d[:4]] = v
    for y in sorted(yearly):
        ys[y] = round((yearly[y] / prev - 1) * 100, 1)
        prev = yearly[y]
    return {"taken": taken, "endValue": round(eq, 2), "returnPct": round((eq / start - 1) * 100, 1),
            "cagrPct": round(((max(eq, 1) / start) ** (1 / yrs) - 1) * 100, 2), "maxDrawdownPct": round(mdd * 100, 2),
            "worstDayPct": round(worst_day * 100, 2), "byYearPct": ys, "curve": curve[::5][-300:]}


def brief(tr):
    s = summarize(tr)
    return {k: s.get(k) for k in ("trades", "avgPct", "medianPct", "winRatePct", "profitFactor", "tStat", "worstPct",
                                  "firstHalfAvgPct", "secondHalfAvgPct")}


def main():
    names = MV.nasdaq_symbols()
    data = MV.download(sorted(names), start="2018-06-01", interval="1d", actions=True)
    ev = collect(data)
    print("tickers", len(data), "events", len(ev))
    grid = []
    plans = [("fadeUp", -1, "open1"), ("fadeUp", -1, "close"), ("crashShort", -1, "open2")]
    for fname in FILTERS:
        for kind, side, entry in plans:
            for th in THRESH:
                sel = pick(ev, fname, kind, th)
                for stop in STOPS:
                    for hold in HOLDS:
                        s = summarize([x for x in (trade(e, side, entry, stop, hold) for e in sel) if x])
                        grid.append({"key": f"{kind}|{entry}|{fname}|t{int(th * 100)}|s{int(stop * 100) if stop else 0}|h{hold}",
                                     "kind": kind, "entry": entry, "filter": fname, "thresholdPct": int(th * 100),
                                     "stopPct": int(stop * 100) if stop else None, "hold": hold, "robust": robust(s),
                                     **{k: s.get(k) for k in ("trades", "winRatePct", "avgPct", "medianPct", "profitFactor",
                                                               "stopsPct", "tStat", "worstPct", "p5Pct", "maeP90Pct",
                                                               "firstHalfAvgPct", "secondHalfAvgPct")}})
    rob = sorted([g for g in grid if g["robust"]], key=lambda g: -(g["tStat"] or 0))
    # the live candidates: next-open short, real price >= 5 $, 20 M $ a day, 30 %+ spike
    cands = {}
    for fname, kind, entry, th in (("r5_dv50", "fadeUp", "open1", 0.30), ("r5_dv50", "fadeUp", "open1", 0.50),
                                   ("r5_dv20", "fadeUp", "open1", 0.30), ("r5_dv20", "fadeUp", "open1", 0.50),
                                   ("r5_dv20", "crashShort", "open2", 0.30)):
        sel = pick(ev, fname, kind, th)
        for stop in STOPS:
            for hold in (5, 10, 15):
                tr = [x for x in (trade(e, -1, entry, stop, hold) for e in sel) if x]
                key = f"{kind}|{fname}|t{int(th * 100)}|s{int(stop * 100) if stop else 0}|h{hold}"
                s = summarize(tr)
                c = {"summary": s, "robust": robust(s),
                     "borrow100": brief([x for x in (trade(e, -1, entry, stop, hold, 1.0) for e in sel) if x]),
                     "borrow200": brief([x for x in (trade(e, -1, entry, stop, hold, 2.0) for e in sel) if x]),
                     "portfolio": {f"f{int(f * 100)}_s{sl}": {k: v for k, v in portfolio(tr, f, sl).items() if k != "curve"}
                                   for f in (0.05, 0.10) for sl in (4, 6, 8)}}
                if kind == "fadeUp" and hold == 10 and stop in (None, 0.50, 0.75) and th == 0.30:
                    by = lambda f: brief([x for x, e in ((trade(e, -1, entry, stop, hold), e) for e in sel) if x and f(e)])
                    c["slices"] = {"noReverseSplitBefore": by(lambda e: not e["rsBefore"]),
                                   "reverseSplitBefore": by(lambda e: e["rsBefore"]),
                                   "gapUp10": by(lambda e: e["gapOpen"] >= 0.10), "intradayRun": by(lambda e: e["gapOpen"] < 0.10),
                                   "price5to20": by(lambda e: e["prevRaw"] < 20), "price20plus": by(lambda e: e["prevRaw"] >= 20),
                                   "dv50plus": by(lambda e: e["dv"] >= 50e6), "move30to50": by(lambda e: e["move"] < 0.5),
                                   "move50plus": by(lambda e: e["move"] >= 0.5)}
                    c["curve"] = portfolio(tr, 0.05, 6)["curve"]
                    c["examples"] = [{k: (round(x[k] * 100, 2) if k in ("ret", "gross", "mae") else x[k])
                                      for k in ("t", "d", "out", "ret", "gross", "mae", "why")}
                                     for x in sorted(tr, key=lambda z: z["d"])[-15:]]
                    c["worst"] = [{k: (round(x[k] * 100, 2) if k in ("ret", "gross", "mae") else x[k])
                                   for k in ("t", "d", "out", "ret", "mae", "why")} for x in sorted(tr, key=lambda z: z["ret"])[:10]]
                cands[key] = c
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "version": 2, "tickers": len(data), "events": len(ev),
           "from": min((e["d"] for e in ev), default=None), "to": max((e["d"] for e in ev), default=None),
           "assumptions": {"costPct": COST * 100, "stopSlipPct": STOP_SLIP * 100, "borrowPctYear": BORROW * 100,
                           "split": MID, "sleeveStart": 1500},
           "robustCount": len(rob), "robustTop": rob[:30], "live": LIVE, "candidates": cands,
           "grid": [g for g in grid if g["filter"] in ("r5_dv50", "r5_dv20", "adj5_dv20")],
           "notes": "Günlük veriler; bugün Nasdaq'ta işlem gören hisseler (hayatta kalma yanılgısı var, short tarafının "
                    "aleyhine). Fiyat filtresi o gün gerçekten işlem gören fiyatla (ters bölünme düzeltmesi geri alındı). "
                    "Satın alma haberiyle yükselip yatay kalan hisseler hariç. Sağlam = 60+ işlem, iki yarıda da pozitif, "
                    "t ≥ 2, kâr faktörü ≥ 1,15 ve yılların %70'inde pozitif. İşlemler zaman içinde çakıştığı için t değeri "
                    "olduğundan yüksek görünür."}
    (ROOT / "spike_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("robust:", len(rob))
    for k, c in cands.items():
        s = c["summary"]
        print(k, s.get("trades"), s.get("avgPct"), s.get("winRatePct"), s.get("tStat"), s.get("worstPct"), c["robust"],
              c["portfolio"]["f5_s6"].get("cagrPct"), c["portfolio"]["f5_s6"].get("maxDrawdownPct"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
