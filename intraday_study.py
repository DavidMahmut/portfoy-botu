"""Research only (never trades): which moves actually make money, and which situations lose it?

Written after the first x10 week (16-20 Nov 2026): the market fell 8-10 %, every long opportunity trade lost and the
index rule saved the account by going to cash. Three questions, each answered with data and with the bot's real
timing and costs:

  A. Stocks (all liquid US stocks, hourly bars of the last ~730 days): when a stock is up or down 4 %+ at one of the
     bot's scan times, does it pay to follow the move or to fade it, and for how long? Split by company size,
     market regime (S&P 500 above / below its 200-day average), fear level (VIX) and the two halves of the period.
     Also: would pausing a rule after a losing streak (a "kill switch") have helped?
  B. Nasdaq 100 (QQQ) during the day: does the morning direction carry on until the close (intraday momentum)?
  C. Nasdaq 100 day to day since 1999: what adds money to the bot's trend rule in the periods it sits in cash -
     buying sharp drops (short-term reversal), panic peaks (VIX), follow-through days, big up days?

Runs on GitHub Actions (.github/workflows/intraday.yml). Writes intraday_results.json. Results only; the bot itself
is changed by hand after reading them.
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

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "intraday_results.json"
CHECKS = ("10:30", "11:30", "12:30", "13:30", "14:30", "15:30")   # hourly bar closes = the moments a scan could act
SCAN_OF = {0: "10:30 taraması", 1: "12:00 taramasına yakın (11:30)", 2: "12:00 taramasına yakın (12:30)",
           3: "13:30 taraması", 4: "15:00 taramasına yakın (14:30)", 5: "15:00 taramasına yakın (15:30)"}
MIN_ABS = 0.04
HOLDS = (0, 1, 2, 4)                   # extra trading days after the entry day (0 = exit at today's close)
COST_BIG, COST_SMALL = 0.0015, 0.0030  # round trip (slippage + commission); companies below 2 B$ cost more
SSR = 0.10
THRESH = (0.04, 0.06, 0.08, 0.12)
POSF = (0.0, 0.7)


def _ny(idx):
    idx = pd.DatetimeIndex(idx)
    return idx.tz_convert("America/New_York") if idx.tz is not None else idx.tz_localize("UTC").tz_convert("America/New_York")


def load_hourly(ts, period="730d"):
    import yfinance as yf
    out = {}
    for i in range(0, len(ts), 40):
        chunk = ts[i:i + 40]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(chunk, period=period, interval="1h", group_by="ticker", auto_adjust=False,
                                 prepost=False, progress=False, threads=True)
                break
            except Exception as e:
                print("retry", e)
                time.sleep(10)
        if df is None:
            continue
        for t in chunk:
            try:
                s = df[t].dropna(subset=["Open", "High", "Low", "Close"])
            except Exception:
                continue
            if len(s) > 500:
                s = s.copy()
                s.index = _ny(s.index)
                out[t] = s[["Open", "High", "Low", "Close"]].astype(float)
        time.sleep(2)
    return out


def day_arrays(bars):
    """[(date, open, high, low, close arrays)] for days whose first bar starts 09:30."""
    out = []
    for d, g in bars.groupby(bars.index.date):
        if len(g) >= 5 and g.index[0].hour == 9 and g.index[0].minute == 30:
            out.append((d, g["Open"].values, g["High"].values, g["Low"].values, g["Close"].values))
    return out


def load_daily(start="1993-01-01"):
    import yfinance as yf
    df = yf.download(["QQQ", "SPY", "^VIX", "^IRX"], start=start, interval="1d", auto_adjust=True, group_by="ticker",
                     progress=False, threads=True)
    return {t: df[t].dropna(subset=["Close"]) for t in ("QQQ", "SPY", "^VIX", "^IRX")}


# ------------------------------------------------------------------------------------------------ A: stocks
def regime_table(daily):
    """Per date: was SPY above its 200-day average at the previous close, VIX at the previous close."""
    spy = daily["SPY"]["Close"]
    sma = spy.rolling(200).mean()
    up = (spy > sma).shift(1)
    vix = daily["^VIX"]["Close"].reindex(spy.index).ffill().shift(1)
    return {d.date(): (bool(u) if u == u else None, float(v) if v == v else None) for d, u, v in zip(spy.index, up, vix)}


def spy_moves(spy_bars):
    """Per date: SPY move from the previous close to each checkpoint."""
    out, days = {}, day_arrays(spy_bars)
    for k in range(1, len(days)):
        d, o, h, l, c = days[k]
        pc = days[k - 1][4][-1]
        out[d] = [float(c[j] / pc - 1) if j < len(c) else None for j in range(6)]
    return out


def stock_rows(t, bars, info, reg, spym):
    days = day_arrays(bars)
    cap, dv = info.get("cap"), info.get("dv")
    rows = []
    hmax = max(HOLDS)
    for k in range(2, len(days)):
        d, o, h, l, c = days[k]
        pc, ppc = days[k - 1][4][-1], days[k - 2][4][-1]
        if pc <= 0 or ppc <= 0:
            continue
        ssr_prev = days[k - 1][3].min() <= ppc * (1 - SSR)
        nb = len(c)
        later = [days[k + i] for i in range(1, hmax + 1) if k + i < len(days)]
        up, vix = reg.get(d, (None, None))
        sm = spym.get(d) or [None] * 6
        for j in range(min(6, nb - 1)):
            px = float(c[j])
            m = px / pc - 1
            if abs(m) < MIN_ABS:
                continue
            hi, lo = float(h[:j + 1].max()), float(l[:j + 1].min())
            pos = (px - lo) / (hi - lo) if hi > lo else 0.5
            # the path after the entry: rest of today, then whole later days
            seg_o = [o[j + 1:]] + [x[1] for x in later]
            seg_h = [h[j + 1:]] + [x[2] for x in later]
            seg_l = [l[j + 1:]] + [x[3] for x in later]
            seg_c = [c[j + 1:]] + [x[4] for x in later]
            ends = np.cumsum([len(s) for s in seg_c])          # path length up to the end of each day
            po, ph, pl, pcl = (np.concatenate(seg_o), np.concatenate(seg_h), np.concatenate(seg_l),
                               np.concatenate(seg_c))
            sl, ss = lo * 0.999, hi * 1.001
            hit_l = np.nonzero(pl <= sl)[0]
            hit_s = np.nonzero(ph >= ss)[0]
            il = int(hit_l[0]) if len(hit_l) else 10 ** 9
            is_ = int(hit_s[0]) if len(hit_s) else 10 ** 9
            out = {}
            for H in HOLDS:
                if H >= len(ends):
                    out[H] = (None, None, None)
                    continue
                e = int(ends[H])
                xc = float(pcl[e - 1])
                lc = xc / px - 1
                xl = min(float(po[il]), sl) if il < e else xc
                xs = max(float(po[is_]), ss) if is_ < e else xc
                out[H] = (lc, xl / px - 1, (px - xs) / px)
            rows.append((t, d.isoformat(), j, m, pos, float(o[0] / pc - 1), float(c[0] / o[0] - 1), cap, dv,
                         bool(lo <= pc * (1 - SSR) or ssr_prev), up, vix, sm[j],
                         abs(px / sl - 1), abs(ss / px - 1),
                         *[out[H][0] for H in HOLDS], *[out[H][1] for H in HOLDS], *[out[H][2] for H in HOLDS]))
    return rows


COLS = (["t", "d", "j", "m", "pos", "gap", "h1", "cap", "dv", "ssr", "up", "vix", "spym", "riskL", "riskS"]
        + [f"lc{H}" for H in HOLDS] + [f"ls{H}" for H in HOLDS] + [f"ss{H}" for H in HOLDS])


def stats(r):
    r = np.asarray(r, dtype=float)
    r = r[~np.isnan(r)]
    if len(r) < 5:
        return {"n": int(len(r))}
    w, lo = r[r > 0], r[r <= 0]
    sd = r.std(ddof=1)
    return {"n": int(len(r)), "avg": round(float(r.mean() * 100), 3), "med": round(float(np.median(r) * 100), 3),
            "win": round(float(len(w) / len(r) * 100), 1),
            "pf": round(float(w.sum() / -lo.sum()), 2) if len(lo) and lo.sum() < 0 else None,
            "t": round(float(r.mean() / (sd / math.sqrt(len(r)))), 2) if sd > 0 else None}


SETUPS = {   # name: (direction of the move, side of the trade, Turkish label)
    "LR": (1, 1, "yükselene long (devam)"),
    "SR": (1, -1, "yükselene short (sönme)"),
    "LF": (-1, 1, "düşene long (tepki)"),
    "SF": (-1, -1, "düşene short (devam)"),
}


def returns_for(df, side, H, stop):
    if side > 0:
        r = df[f"ls{H}"] if stop else df[f"lc{H}"]
    else:
        r = df[f"ss{H}"] if stop else -df[f"lc{H}"]
    cost = np.where(df["cap"].fillna(0) >= 2, COST_BIG, COST_SMALL)
    return r - cost


def select(df, setup, T, P, j):
    mdir, side, _ = SETUPS[setup]
    x = df[df["j"] == j]
    if mdir > 0:
        x = x[(x["m"] >= T) & (x["pos"] >= P)]
    else:
        x = x[(x["m"] <= -T) & (x["pos"] <= 1 - P)]
    if side < 0:
        x = x[~x["ssr"]]                  # SEC Rule 201: no new short after a 10 % fall today or yesterday
    return x


def part_a(df):
    mid = sorted(df["d"].unique())[len(df["d"].unique()) // 2]
    big = (df["cap"].fillna(0) >= 2) & (df["dv"].fillna(0) >= 50)
    df = df.assign(big=big, h2=df["d"] >= mid)
    grid = []
    for setup, (mdir, side, label) in SETUPS.items():
        for T in THRESH:
            for P in POSF:
                for j in range(6):
                    x = select(df, setup, T, P, j)
                    if len(x) < 30:
                        continue
                    for H in HOLDS:
                        for stop in (True, False):
                            r = returns_for(x, side, H, stop)
                            ok = ~r.isna()
                            xx, rr = x[ok], r[ok]
                            b = xx["big"]
                            item = {"setup": setup, "label": label, "T": T, "P": P, "j": j, "check": CHECKS[j], "H": H,
                                    "stop": stop, "all": stats(rr), "big": stats(rr[b]),
                                    "bigH1": stats(rr[b & ~xx["h2"]]), "bigH2": stats(rr[b & xx["h2"]]),
                                    "bigUp": stats(rr[b & (xx["up"] == True)]), "bigDown": stats(rr[b & (xx["up"] == False)]),
                                    "bigVixLo": stats(rr[b & (xx["vix"] < 20)]),
                                    "bigVixMid": stats(rr[b & (xx["vix"] >= 20) & (xx["vix"] < 30)]),
                                    "bigVixHi": stats(rr[b & (xx["vix"] >= 30)]),
                                    "bigMktWith": stats(rr[b & (np.sign(xx["spym"].fillna(0)) == side)]),
                                    "bigMktAgainst": stats(rr[b & (np.sign(xx["spym"].fillna(0)) == -side)]),
                                    "small": stats(rr[~b])}
                            grid.append(item)
    return grid, mid


def robust(grid, key="big", min_n=150, min_t=2.5):
    out = []
    for g in grid:
        s, h1, h2 = g[key], g[key + "H1"] if key == "big" else None, g[key + "H2"] if key == "big" else None
        if s.get("n", 0) >= min_n and (s.get("t") or 0) >= min_t and (s.get("avg") or 0) > 0 \
                and h1 and h2 and (h1.get("avg") or -1) > 0 and (h2.get("avg") or -1) > 0:
            out.append(g)
    return sorted(out, key=lambda g: -(g[key]["t"] or 0))


def regime_robust(grid, reg, min_n=100, min_t=2.5):
    out = [g for g in grid if g[reg].get("n", 0) >= min_n and (g[reg].get("t") or 0) >= min_t and (g[reg].get("avg") or 0) > 0]
    return sorted(out, key=lambda g: -(g[reg]["t"] or 0))


def trades_for(df, g):
    x = select(df, g["setup"], g["T"], g["P"], g["j"])
    x = x[(x["cap"].fillna(0) >= 2) & (x["dv"].fillna(0) >= 50)]
    r = returns_for(x, SETUPS[g["setup"]][1], g["H"], g["stop"])
    x = x.assign(ret=r)
    x = x[~x["ret"].isna()]
    risk = x["riskL"] if SETUPS[g["setup"]][1] > 0 else x["riskS"]
    return x.assign(risk=risk).sort_values(["d", "m"], key=lambda s: s if s.name == "d" else -s.abs())


def sleeve(tr, H, risk=0.02, cap=1.0, slots=6, stop=True, start=3000.0):
    """Account simulation: trades of a day taken biggest move first, at most `slots` open; size = risk / stop
    distance (at most `cap` x the account) with a stop, else `cap`/2 x the account."""
    eq, peak, mdd, open_until, n = start, start, 0.0, [], 0
    days = sorted(tr["d"].unique())
    if not days:
        return {"trades": 0}
    for d, g in tr.groupby("d", sort=True):
        open_until = [u for u in open_until if u >= d]
        for _, x in g.iterrows():
            if len(open_until) >= slots:
                break
            w = min(cap, risk / max(x["risk"], 0.004)) if stop else cap / 2
            eq += eq * w * x["ret"]
            n += 1
            if eq <= 0:
                eq = 0.0
                break
            ex = (pd.Timestamp(d) + pd.tseries.offsets.BDay(H)).date().isoformat()
            open_until.append(ex if H else d)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        if eq <= 0:
            break
    yrs = max((pd.Timestamp(days[-1]) - pd.Timestamp(days[0])).days / 365.25, 0.5)
    return {"trades": n, "returnPct": round((eq / start - 1) * 100, 1),
            "cagrPct": round(((eq / start) ** (1 / yrs) - 1) * 100, 1) if eq > 0 else -100.0,
            "maxDrawdownPct": round(mdd * 100, 1)}


def kill_switch(tr, window):
    """Trade only while the average of the last `window` signals (taken or not) is above zero."""
    r = tr["ret"].values
    taken = []
    for i in range(len(r)):
        past = r[max(0, i - window):i]
        if i < window or past.mean() > 0:
            taken.append(r[i])
    return stats(taken), round(len(taken) / max(len(r), 1) * 100, 1)


# ------------------------------------------------------------------------------------------------ B: index intraday
def part_b(bars, vix):
    days = day_arrays(bars)
    rows = []
    for k in range(1, len(days) - 1):
        d, o, h, l, c = days[k]
        if len(c) < 7:
            continue
        pc = days[k - 1][4][-1]
        nxt = days[k + 1]
        rows.append({"d": d, "vix": vix.get(d), "r": [float(c[j] / pc - 1) for j in range(6)],
                     "rest": [float(c[-1] / c[j] - 1) for j in range(6)], "last": float(c[-1] / c[-2] - 1),
                     "on": float(nxt[1][0] / c[-1] - 1), "day": float(c[-1] / pc - 1)})
    out = {"days": len(rows), "from": str(rows[0]["d"]) if rows else None, "to": str(rows[-1]["d"]) if rows else None,
           "momentum": [], "lastHalfHour": [], "overnightAfterUpDay": []}
    for j in range(6):
        for a in (0.005, 0.01, 0.015, 0.02, 0.03):
            lg = [x["rest"][j] - 0.0006 for x in rows if x["r"][j] >= a]
            sh = [-x["rest"][j] - 0.0006 for x in rows if x["r"][j] <= -a]
            hv = [x["rest"][j] * (1 if x["r"][j] > 0 else -1) - 0.0006 for x in rows
                  if abs(x["r"][j]) >= a and (x["vix"] or 0) >= 25]
            out["momentum"].append({"check": CHECKS[j], "a": a, "longIfUp": stats(lg), "shortIfDown": stats(sh),
                                    "followHighVix": stats(hv)})
    for a in (0.0, 0.005, 0.01, 0.02):
        lg = [x["last"] * (1 if x["r"][5] > 0 else -1) - 0.0006 for x in rows if abs(x["r"][5]) >= a]
        out["lastHalfHour"].append({"a": a, "follow": stats(lg)})
    for a in (0.01, 0.02, 0.03):
        out["overnightAfterUpDay"].append({"a": a, "longOvernight": stats([x["on"] - 0.0006 for x in rows if x["day"] >= a]),
                                           "longOvernightAfterDown": stats([x["on"] - 0.0006 for x in rows if x["day"] <= -a])})
    return out


# ------------------------------------------------------------------------------------------------ C: index day to day
def prepare_daily(daily):
    q = daily["QQQ"]
    idx = q.index
    spy = daily["SPY"]["Close"]
    s50, s200 = spy.rolling(50).mean().reindex(idx).ffill(), spy.rolling(200).mean().reindex(idx).ffill()
    spy = spy.reindex(idx).ffill()
    C, O, H, L = q["Close"], q["Open"], q["High"], q["Low"]
    r = C.pct_change().fillna(0.0)
    d = pd.DataFrame({"C": C, "O": O, "H": H, "L": L, "r": r}, index=idx)
    d["pc"] = C.shift(1)
    d["rf"] = (daily["^IRX"]["Close"].reindex(idx).ffill().fillna(2.0) / 100 / 252).clip(lower=0)
    d["vix"] = daily["^VIX"]["Close"].reindex(idx).ffill()
    d["rv"] = r.rolling(20).std() * math.sqrt(252)
    d["dir"] = np.where(spy > s200, 1, np.where(s50 < s200, -1, 0))
    d.loc[s200.isna().values, "dir"] = 1
    d["ath"] = C.cummax()
    d["dd"] = C / d["ath"] - 1
    delta = C.diff()
    up, dn = delta.clip(lower=0), -delta.clip(upper=0)
    rs = up.ewm(alpha=1 / 2, adjust=False).mean() / dn.ewm(alpha=1 / 2, adjust=False).mean().replace(0, np.nan)
    d["rsi2"] = (100 - 100 / (1 + rs)).fillna(50)
    d["r3"] = C / C.shift(3) - 1
    d["r5"] = C / C.shift(5) - 1
    d["hi252"] = C.rolling(252, min_periods=20).max()
    d["lo20"] = C.rolling(20).min()
    d["vixmax5"] = d["vix"].rolling(5).max()
    d["above200q"] = C > C.rolling(200).mean()
    return d.dropna(subset=["pc"])


def base_target(row, st):
    """The live v7 index rule: decided on yesterday's close."""
    rv = row["rv"] if row["rv"] == row["rv"] else 0.2
    size = min(10.0, 0.5 / max(rv, 0.05))
    dip = row["dd"] <= -0.20 or (st.get("dip") and row["C"] < row["ath"])
    st["dip"] = bool(dip)
    if row["dir"] > 0:
        return size
    if row["dir"] < 0 and not dip:
        return -min(size, 3.0)
    return 0.0


