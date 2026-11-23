"""Research only (never trades): the stock-level moves of intraday_study.py judged the honest way.

intraday_study.py counted every stock as a separate trade. On a sell-off day hundreds of stocks fall together and
then rebound together, so those trades are not independent and the t-values were far too high; its kill-switch test
also looked at signals whose result was not known yet. Here:

  cohorts      all signals of one day form one "cohort" (equal weight, at most M stocks, biggest moves first);
               a rule is judged on its cohorts (one number per day), compared with SPY over the same days.
  account      cohorts enter with a fixed share of the account (B) and leave H days later; equity curve, yearly
               growth, worst fall.
  kill switch  a cohort is traded only when the cohorts that have already finished (exit before today) earned money
               on average over the last N of them - exactly what the live bot can know.
  regimes      S&P 500 above / below its 200-day line, VIX below 20 / 20-30 / 30+.

Runs on GitHub Actions (.github/workflows/cohort.yml); writes cohort_results.json.
"""
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import intraday_study as I

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "cohort_results.json"
TS = (0.04, 0.06, 0.08, 0.12)
MS = (5, 10, 20)
BS = (0.5, 1.0, 2.0)


def spy_fwd(daily):
    """SPY close-to-close return from day d to d+H trading days, per H."""
    c = daily["SPY"]["Close"]
    out = {}
    for H in I.HOLDS:
        f = c.shift(-max(H, 0)) / c - 1 if H else pd.Series(0.0, index=c.index)
        out[H] = {d.date().isoformat(): float(v) for d, v in f.dropna().items()}
    return out


def cohort_series(x, M):
    """x: trades of one rule (columns d, m, ret, up, vix). One row per day: mean return of the day's biggest M moves."""
    x = x.assign(am=x["m"].abs()).sort_values(["d", "am"], ascending=[True, False])
    g = x.groupby("d", sort=True).head(M).groupby("d", sort=True)
    c = g["ret"].mean().to_frame("r")
    c["n"] = g.size()
    c["up"] = g["up"].first()
    c["vix"] = g["vix"].first()
    return c


def cstats(r):
    r = np.asarray(r, dtype=float)
    r = r[~np.isnan(r)]
    if len(r) < 8:
        return {"n": int(len(r))}
    sd = r.std(ddof=1)
    return {"n": int(len(r)), "avg": round(float(r.mean() * 100), 3), "med": round(float(np.median(r) * 100), 3),
            "pos": round(float((r > 0).mean() * 100), 1), "t": round(float(r.mean() / (sd / math.sqrt(len(r)))), 2) if sd > 0 else None}


def account(c, H, B, gate=None):
    """Equity curve: each day's cohort gets B x equity at entry and pays out H trading days later."""
    days = list(c.index)
    pos = {d: i for i, d in enumerate(days)}
    pend = {}
    eq, peak, mdd = 1.0, 1.0, 0.0
    for i, d in enumerate(days):
        for pay in pend.pop(i, []):
            eq += pay
        if gate is None or gate.get(d, True):
            pay = eq * B * float(c.loc[d, "r"])
            if H <= 0:
                eq += pay
            else:                      # paid out H cohort days later (cohort days stand in for trading days)
                pend.setdefault(i + H, []).append(pay)
        if eq <= 0:
            return {"cagrPct": -100.0, "maxDrawdownPct": -100.0}
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    for pays in pend.values():
        eq += sum(pays)
    yrs = max((pd.Timestamp(days[-1]) - pd.Timestamp(days[0])).days / 365.25, 0.5) if days else 1
    return {"cagrPct": round((eq ** (1 / yrs) - 1) * 100, 1) if eq > 0 else -100.0, "maxDrawdownPct": round(mdd * 100, 1),
            "endX": round(eq, 2)}


def kill_gate(c, H, N):
    """Trade a day's cohort only if the last N cohorts that had already finished (exited before it) averaged > 0."""
    days = list(c.index)
    bdays = pd.to_datetime(pd.Series(days))
    exit_ = {d: (pd.Timestamp(d) + pd.tseries.offsets.BDay(max(H, 0))).date().isoformat() for d in days}
    gate, done = {}, []
    j = 0
    order = sorted(days, key=lambda d: exit_[d])
    for d in days:
        while j < len(order) and exit_[order[j]] < d:
            done.append(float(c.loc[order[j], "r"]))
            j += 1
        last = done[-N:]
        gate[d] = len(last) < N or (sum(last) / len(last) > 0)
    return gate


