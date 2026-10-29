"""Research only (never trades): what if the bot watched for Nasdaq 100 drops of 20 % or more?

Runs on GitHub Actions (.github/workflows/dip.yml) because it needs Yahoo Finance data. Writes dip_results.json.
QQQ daily total-return closes since March 1999 (so the 2000-2002 and 2008 crashes are included), SPY for the market
trend, 13-week T-bill yield (^IRX) for cash. 3x and -3x funds before they existed (TQQQ/SQQQ start in 2010) are
simulated from QQQ's daily return minus financing and fees; the simulation is checked against the real TQQQ.
Signals use the close; the position applies from the next day. Each change in position costs 0.10 %.
"""
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
CAPITAL = 3000.0
COST = 0.0010                 # slippage + commission per unit of position change
FEE3 = 0.0095                 # yearly expense ratio of the 3x funds
WINDOWS = {"1999-2026": "1999-03-10", "2010-2026": "2010-02-11", "2018-2026": "2018-01-02"}


def load():
    import yfinance as yf
    df = yf.download(["QQQ", "SPY", "TQQQ", "^IRX"], start="1993-01-01", interval="1d", auto_adjust=True,
                     group_by="ticker", progress=False, threads=True)
    px = {t: df[t]["Close"].dropna() for t in ("QQQ", "SPY", "TQQQ", "^IRX")}
    return px


def prepare(px):
    q = px["QQQ"]
    idx = q.index
    spy_full = px["SPY"]
    sma50 = spy_full.rolling(50).mean().reindex(idx).ffill()
    sma200 = spy_full.rolling(200).mean().reindex(idx).ffill()
    spy = spy_full.reindex(idx).ffill()
    rf = (px["^IRX"].reindex(idx).ffill().fillna(2.0) / 100 / 252).clip(lower=0)
    r = q.pct_change().fillna(0.0)
    r3 = 3 * r - 2 * rf - FEE3 / 252          # long 3x: borrows 2x at roughly the T-bill rate
    ri = -3 * r + 4 * rf - FEE3 / 252         # short 3x: collateral earns interest on 1 + 3 notional
    ath = q.cummax()
    dd = q / ath - 1
    rv = r.rolling(20).std() * math.sqrt(252)
    direction = pd.Series(np.where(spy > sma200, 1, np.where(sma50 < sma200, -1, 0)), index=idx)
    direction[sma200.isna()] = 1
    return pd.DataFrame({"q": q, "r": r, "r3": r3, "ri": ri, "rf": rf, "dd": dd, "ath": ath, "rv": rv,
                         "dir": direction})


def run(D, weights_fn):
    """weights_fn(i, row, state) -> (wQ, w3, wI) decided at day i's close, applied to day i+1."""
    w = (0.0, 0.0, 0.0)
    eq, curve, inmkt, state = 1.0, [], [], {}
    rows = D.to_dict("records")
    for i, row in enumerate(rows):
        if i > 0:
            wq, w3, wi = w
            cash = 1 - wq - w3 - wi
            day = wq * row["r"] + w3 * row["r3"] + wi * row["ri"] + cash * row["rf"]
            eq *= 1 + day
        new = weights_fn(i, row, state)
        turn = sum(abs(a - b) for a, b in zip(new, w))
        eq *= 1 - turn * COST
        w = new
        curve.append(eq)
        inmkt.append(1 if sum(new) > 0 else 0)
    return pd.Series(curve, index=D.index), pd.Series(inmkt, index=D.index)


def expo(w):
    return w[0] + 3 * w[1] - 3 * w[2]


def v5_weights(target):
    if target >= 0:
        a = max(0.0, (target - 1) / 2)
        return (target if target <= 1 else 1 - a, a, 0.0)
    return (0.0, 0.0, min(1.0, -target / 3))


