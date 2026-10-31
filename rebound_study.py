"""Research only (never trades): does it pay to buy a Nasdaq stock that fell hard and then calmed down?

Two kinds of "fell a lot":
  crash  - a one-day fall of 20 % (or 30 %) or more, 10 to 60 trading days ago,
  dd     - the price is 30 / 40 / 50 % or more below its 52-week high.
"Calmed down" (stabilisation), checked on day t with data up to t only:
  S1 - no new 20-day closing low in the last 10 days (the low is at least 10 days old),
  S2 - S1 and the price is above its 10- and 20-day averages (it has started to turn up),
  S3 - S2 and the last 10 days were at least 30 % calmer than the 10 days before (volatility contracting).
Baselines without stabilisation: buy the day after the crash, or as soon as the drawdown is reached.

Trade: buy at the next day's open, hold 20 or 60 trading days, 0.2 % round-trip cost. Every result is also shown
against QQQ over exactly the same days (excess return), because 2019-2026 was mostly a rising market.
Filters: real traded price >= 10 $, >= 20 million $ traded a day, no reverse split in the previous 12 months.
One trade per stock at a time. Survivorship bias: companies that were delisted or went bankrupt are missing, which
makes buying fallen stocks look BETTER than it really was.

Writes rebound_results.json, including the stocks that meet the best rules today.
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
COST = 0.002
HOLDS = (20, 60)
MIN_PRICE, MIN_DV = 10.0, 20e6


def features(s):
    c, o, h, v = (s[k].astype(float) for k in ("Close", "Open", "High", "Volume"))
    sp = s["Stock Splits"].astype(float).fillna(0) if "Stock Splits" in s else pd.Series(0.0, index=s.index)
    spv = np.where(sp.values > 0, sp.values, 1.0)
    after = np.ones(len(c))
    acc = 1.0
    for k in range(len(c) - 1, -1, -1):
        after[k] = acc
        acc *= spv[k]
    f = pd.DataFrame(index=s.index)
    f["c"], f["o"] = c, o
    f["ret"] = c / c.shift(1) - 1
    f["dd"] = c / h.rolling(252, min_periods=120).max() - 1
    f["raw"] = c * after
    f["dv"] = (c * v).rolling(20).mean().shift(1)
    rs = pd.Series(((sp.values > 0) & (sp.values < 1)).astype(float), index=s.index)
    f["rs"] = rs.rolling(252, min_periods=1).sum() > 0
    for x in (20, 30):
        crash = f["ret"] <= -x / 100
        last = pd.Series(np.where(crash.values, np.arange(len(c)), np.nan), index=s.index).ffill()
        f[f"age{x}"] = np.arange(len(c)) - last.values
        f[f"csize{x}"] = pd.Series(np.where(crash.values, f["ret"].values, np.nan), index=s.index).ffill()
    f["s1"] = c.rolling(10).min() > c.shift(10).rolling(10).min()
    f["s2"] = f["s1"] & (c > c.rolling(10).mean()) & (c > c.rolling(20).mean())
    vol10 = f["ret"].rolling(10).std()
    f["s3"] = f["s2"] & (vol10 < vol10.shift(10) * 0.7)
    f["ok"] = (f["raw"] >= MIN_PRICE) & (f["dv"] >= MIN_DV) & (~f["rs"])
    return f


RULES = {}
for x in (20, 30):
    RULES[f"crash{x}_now"] = lambda f, x=x: f[f"age{x}"] == 0
    for st in ("s1", "s2", "s3"):
        RULES[f"crash{x}_{st}"] = lambda f, x=x, st=st: (f[f"age{x}"] >= 10) & (f[f"age{x}"] <= 60) & f[st]
for y in (30, 40, 50):
    RULES[f"dd{y}_now"] = lambda f, y=y: f["dd"] <= -y / 100
    for st in ("s1", "s2", "s3"):
        RULES[f"dd{y}_{st}"] = lambda f, y=y, st=st: (f["dd"] <= -y / 100) & f[st]


def trades_for(t, f, rule, hold, q):
    sig = (rule(f) & f["ok"]).values
    idx = f.index
    o, c = f["o"].values, f["c"].values
    out, nxt = [], 0
    for i in np.flatnonzero(sig):
        if i < nxt or i + hold >= len(c) or str(idx[i].date()) < START:
            continue
        e, x = o[i + 1], c[i + hold]
        if not (e > 0 and x > 0):
            continue
        d_in, d_out = idx[i + 1], idx[i + hold]
        r = x / e - 1 - COST
        qr = None
        if d_in in q.index and d_out in q.index:
            qr = float(q.loc[d_out, "Close"] / q.loc[d_in, "Open"] - 1)
        out.append({"t": t, "d": str(d_in.date()), "ret": float(r), "ex": float(r - qr) if qr is not None else None,
                    "dd": float(f["dd"].values[i]) if np.isfinite(f["dd"].values[i]) else None})
        nxt = i + hold
    return out


def summarize(tr):
    if not tr:
        return {"trades": 0}
    r = np.array([x["ret"] for x in tr])
    ex = np.array([x["ex"] for x in tr if x["ex"] is not None])
    half = lambda first: [x for x in tr if (x["d"] < MID) == first and x["ex"] is not None]
    h1, h2 = half(True), half(False)
    years = {}
    for x in tr:
        if x["ex"] is not None:
            years.setdefault(x["d"][:4], []).append(x["ex"])
    return {"trades": int(len(r)), "avgPct": round(float(r.mean() * 100), 2), "medianPct": round(float(np.median(r) * 100), 2),
            "winRatePct": round(float((r > 0).mean() * 100), 1), "worstPct": round(float(r.min() * 100), 1),
            "avgExcessPct": round(float(ex.mean() * 100), 2) if len(ex) else None,
            "medianExcessPct": round(float(np.median(ex) * 100), 2) if len(ex) else None,
            "beatQqqPct": round(float((ex > 0).mean() * 100), 1) if len(ex) else None,
            "tStatExcess": round(float(ex.mean() / (ex.std(ddof=1) / math.sqrt(len(ex)))), 2) if len(ex) > 2 and ex.std() > 0 else None,
            "firstHalfExcessPct": round(float(np.mean([x["ex"] for x in h1]) * 100), 2) if h1 else None,
            "secondHalfExcessPct": round(float(np.mean([x["ex"] for x in h2]) * 100), 2) if h2 else None,
            "byYearExcess": {y: {"n": len(v), "avgPct": round(float(np.mean(v) * 100), 2)} for y, v in sorted(years.items())}}


def robust(s):
    if s.get("trades", 0) < 80 or s.get("avgExcessPct") is None:
        return False
    yrs = [v["avgPct"] for v in s["byYearExcess"].values() if v["n"] >= 10]
    return (s["avgExcessPct"] > 0 and s["medianExcessPct"] > -1 and (s["firstHalfExcessPct"] or -1) > 0
            and (s["secondHalfExcessPct"] or -1) > 0 and (s["tStatExcess"] or 0) >= 2
            and len(yrs) >= 4 and sum(1 for y in yrs if y > 0) >= math.ceil(len(yrs) * 0.6))


def main():
    names = MV.nasdaq_symbols()
    data = MV.download(sorted(set(names) | {"QQQ"}), start="2018-01-01", interval="1d", actions=True)
    q = data.pop("QQQ")
    q.index = q.index.tz_localize(None) if q.index.tz is not None else q.index
    feats = {}
    for t, s in data.items():
        try:
            if len(s) < 140:
                continue
            s = s.copy()
            s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
            feats[t] = features(s)
        except Exception as e:
            print("features failed", t, e)
    print("tickers", len(feats))
    res = {}
    for name, rule in RULES.items():
        for hold in HOLDS:
            tr = []
            for t, f in feats.items():
                tr += trades_for(t, f, rule, hold, q)
            s = summarize(tr)
            s["robust"] = robust(s)
            res[f"{name}|h{hold}"] = s
            print(name, hold, s.get("trades"), s.get("avgPct"), s.get("avgExcessPct"), s.get("beatQqqPct"),
                  s.get("firstHalfExcessPct"), s.get("secondHalfExcessPct"), s["robust"])
    rob = sorted([k for k, s in res.items() if s["robust"]], key=lambda k: -(res[k]["tStatExcess"] or 0))
    # stocks that meet a rule today (last bar), for the stabilised rules
    F = {}
    try:
        F = json.loads((ROOT / "fundamentals.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    today = []
    for t, f in feats.items():
        last = f.iloc[-1]
        if not bool(last["ok"]):
            continue
        hits = [n for n, rule in RULES.items() if not n.endswith("_now") and bool(rule(f).iloc[-1])]
        if not hits:
            continue
        capb, pe = MV.cap_pe(t, float(last["c"]), F)
        today.append({"ticker": t, "name": names.get(t, t), "price": round(float(last["c"]), 2),
                      "ddPct": round(float(last["dd"]) * 100, 1) if np.isfinite(last["dd"]) else None,
                      "crash20AgeDays": int(last["age20"]) if np.isfinite(last["age20"]) else None,
                      "crash20Pct": round(float(last["csize20"]) * 100, 1) if np.isfinite(last["csize20"]) else None,
                      "dollarVolM": round(float(last["dv"]) / 1e6, 1), "capB": round(capb, 2) if capb else None,
                      "pe": round(pe, 1) if pe else None, "rules": hits,
                      "robustRules": [n for n in hits if any(k.startswith(n + "|") for k in rob)]})
    today.sort(key=lambda r: (-len(r["robustRules"]), r["ddPct"] if r["ddPct"] is not None else 0))
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "tickers": len(feats),
           "lastBar": str(max(f.index[-1] for f in feats.values()).date()) if feats else None,
           "filters": {"minPrice": MIN_PRICE, "minDollarVolM": MIN_DV / 1e6, "costPct": COST * 100},
           "results": res, "robust": rob, "today": today[:80],
           "notes": "Bugün Nasdaq'ta işlem gören hisseler (batan şirketler yok: düşen hisseyi almak olduğundan iyi görünür). "
                    "Getiri: ertesi açılışta al, 20 veya 60 işlem günü tut; 'excess' aynı günlerde QQQ'ya göre fark."}
    (ROOT / "rebound_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("robust:", rob[:10])
    print("today:", [(r["ticker"], r["ddPct"], r["robustRules"]) for r in today[:20]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