def overlay_fns():
    """name -> (label, fn(row, st) -> signed exposure wanted by the overlay at this close)."""
    def rsi2(lv, entry=10, exit_=70, maxd=5):
        def f(row, st):
            if st.get("n"):
                st["n"] += 1
                if row["rsi2"] > exit_ or st["n"] > maxd:
                    st["n"] = 0
                    return 0.0
                return lv
            if row["rsi2"] < entry:
                st["n"] = 1
                return lv
            return 0.0
        return f

    def drop(lv, kind, a, hold):
        def f(row, st):
            if st.get("left", 0) > 0:
                st["left"] -= 1
                return lv
            hit = row["r"] <= -a if kind == "day" else row["r3"] <= -a if kind == "3d" else row["r5"] <= -a
            if hit:
                st["left"] = hold - 1
                return lv
            return 0.0
        return f

    def vix_ease(lv, level=30, ease=0.85, exit_vix=22, maxd=10):
        def f(row, st):
            if st.get("n"):
                st["n"] += 1
                if row["vix"] < exit_vix or st["n"] > maxd:
                    st["n"] = 0
                    return 0.0
                return lv
            if row["vixmax5"] >= level and row["vix"] <= ease * row["vixmax5"]:
                st["n"] = 1
                return lv
            return 0.0
        return f

    def ftd(lv, need_dd=0.10, day_gain=0.015, maxd=20):
        def f(row, st):
            if st.get("on"):
                st["d"] += 1
                if row["C"] < st["low"] or st["d"] > maxd:
                    st["on"] = False
                    return 0.0
                return lv
            dd = row["C"] / row["hi252"] - 1 if row["hi252"] == row["hi252"] else 0
            if st.get("lowC") is None or row["C"] < st["lowC"]:
                st["lowC"], st["since"] = row["C"], 0
            else:
                st["since"] = st.get("since", 0) + 1
            if dd <= -need_dd and st["since"] >= 3 and row["r"] >= day_gain:
                st.update(on=True, d=1, low=st["lowC"])
                st["lowC"] = None
                return lv
            if row["C"] >= row["hi252"]:
                st["lowC"] = None
            return 0.0
        return f

    def bigup(lv, a, hold=1):
        def f(row, st):
            if st.get("left", 0) > 0:
                st["left"] -= 1
                return lv
            if row["r"] >= a:
                st["left"] = hold - 1
                return lv
            return 0.0
        return f

    out = {}
    for lv in (1.0, 2.0, 3.0):
        L = int(lv)
        out[f"rsi2_{L}x"] = (f"RSI(2) 10'un altına inince {L}x long, RSI(2) 70'i geçince ya da 5 günde çık", rsi2(lv))
        out[f"rsi2s_{L}x"] = (f"RSI(2) 5'in altına inince {L}x long, RSI(2) 50'yi geçince ya da 3 günde çık", rsi2(lv, 5, 50, 3))
        for a in (0.03, 0.04):
            out[f"day{int(a * 100)}_{L}x_h1"] = (f"Nasdaq bir günde %{int(a * 100)}+ düşünce {L}x long, 1 gün", drop(lv, "day", a, 1))
            out[f"day{int(a * 100)}_{L}x_h3"] = (f"Nasdaq bir günde %{int(a * 100)}+ düşünce {L}x long, 3 gün", drop(lv, "day", a, 3))
        for a in (0.05, 0.08):
            out[f"d3_{int(a * 100)}_{L}x_h3"] = (f"Nasdaq 3 günde %{int(a * 100)}+ düşünce {L}x long, 3 gün", drop(lv, "3d", a, 3))
            out[f"d5_{int(a * 100)}_{L}x_h5"] = (f"Nasdaq 5 günde %{int(a * 100)}+ düşünce {L}x long, 5 gün", drop(lv, "5d", a, 5))
        out[f"vix_{L}x"] = (f"VIX 30'u geçip zirvesinden %15 inince {L}x long, VIX 22'nin altına inince ya da 10 günde çık", vix_ease(lv))
        out[f"ftd_{L}x"] = (f"Düşüşten sonra takip günü (dipten 4. gün ya da sonra +%1,5) {L}x long, dibin altına inince çık", ftd(lv))
        for a in (0.02, 0.03):
            out[f"bigup{int(a * 100)}_{L}x"] = (f"Nasdaq bir günde %{int(a * 100)}+ yükselince ertesi gün {L}x long", bigup(lv, a))
    return out