def main():
    t0 = time.time()
    daily = I.load_daily(start="2021-01-01")
    reg = I.regime_table(daily)
    idx_bars = I.load_hourly(["SPY"])
    spym = I.spy_moves(idx_bars["SPY"]) if "SPY" in idx_bars else {}
    fwd = spy_fwd(daily)
    mv = json.loads((ROOT / "movers.json").read_text(encoding="utf-8"))
    cols = mv["screen"]["cols"]
    uni = {r["t"]: r for r in (dict(zip(cols, x)) for x in mv["screen"]["rows"])
           if (r.get("dv") or 0) >= 20 and (r.get("p") or 0) >= 5}
    lim = int(os.environ.get("COHORT_LIMIT") or 0)
    ts = sorted(uni)[:lim] if lim else sorted(uni)
    data = I.load_hourly(ts)
    print("loaded", len(data), round(time.time() - t0), "s")
    rows = []
    for t, b in data.items():
        try:
            rows += I.stock_rows(t, b, uni[t], reg, spym)
        except Exception as e:
            print("skip", t, e)
    del data
    df = pd.DataFrame(rows, columns=I.COLS)
    for c in I.COLS[3:]:
        if c not in ("ssr", "up"):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df = df[(df["cap"].fillna(0) >= 2) & (df["dv"].fillna(0) >= 50)]
    dates = sorted(df["d"].unique())
    mid = dates[len(dates) // 2]
    print("rows (big)", len(df), round(time.time() - t0), "s")
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "from": dates[0], "to": dates[-1], "split": mid,
           "rowsBig": int(len(df)), "grid": []}
    for setup, (mdir, side, label) in I.SETUPS.items():
        for T in TS:
            for P in I.POSF:
                for j in range(6):
                    x0 = I.select(df, setup, T, P, j)
                    if len(x0) < 50:
                        continue
                    for H in I.HOLDS:
                        for stop in (True, False):
                            r = I.returns_for(x0, side, H, stop)
                            x = x0.assign(ret=r)
                            x = x[~x["ret"].isna()]
                            if x["d"].nunique() < 30:
                                continue
                            for M in MS:
                                c = cohort_series(x, M)
                                spy = np.array([fwd[H].get(d, np.nan) for d in c.index]) * (1 if side > 0 else -1)
                                ex = c["r"].values - spy
                                item = {"setup": setup, "label": label, "T": T, "P": P, "j": j, "check": I.CHECKS[j], "H": H,
                                        "stop": stop, "M": M, "days": int(len(c)), "perDay": round(float(c["n"].mean()), 1),
                                        "all": cstats(c["r"]), "excess": cstats(ex),
                                        "h1": cstats(c["r"][c.index < mid]), "h2": cstats(c["r"][c.index >= mid]),
                                        "up": cstats(c["r"][c["up"] == True]), "down": cstats(c["r"][c["up"] == False]),
                                        "vixLo": cstats(c["r"][c["vix"] < 20]), "vixMid": cstats(c["r"][(c["vix"] >= 20) & (c["vix"] < 30)]),
                                        "vixHi": cstats(c["r"][c["vix"] >= 30])}
                                if M == 10:
                                    item["acct"] = {f"B{B}": account(c, H, B) for B in BS}
                                    for N in (10, 20):
                                        gate = kill_gate(c, H, N)
                                        taken = c[[gate[d] for d in c.index]]
                                        item[f"kill{N}"] = {**cstats(taken["r"]), "takenPct": round(len(taken) / len(c) * 100, 1),
                                                            "acctB1": account(c, H, 1.0, gate)}
                                    upg = {d: bool(c.loc[d, "up"]) for d in c.index}
                                    item["upOnlyAcctB1"] = account(c, H, 1.0, upg)
                                    item["downOnlyAcctB1"] = account(c, H, 1.0, {d: not v for d, v in upg.items()})
                                out["grid"].append(item)
        print(setup, "done", round(time.time() - t0), "s")
    g = out["grid"]
    rob = [x for x in g if x["M"] == 10 and x["all"].get("n", 0) >= 80 and (x["all"].get("t") or 0) >= 2.5
           and (x["excess"].get("avg") or 0) > 0 and (x["h1"].get("avg") or -1) > 0 and (x["h2"].get("avg") or -1) > 0]
    out["robust"] = sorted(rob, key=lambda x: -(x["all"]["t"] or 0))[:40]
    # drift as live: gap >= 8 % and first hour up, at 10:30, 2 days, stop
    x0 = I.select(df, "LR", 0.08, 0.0, 0)
    x0 = x0[(x0["gap"] >= 0.08) & (x0["h1"] >= 0.005)]
    for H in (1, 2, 4):
        r = I.returns_for(x0, 1, H, True)
        x = x0.assign(ret=r)
        x = x[~x["ret"].isna()]
        c = cohort_series(x, 6)
        out.setdefault("drift", {})[str(H)] = {"all": cstats(c["r"]), "h1": cstats(c["r"][c.index < mid]),
                                               "h2": cstats(c["r"][c.index >= mid]), "up": cstats(c["r"][c["up"] == True]),
                                               "down": cstats(c["r"][c["up"] == False]), "days": int(len(c)),
                                               "acct": {f"B{B}": account(c, H, B) for B in BS},
                                               "kill10": cstats(c[[v for v in kill_gate(c, H, 10).values()]]["r"])}
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    print("done", round(time.time() - t0), "s; robust", len(rob))
    for x in out["robust"][:20]:
        print(x["setup"], x["T"], x["P"], x["check"], x["H"], x["stop"], x["all"], x["excess"].get("avg"), x["up"].get("avg"), x["down"].get("avg"), x.get("acct"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
