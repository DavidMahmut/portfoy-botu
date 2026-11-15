"""Research only (never trades): what does leverage above 3x do to the bot's index rule?

Written for the "x10" paper account (Nov 2026). Same direction rule as the v6 index sleeve (SPY above its 200-day
average = long; below it with the 50-day under the 200-day = short; no short after a 20 % Nasdaq fall), same
volatility targeting (exposure = target volatility / QQQ's realised 20-day volatility), but the exposure may go up to
3x, 5x or 10x and is held as QQQ on margin instead of 3x funds:

  long   L x QQQ, the part above the account's own money is borrowed at the T-bill rate + 1.5 points,
  short  L x QQQ sold short, 0.5 %/year borrow fee,
  margin 10 % initial (so 10x is the ceiling); the broker closes the whole position when the account's value falls
         below 5 % of the position (the 50 % close-out rule European CFD accounts use). The day's low (long) or high
         (short) decides whether that happened; a gap through the level fills at the open,
  costs  0.10 % of every change in exposure, 0.20 % extra on a forced close-out.

Two views of every variant:
  "compounded" - 3.000 $ left alone: yearly return, worst fall, number of forced close-outs,
  "restart"    - what the live x10 account does: when the account falls below 300 $ it is refilled with 3.000 $;
                 counts the refills and reports money in vs. money at the end.

Runs on GitHub Actions (.github/workflows/lev.yml); writes lev_results.json.
"""
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import dip_study as DS

ROOT = Path(__file__).resolve().parent
CAPITAL, REFILL_BELOW = 3000.0, 300.0
SPREAD, BORROW = 0.015, 0.005
COST, LIQ_COST = 0.0010, 0.0020
CLOSEOUT = 0.05
VTS = (0.25, 0.35, 0.50, 0.75, 1.00, 1.50, None)        # None = always at the cap
CAPS = (3.0, 5.0, 10.0)
SHORTS = {"s3": 3.0, "sc": None, "s0": 0.0}              # short side: at most 3x, same as the long cap, or no shorts
WINDOWS = DS.WINDOWS


def load():
    import yfinance as yf
    df = yf.download(["QQQ", "SPY", "^IRX"], start="1993-01-01", interval="1d", auto_adjust=True, group_by="ticker",
                     progress=False, threads=True)
    q = df["QQQ"].dropna(subset=["Close"])
    return {"QQQ": q["Close"], "SPY": df["SPY"]["Close"].dropna(), "^IRX": df["^IRX"]["Close"].dropna(), "ohlc": q}


def prepare(px):
    D = DS.prepare(px)
    o = px["ohlc"].reindex(D.index)
    pc = D["q"].shift(1)
    D["lo"] = (o["Low"] / pc - 1).fillna(0.0).clip(upper=0.0)
    D["hi"] = (o["High"] / pc - 1).fillna(0.0).clip(lower=0.0)
    D["op"] = (o["Open"] / pc - 1).fillna(0.0)
    return D


def target_fn(vt, cap, short_cap):
    """v6 direction with the given volatility target and caps; returns fn(row, state) -> target exposure (signed)."""
    def fn(row, st):
        rv = row["rv"] if not math.isnan(row["rv"]) else 0.2
        size = cap if vt is None else min(cap, vt / max(rv, 0.05))
        d = row["dir"]
        dip = row["dd"] <= -0.20 or (st.get("dip") and row["q"] < row["ath"])
        st["dip"] = bool(dip)
        if d < 0:
            if dip or short_cap <= 0:
                return 0.0
            return -min(size, short_cap)
        return d * size
    return fn


def hold_fn(lev):
    return lambda row, st: lev


def simulate(rows, fn, refill=False):
    """rows: list of dicts (one per day). Returns (curve, stats). With refill=True the account is topped up with
    CAPITAL whenever it falls below REFILL_BELOW (the live x10 rule) and `paid` counts the money put in."""
    E, N = CAPITAL, 0.0                       # account value and signed QQQ position, in dollars
    paid, refills, closeouts, turn_cost, fin_cost = CAPITAL, 0, 0, 0.0, 0.0
    curve, levs, st, dead = [], [], {}, False
    for i, row in enumerate(rows):
        if i > 0 and N != 0 and not dead:
            S = abs(N)
            if N > 0:
                x = (CLOSEOUT * N - E) / (N * (1 - CLOSEOUT))            # move from yesterday's close that triggers it
                hit = row["lo"] <= x
                fill = min(x, row["op"]) if hit else row["r"]
            else:
                x = (E - CLOSEOUT * S) / (S * (1 + CLOSEOUT))
                hit = row["hi"] >= x
                fill = max(x, row["op"]) if hit else row["r"]
            E += N * fill
            rf = row["rf"]
            if N > 0:
                debit = max(0.0, N - (E - N * fill))
                c = debit * (rf + SPREAD / 252) - max(0.0, (E - N * fill) - N) * rf
            else:
                c = S * BORROW / 252
            E -= c
            fin_cost += c
            if hit:
                E -= S * LIQ_COST
                closeouts += 1
                N = 0.0
        elif i > 0 and not dead:
            E += E * row["rf"]
        if E <= 0 and not dead:
            E, N = 0.0, 0.0
            if not refill:
                dead = True
        if refill and E < REFILL_BELOW:
            E, N = CAPITAL, 0.0                # what was left is written off; a fresh 3.000 $ starts
            paid += CAPITAL
            refills += 1
        if not dead:
            tgt = fn(row, st)
            cur = N / E if E > 0 else 0.0
            if abs(tgt - cur) >= max(0.3, 0.10 * abs(tgt)) or ((tgt == 0) != (cur == 0)):
                newN = tgt * E
                c = abs(newN - N) * COST
                E -= c
                turn_cost += c
                N = tgt * E
        curve.append(E)
        levs.append(abs(N) / E if E > 0 else 0.0)
    lv = np.array(levs)
    return curve, {"closeouts": closeouts, "refills": refills, "paid": round(paid, 2), "end": round(E, 2),
                   "avgLeverage": round(float(lv.mean()), 2), "daysAt9xPct": round(float((lv >= 9).mean()) * 100, 1),
                   "financingCost": round(fin_cost, 2), "tradingCost": round(turn_cost, 2), "ruined": dead}