BEARS = {"2000-2002": ("2000-03-24", "2002-10-09"), "2007-2009": ("2007-10-31", "2009-03-09"),
         "2020": ("2020-02-19", "2020-04-30"), "2022": ("2021-11-19", "2022-12-28"), "2025": ("2025-02-19", "2025-05-30")}
WINDOWS = {"1999-2026": "1999-03-10", "2010-2026": "2010-02-11", "2018-2026": "2018-01-02"}


def simulate(D, overlay=None, mode="cash", exec_at="close", cost=0.0006, spread=0.015, borrow=0.005):
    """Daily account with the overnight and the day session apart. Base rule: decided on yesterday's close,
    executed at today's open (the bot trades at ~10:00). Overlay: decided at the close (the bot can act at 15:50)
    and held from that close (exec_at="close") or from the next open (exec_at="open").
    mode "cash": the overlay only acts while the base rule is flat; "add": on top of the base (max 10x)."""
    rows = D.to_dict("records")
    E, x, pend_base, pend_ov = 1.0, 0.0, 0.0, 0.0
    st_b, st_o = {}, {}
    curve, trades, cur_ov, ov_ret, closeouts = [], [], 0.0, 1.0, 0
    ov_now = 0.0
    for i, row in enumerate(rows):
        if i == 0:
            curve.append(E)
            pend_base = base_target(row, st_b)
            continue
        ro = row["O"] / row["pc"] - 1
        ri = row["C"] / row["O"] - 1
        # overnight on yesterday's closing position
        E *= 1 + x * ro
        # at the open: base rule (and an overlay executed at the open)
        new = pend_base + (pend_ov if exec_at == "open" else ov_now)
        if exec_at == "open":
            ov_now = pend_ov
        new = max(-10.0, min(10.0, new))
        E *= 1 - abs(new - x) * cost
        x = new
        # forced close-out during the day (5 % of exposure)
        worst = (row["L"] / row["O"] - 1) if x > 0 else (row["H"] / row["O"] - 1)
        if x != 0 and 1 + x * worst <= 0.05 * abs(x) * (1 + worst):
            E *= max(0.0, 1 + x * worst) * (1 - 0.002)
            x, closeouts = 0.0, closeouts + 1
            ov_now = 0.0
        else:
            E *= 1 + x * ri
        # financing for the day
        if x > 1:
            E -= E * (x - 1) * (row["rf"] + spread / 252)
        elif x >= 0:
            E += E * (1 - x) * row["rf"]
        else:
            E += E * row["rf"] - E * abs(x) * borrow / 252
        if E <= 0:
            E = 0.0
        # decisions at this close
        pend_base = base_target(row, st_b)
        want = overlay(row, st_o) if overlay else 0.0
        if mode == "cash" and pend_base != 0:
            want = 0.0
            st_o.clear()
        if want and not cur_ov:
            trades.append({"d": str(D.index[i].date()), "lv": want, "start": E})
        if cur_ov and not want and trades:
            trades[-1]["ret"] = E / trades[-1]["start"] - 1
        cur_ov = want
        if exec_at == "close":
            target = max(-10.0, min(10.0, x - ov_now + want))
            E *= 1 - abs(target - x) * cost
            x, ov_now = target, want
        else:
            pend_ov = want
        curve.append(E)
    s = pd.Series(curve, index=D.index)
    return s, [t for t in trades if "ret" in t], closeouts