def make_v5(overlay=None, thr=-0.20):
    def fn(i, row, st):
        rv = row["rv"] if not math.isnan(row["rv"]) else 0.2
        tgt = row["dir"] * min(3.0, 0.25 / max(rv, 0.05))
        dip = row["dd"] <= thr or (st.get("dip") and row["q"] < row["ath"])
        st["dip"] = bool(dip)
        if overlay == "noshort" and dip:
            tgt = max(tgt, 0.0)
        elif overlay == "long1" and dip:
            tgt = max(tgt, 1.0)
        elif overlay == "long3" and dip:
            tgt = 3.0
        cur = st.get("w", (0.0, 0.0, 0.0))
        now = expo(cur)
        if abs(tgt - now) >= 0.3 or ((tgt == 0) != (now == 0)):
            cur = v5_weights(tgt)
        st["w"] = cur
        return cur
    return fn


def make_dip(thr, lev):
    """Cash (T-bills) until QQQ is `thr` below its all-time high, then long until a new all-time high."""
    def fn(i, row, st):
        if not st.get("on") and row["dd"] <= thr:
            st["on"] = True
        elif st.get("on") and row["dd"] >= 0:
            st["on"] = False
        if not st.get("on"):
            return (0.0, 0.0, 0.0)
        return (1.0, 0.0, 0.0) if lev == 1 else (0.0, 1.0, 0.0)
    return fn


def hold(lev):
    return lambda i, row, st: (1.0, 0.0, 0.0) if lev == 1 else (0.0, 1.0, 0.0)


def metrics(curve, inmkt, rf):
    s = curve / curve.iloc[0] * CAPITAL
    r = s.pct_change().dropna()
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    cagr = (s.iloc[-1] / CAPITAL) ** (1 / yrs) - 1 if s.iloc[-1] > 0 else -1.0
    ex = r - rf.reindex(r.index).fillna(0)
    yearly = (1 + r).groupby(r.index.year).prod() - 1
    return {"endValue": round(float(s.iloc[-1]), 2), "cagrPct": round(cagr * 100, 2),
            "maxDrawdownPct": round(float((s / s.cummax() - 1).min()) * 100, 2),
            "volPct": round(float(r.std() * math.sqrt(252)) * 100, 2),
            "sharpe": round(float(ex.mean() / ex.std() * math.sqrt(252)), 2) if ex.std() > 0 else None,
            "inMarketPct": round(float(inmkt.reindex(s.index).mean()) * 100, 1),
            "worstYearPct": round(float(yearly.min()) * 100, 1), "bestYearPct": round(float(yearly.max()) * 100, 1),
            "yearly": {str(k): round(float(v) * 100, 1) for k, v in yearly.items()}}


def episodes(D, thr=-0.20):
    """Every fall of QQQ of `thr` or more from an all-time high, with what a buyer at the trigger got."""
    out, i, n = [], 0, len(D)
    q, dd, ath = D["q"].values, D["dd"].values, D["ath"].values
    r, r3 = D["r"].values, D["r3"].values
    dates = D.index
    while i < n:
        if dd[i] <= thr:
            peak_val = ath[i]
            peak_i = int(np.where(q[:i + 1] == peak_val)[0][-1])
            j = i
            while j < n and q[j] < peak_val:
                j += 1
            seg = slice(i, min(j, n - 1) + 1)
            trough_i = i + int(np.argmin(q[seg]))
            g1 = float(np.prod(1 + r[i + 1:min(j, n - 1) + 1]) - 1)
            path3 = np.cumprod(1 + r3[i + 1:min(j, n - 1) + 1])
            g3 = float(path3[-1] - 1) if len(path3) else 0.0
            worst3 = float(path3.min() - 1) if len(path3) else 0.0
            ev = {"peak": str(dates[peak_i].date()), "trigger": str(dates[i].date()),
                  "trough": str(dates[trough_i].date()), "troughPct": round(float(dd[trough_i]) * 100, 1),
                  "afterTriggerWorstPct": round(float(q[trough_i] / q[i] - 1) * 100, 1),
                  "afterTriggerWorst3xPct": round(worst3 * 100, 1),
                  "recovered": str(dates[j].date()) if j < n else None,
                  "daysTriggerToRecovery": int((dates[min(j, n - 1)] - dates[i]).days),
                  "buyAtTriggerToRecovery1xPct": round(g1 * 100, 1), "buyAtTriggerToRecovery3xPct": round(g3 * 100, 1),
                  "open": j >= n}
            out.append(ev)
            i = j + 1
        else:
            i += 1
    return out