def metrics(curve, idx, rf):
    s = pd.Series(curve, index=idx)
    yrs = (idx[-1] - idx[0]).days / 365.25
    if s.iloc[-1] <= 0:
        dead_at = str(s[s <= 0].index[0].date())
        return {"endValue": 0.0, "cagrPct": -100.0, "maxDrawdownPct": -100.0, "ruinedOn": dead_at}
    r = s.pct_change().dropna()
    ex = r - rf.reindex(r.index).fillna(0)
    yearly = (1 + r).groupby(r.index.year).prod() - 1
    return {"endValue": round(float(s.iloc[-1]), 2), "cagrPct": round(((s.iloc[-1] / CAPITAL) ** (1 / yrs) - 1) * 100, 2),
            "maxDrawdownPct": round(float((s / s.cummax() - 1).min()) * 100, 2),
            "volPct": round(float(r.std() * math.sqrt(252)) * 100, 1),
            "sharpe": round(float(ex.mean() / ex.std() * math.sqrt(252)), 2) if ex.std() > 0 else None,
            "worstYearPct": round(float(yearly.min()) * 100, 1), "bestYearPct": round(float(yearly.max()) * 100, 1),
            "worstDayPct": round(float(r.min()) * 100, 1),
            "yearly": {str(k): round(float(v) * 100, 1) for k, v in yearly.items()}}


def evaluate(D, fn):
    out = {}
    for wname, start in WINDOWS.items():
        seg = D[D.index >= start]
        rows = seg.to_dict("records")
        curve, st = simulate(rows, fn)
        m = metrics(curve, seg.index, D["rf"])
        _, rs = simulate(rows, fn, refill=True)
        out[wname] = {**m, **{k: st[k] for k in ("closeouts", "avgLeverage", "daysAt9xPct", "financingCost", "tradingCost")},
                      "restart": {"refills": rs["refills"], "paid": rs["paid"], "end": rs["end"],
                                  "net": round(rs["end"] - rs["paid"], 2), "closeouts": rs["closeouts"]}}
    return out


def main(px=None):
    px = px or load()
    D = prepare(px)
    res = {}
    for lev in (1.0, 2.0, 3.0, 5.0, 10.0):
        res[f"hold_{int(lev)}x"] = {"label": f"QQQ sürekli {int(lev)}x long (yön kuralı yok)", "kind": "hold", "cap": lev,
                                    **evaluate(D, hold_fn(lev))}
    for cap in CAPS:
        for vt in VTS:
            for sname, sc in SHORTS.items():
                key = f"c{int(cap)}_vt{'max' if vt is None else int(vt * 100)}_{sname}"
                short_cap = cap if sc is None else sc
                label = (f"en fazla {int(cap)}x, " + ("hep tavanda" if vt is None else f"hedef oynaklık %{int(vt * 100)}")
                         + {"s3": ", short en fazla 3x", "sc": ", short da aynı tavanda", "s0": ", short yok"}[sname])
                res[key] = {"label": label, "kind": "rule", "cap": cap, "vt": vt, "shorts": sname,
                            **evaluate(D, target_fn(vt, cap, short_cap))}
        print("cap", cap, "done")
    w = "1999-2026"
    rules = {k: v for k, v in res.items() if v["kind"] == "rule"}
    best = sorted(rules, key=lambda k: -rules[k][w]["cagrPct"])[:10]
    # "robust" best: highest growth among variants that never lose 75 %+ in any window and beat v6-like c3_vt25_s3 in all
    base = rules.get("c3_vt25_s3")
    ok = [k for k, v in rules.items() if all(v[x]["maxDrawdownPct"] > -75 for x in WINDOWS)
          and all(v[x]["cagrPct"] >= base[x]["cagrPct"] for x in WINDOWS)]
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "start": str(D.index[0].date()),
           "end": str(D.index[-1].date()), "capital": CAPITAL, "windows": WINDOWS,
           "assumptions": {"marginSpreadPct": SPREAD * 100, "shortBorrowPct": BORROW * 100, "costPct": COST * 100,
                           "closeOutAtPct": CLOSEOUT * 100, "refillBelow": REFILL_BELOW},
           "results": res, "bestByGrowth": best, "saferBest": sorted(ok, key=lambda k: -rules[k][w]["cagrPct"])[:10],
           "notes": "QQQ toplam getiri, günlük; kaldıraç QQQ'yu krediyle alarak (short'ta açığa satarak) kurulur. "
                    "Zorunlu kapatma günün en düşük/en yüksek fiyatına göre; hesap değeri pozisyonun %5'inin altına inerse "
                    "pozisyonun tamamı kapatılır. Geçmiş sonuçlar geleceği garanti etmez; en iyi görünen ayar geçmişe "
                    "uydurulmuş olabilir."}
    (ROOT / "lev_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for k in list(res)[:5] + best[:6]:
        print(k, {x: (res[k][x]["cagrPct"], res[k][x]["maxDrawdownPct"], res[k][x]["closeouts"], res[k][x]["restart"]["refills"])
                  for x in WINDOWS})
    return 0


if __name__ == "__main__":
    sys.exit(main())