def metrics(s, start, end=None):
    s = s[(s.index >= start) & ((s.index <= end) if end else True)]
    if len(s) < 5 or s.iloc[0] <= 0:
        return None
    s = s / s.iloc[0]
    yrs = max((s.index[-1] - s.index[0]).days / 365.25, 0.1)
    endv = float(s.iloc[-1])
    return {"returnPct": round((endv - 1) * 100, 1),
            "cagrPct": round((endv ** (1 / yrs) - 1) * 100, 2) if endv > 0 else -100.0,
            "maxDrawdownPct": round(float((s / s.cummax() - 1).min()) * 100, 1)}


def part_c(daily):
    D = prepare_daily(daily)
    base, _, bco = simulate(D)
    res = {"base": {"label": "Şu anki kural (v7 endeks kolu)", "closeouts": bco,
                    **{w: metrics(base, s) for w, s in WINDOWS.items()},
                    "bears": {k: metrics(base, a, b) for k, (a, b) in BEARS.items()}}}
    for name, (label, fn) in overlay_fns().items():
        for mode in ("cash", "add"):
            for ex in ("close", "open"):
                s, tr, co = simulate(D, fn, mode, ex)
                rets = [t["ret"] for t in tr]
                res[f"{name}|{mode}|{ex}"] = {
                    "label": label, "mode": mode, "exec": ex, "closeouts": co,
                    "trades": len(rets), "winPct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1) if rets else None,
                    "avgTradePct": round(float(np.mean(rets)) * 100, 2) if rets else None,
                    "recentTrades": [{"d": t["d"], "retPct": round(t["ret"] * 100, 2)} for t in tr[-8:]],
                    **{w: metrics(s, st) for w, st in WINDOWS.items()},
                    "bears": {k: metrics(s, a, b) for k, (a, b) in BEARS.items()}}
        print("overlay", name, res[f"{name}|cash|close"]["1999-2026"])
    w = "1999-2026"
    b = res["base"]
    better = [k for k, v in res.items() if k != "base" and v[w] and all(
        v[x] and b[x] and v[x]["cagrPct"] > b[x]["cagrPct"] for x in WINDOWS)
              and v[w]["maxDrawdownPct"] >= b[w]["maxDrawdownPct"] - 5]
    last = D.iloc[-1]
    now = {"date": str(D.index[-1].date()), "qqq": round(float(last["C"]), 2), "ddPct": round(float(last["dd"]) * 100, 1),
           "rsi2": round(float(last["rsi2"]), 1), "vix": round(float(last["vix"]), 1), "r3Pct": round(float(last["r3"]) * 100, 1),
           "r5Pct": round(float(last["r5"]) * 100, 1), "dir": int(last["dir"])}
    return res, sorted(better, key=lambda k: -res[k][w]["cagrPct"]), now