def main(px=None):
    px = px or load()
    D = prepare(px)
    strategies = [
        ("qqq", "QQQ al-tut", hold(1)),
        ("q3", "3x Nasdaq al-tut (TQQQ benzeri)", hold(3)),
        ("dip20", "Yalnızca düşüş takibi: zirveden %20 düşünce QQQ al, yeni zirvede sat", make_dip(-0.20, 1)),
        ("dip20x3", "Aynısı 3x fonla", make_dip(-0.20, 3)),
        ("dip30", "Zirveden %30 düşünce QQQ al, yeni zirvede sat", make_dip(-0.30, 1)),
        ("dip30x3", "Aynısı 3x fonla", make_dip(-0.30, 3)),
        ("v5", "v5 endeks kolu (şu anki kural)", make_v5()),
        ("v5_noshort", "v5 + Nasdaq %20 düşmüşse short açma", make_v5("noshort")),
        ("v5_long1", "v5 + Nasdaq %20 düşmüşse en az 1x long", make_v5("long1")),
        ("v5_long3", "v5 + Nasdaq %20 düşmüşse 3x long", make_v5("long3")),
    ]
    res = {}
    for key, label, fn in strategies:
        curve, inm = run(D, fn)
        res[key] = {"label": label}
        for wname, start in WINDOWS.items():
            seg = curve[curve.index >= start]
            res[key][wname] = metrics(seg, inm, D["rf"])
        print(key, {w: (res[key][w]["cagrPct"], res[key][w]["maxDrawdownPct"]) for w in WINDOWS})
    # check the 3x simulation against the real TQQQ
    check = None
    if "TQQQ" in px and len(px["TQQQ"]):
        t = px["TQQQ"]
        sim = (1 + D["r3"]).cumprod().reindex(t.index).dropna()
        t = t.reindex(sim.index)
        yrs = (t.index[-1] - t.index[0]).days / 365.25
        check = {"from": str(t.index[0].date()), "realCagrPct": round(((t.iloc[-1] / t.iloc[0]) ** (1 / yrs) - 1) * 100, 2),
                 "simCagrPct": round(((sim.iloc[-1] / sim.iloc[0]) ** (1 / yrs) - 1) * 100, 2),
                 "dailyCorr": round(float(t.pct_change().corr(sim.pct_change())), 4)}
    last = D.iloc[-1]
    now = {"date": str(D.index[-1].date()), "qqq": round(float(last["q"]), 2), "ath": round(float(last["ath"]), 2),
           "athDate": str(D.index[D["q"] == last["ath"]][-1].date()), "drawdownPct": round(float(last["dd"]) * 100, 2),
           "triggerPrice": round(float(last["ath"]) * 0.8, 2)}
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "start": str(D.index[0].date()),
           "end": str(D.index[-1].date()), "capital": CAPITAL, "windows": WINDOWS, "results": res,
           "episodes": episodes(D), "tqqqCheck": check, "now": now,
           "notes": "QQQ toplam getiri (temettü dahil), günlük kapanış. Nakit 13 haftalık hazine bonosu faizi kazanır. 2010 "
                    "öncesi 3x ve −3x fonlar QQQ'nun günlük getirisinden simüle edildi (finansman ve %0,95 masraf düşülerek). "
                    "Sinyal kapanışta, pozisyon ertesi gün; her pozisyon değişikliği %0,10 maliyet."}
    (ROOT / "dip_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