# ------------------------------------------------------------------------------------------------ main
def main():
    t0 = time.time()
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "checks": CHECKS, "holds": HOLDS,
           "costs": {"bigRoundTripPct": COST_BIG * 100, "smallRoundTripPct": COST_SMALL * 100}}
    daily = load_daily()
    reg = regime_table(daily)
    vixd = {d.date(): float(v) for d, v in daily["^VIX"]["Close"].shift(1).dropna().items()}
    idx_bars = load_hourly(["SPY", "QQQ"])
    spym = spy_moves(idx_bars["SPY"]) if "SPY" in idx_bars else {}
    # C first: cheap and independent of the hourly downloads
    try:
        out["index"], out["indexBetter"], out["indexNow"] = part_c(daily)
    except Exception as e:
        out["indexError"] = f"{type(e).__name__}: {e}"
        print("part C failed", e)
    try:
        out["intradayIndex"] = part_b(idx_bars["QQQ"], vixd) if "QQQ" in idx_bars else None
    except Exception as e:
        out["intradayIndexError"] = f"{type(e).__name__}: {e}"
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("index parts done", round(time.time() - t0), "s")
    if os.environ.get("INTRADAY_INDEX_ONLY") == "1":
        return 0
    mv = json.loads((ROOT / "movers.json").read_text(encoding="utf-8"))
    cols = mv["screen"]["cols"]
    uni = {r["t"]: r for r in (dict(zip(cols, x)) for x in mv["screen"]["rows"])
           if (r.get("dv") or 0) >= 20 and (r.get("p") or 0) >= 5}
    lim = int(os.environ.get("INTRADAY_LIMIT") or 0)
    ts = sorted(uni)[:lim] if lim else sorted(uni)
    data = load_hourly(ts)
    print("loaded", len(data), "of", len(ts), round(time.time() - t0), "s")
    rows = []
    for t, b in data.items():
        try:
            rows += stock_rows(t, b, uni[t], reg, spym)
        except Exception as e:
            print("skip", t, e)
    del data
    df = pd.DataFrame(rows, columns=COLS)
    for c in COLS[3:]:
        if c not in ("ssr", "up"):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    print("rows", len(df), round(time.time() - t0), "s")
    out["stocks"] = {"tickers": int(df["t"].nunique()), "rows": int(len(df)), "from": df["d"].min(), "to": df["d"].max()}
    grid, mid = part_a(df)
    out["stocks"]["split"] = mid
    out["grid"] = grid
    rob = robust(grid)
    out["robust"] = [{k: g[k] for k in ("setup", "label", "T", "P", "check", "j", "H", "stop", "big", "bigH1", "bigH2",
                                        "bigUp", "bigDown", "bigVixHi")} for g in rob[:40]]
    out["robustUp"] = [{k: g[k] for k in ("setup", "label", "T", "P", "check", "j", "H", "stop", "bigUp", "bigDown")}
                       for g in regime_robust(grid, "bigUp")[:25]]
    out["robustDown"] = [{k: g[k] for k in ("setup", "label", "T", "P", "check", "j", "H", "stop", "bigUp", "bigDown")}
                         for g in regime_robust(grid, "bigDown")[:25]]
    # account simulations and kill switch for the strongest few, plus the two live plays
    live = [{"setup": "LR", "T": 0.08, "P": 0.0, "j": 0, "H": 2, "stop": True, "name": "drift (yaklaşık)"},
            {"setup": "LR", "T": 0.06, "P": 0.7, "j": 1, "H": 2, "stop": True, "name": "haber takibi fiyat şartı 12:00"},
            {"setup": "LR", "T": 0.06, "P": 0.7, "j": 3, "H": 2, "stop": True, "name": "haber takibi fiyat şartı 13:30"},
            {"setup": "LR", "T": 0.06, "P": 0.7, "j": 4, "H": 2, "stop": True, "name": "haber takibi fiyat şartı 15:00"}]
    picks = live + [dict(g, name="test") for g in rob[:12]]
    sims = []
    for g in picks:
        tr = trades_for(df, g)
        if g["name"].startswith("drift"):
            tr = tr[(tr["gap"] >= 0.08) & (tr["h1"] >= 0.005)]
        item = {"name": g["name"], "setup": g["setup"], "T": g["T"], "P": g["P"], "check": CHECKS[g["j"]], "H": g["H"],
                "stop": g["stop"], "all": stats(tr["ret"]), "up": stats(tr[tr["up"] == True]["ret"]),
                "down": stats(tr[tr["up"] == False]["ret"])}
        for risk, cap in ((0.01, 0.5), (0.02, 1.0), (0.03, 1.0)):
            item[f"sleeve_r{int(risk * 100)}_c{int(cap * 10)}"] = sleeve(tr, g["H"], risk, cap, 6, g["stop"])
        item["sleeveUpOnly"] = sleeve(tr[tr["up"] == True], g["H"], 0.02, 1.0, 6, g["stop"])
        for wdw in (10, 20, 40):
            s, pct = kill_switch(tr, wdw)
            item[f"kill{wdw}"] = {**s, "takenPct": pct}
        sims.append(item)
    out["sims"] = sims
    out["notes"] = ("Saatlik çubuklar (Yahoo, son ~730 gün); giriş, kontrol saatindeki çubuğun kapanışı; zarar-kes o ana kadarki "
                    "günün en düşüğü (long) ya da en yükseği (short), sonraki saatlik çubuklarda kontrol edilir. Maliyet gidiş-dönüş "
                    "%0,15 (2 milyar $ altı şirketlerde %0,30). Şirket büyüklüğü ve hacim bugünkü değerler; borsadan çıkan "
                    "şirketler veride yok. 'big' = piyasa değeri ≥ 2 milyar $ ve günde ≥ 50 milyon $ işlem. Rejim: S&P 500 bir "
                    "önceki kapanışta 200 günlük ortalamasının üstünde (Up) ya da altında (Down).")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("done", round(time.time() - t0), "s; robust", len(rob))
    for g in rob[:15]:
        print(g["setup"], g["T"], g["P"], g["check"], g["H"], g["stop"], g["big"], "up", g["bigUp"].get("avg"), "down", g["bigDown"].get("avg"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
