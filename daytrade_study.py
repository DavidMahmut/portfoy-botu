"""Research only (never trades): the owner's daily 3 long + 3 short rule (v8, from 27 Nov 2026), tested before use.

The rule: every morning the bot ranks the large US stocks, opens the best 3 longs and the best 3 shorts at the open
with 3x total leverage (each position = half the account), takes profit when a position has made 15 % on its own
money (= a 5 % move of the stock, because of the 3x), otherwise closes everything at the close. No stop.

What is open here is HOW to rank. Several rankings that only use what is known at the open are compared:
  longs   biggest gap up / gap up in an up-trend / gap down in an up-trend (buy the dip) / 20-day momentum leaders /
          momentum + gap up,
  shorts  biggest gap down / gap down in a down-trend / biggest gap up (fade) / gap up in a down-trend / 20-day
          momentum laggards.
Hourly bars of the last ~730 days (Yahoo); entry at the first bar's open, take-profit checked on hourly highs/lows,
otherwise the last close; 0.10 % round trip; no borrow fee (positions never stay overnight). SEC Rule 201: a stock
that opens 10 %+ below the previous close cannot be shorted. Today's large companies only (survivorship).

Runs on GitHub Actions (.github/workflows/daytrade.yml); writes daytrade_results.json.
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
OUT = ROOT / "daytrade_results.json"
TP = 0.05            # stock move that makes a 3x position +15 %
COST = 0.0010        # round trip
N_PICK = 3
W = 0.5              # each position = half the account (6 positions = 3x)
LONGS = {
    "gapUp": "En büyük açılış boşluğu (yukarı)",
    "gapUpTrend": "Yukarı boşluk + 200 günlük ortalamanın üstünde",
    "gapDownTrend": "Aşağı boşluk + 200 günlük ortalamanın üstünde (düşüşte al)",
    "mom": "Son 20 günün en güçlüleri",
    "momGap": "Son 20 günün güçlüleri + yukarı boşluk",
    "gapUpMid": "Yukarı boşluk %2–8 arası + yükselen trend",
}
SHORTS = {
    "gapDown": "En büyük açılış boşluğu (aşağı)",
    "gapDownTrend": "Aşağı boşluk + 200 günlük ortalamanın altında",
    "gapUpFade": "En büyük yukarı boşluk (sönmesine)",
    "gapUpWeak": "Yukarı boşluk + 200 günlük ortalamanın altında (zayıf hissenin sıçraması)",
    "mom": "Son 20 günün en zayıfları",
    "momGap": "Son 20 günün zayıfları + aşağı boşluk",
}


def features(t, bars, info):
    days = I.day_arrays(bars)
    if len(days) < 220:
        return []
    closes = np.array([d[4][-1] for d in days], dtype=float)
    rows = []
    for k in range(201, len(days)):
        d, o, h, l, c = days[k]
        pc = closes[k - 1]
        if pc <= 0 or o[0] <= 0:
            continue
        sma50 = closes[k - 50:k].mean()
        sma200 = closes[k - 200:k].mean()
        gap = o[0] / pc - 1
        mom20 = pc / closes[k - 21] - 1
        ent = float(o[0])
        hi, lo = h.max(), l.min()
        close = float(c[-1])
        long_r = (TP if hi >= ent * (1 + TP) else close / ent - 1) - COST
        short_r = (TP if lo <= ent * (1 - TP) else 1 - close / ent) - COST
        rows.append((t, d.isoformat(), gap, mom20, pc > sma200, pc > sma50, long_r, short_r,
                     bool(hi >= ent * (1 + TP)), bool(lo <= ent * (1 - TP)), info.get("cap")))
    return rows


COLS = ["t", "d", "gap", "mom", "a200", "a50", "lr", "sr", "ltp", "stp", "cap"]


def pick(day, rule, side):
    x = day
    if side > 0:
        if rule == "gapUp":
            s = x["gap"]
        elif rule == "gapUpTrend":
            s = x["gap"].where(x["a200"])
        elif rule == "gapDownTrend":
            s = (-x["gap"]).where(x["a200"])
        elif rule == "mom":
            s = x["mom"]
        elif rule == "momGap":
            s = (x["mom"].rank(pct=True) + x["gap"].rank(pct=True)).where(x["gap"] > 0)
        elif rule == "gapUpMid":
            s = x["gap"].where(x["a200"] & x["a50"] & (x["gap"] <= 0.08))
    else:
        ok = x["gap"] > -0.10                     # SEC Rule 201
        if rule == "gapDown":
            s = (-x["gap"]).where(ok)
        elif rule == "gapDownTrend":
            s = (-x["gap"]).where(ok & ~x["a200"])
        elif rule == "gapUpFade":
            s = x["gap"]
        elif rule == "gapUpWeak":
            s = x["gap"].where(~x["a200"])
        elif rule == "mom":
            s = (-x["mom"]).where(ok)
        elif rule == "momGap":
            s = ((-x["mom"]).rank(pct=True) + (-x["gap"]).rank(pct=True)).where(ok & (x["gap"] < 0))
    s = s.dropna()
    return x.loc[s.sort_values(ascending=False).index[:N_PICK]]


def stats(r):
    r = np.asarray(r, dtype=float)
    r = r[~np.isnan(r)]
    if len(r) < 10:
        return {"n": int(len(r))}
    sd = r.std(ddof=1)
    return {"n": int(len(r)), "avg": round(float(r.mean() * 100), 3), "pos": round(float((r > 0).mean() * 100), 1),
            "t": round(float(r.mean() / (sd / math.sqrt(len(r)))), 2) if sd > 0 else None}


def curve(daily_ret):
    eq = np.cumprod(1 + np.asarray(daily_ret))
    if not len(eq):
        return {}
    peak = np.maximum.accumulate(eq)
    yrs = len(eq) / 252
    return {"endX": round(float(eq[-1]), 3), "cagrPct": round((float(eq[-1]) ** (1 / yrs) - 1) * 100, 1) if eq[-1] > 0 else -100.0,
            "maxDrawdownPct": round(float((eq / peak - 1).min()) * 100, 1),
            "worstDayPct": round(float(np.min(daily_ret)) * 100, 2), "bestDayPct": round(float(np.max(daily_ret)) * 100, 2)}


def main():
    t0 = time.time()
    daily = I.load_daily(start="2021-01-01")
    spy = daily["SPY"]["Close"]
    up = (spy > spy.rolling(200).mean()).shift(1)
    reg = {d.date().isoformat(): bool(u) for d, u in up.dropna().items()}
    mv = json.loads((ROOT / "movers.json").read_text(encoding="utf-8"))
    cols = mv["screen"]["cols"]
    uni = {r["t"]: r for r in (dict(zip(cols, x)) for x in mv["screen"]["rows"])
           if (r.get("dv") or 0) >= 50 and (r.get("p") or 0) >= 10 and (r.get("cap") or 0) >= 2}
    lim = int(os.environ.get("DAYTRADE_LIMIT") or 0)
    ts = sorted(uni)[:lim] if lim else sorted(uni)
    data = I.load_hourly(ts)
    print("loaded", len(data), "of", len(ts), round(time.time() - t0), "s")
    rows = []
    for t, b in data.items():
        try:
            rows += features(t, b, uni[t])
        except Exception as e:
            print("skip", t, e)
    df = pd.DataFrame(rows, columns=COLS)
    days = sorted(df["d"].unique())
    mid = days[len(days) // 2]
    print("rows", len(df), "days", len(days), round(time.time() - t0), "s")
    side_ret = {}
    tp_rate = {}
    for side, rules in ((1, LONGS), (-1, SHORTS)):
        for rule in rules:
            rets, tps, ds = [], [], []
            for d, g in df.groupby("d", sort=True):
                p = pick(g, rule, side)
                if len(p) < N_PICK:
                    continue
                rets.append(float((p["lr"] if side > 0 else p["sr"]).mean()))
                tps.append(float((p["ltp"] if side > 0 else p["stp"]).mean()))
                ds.append(d)
            side_ret[(side, rule)] = pd.Series(rets, index=ds)
            tp_rate[(side, rule)] = float(np.mean(tps)) if tps else None
    # random baseline: 3 random large stocks a day
    rng = np.random.default_rng(7)
    base_l, base_s = [], []
    for d, g in df.groupby("d", sort=True):
        i = rng.choice(len(g), size=min(N_PICK, len(g)), replace=False)
        base_l.append(float(g["lr"].values[i].mean()))
        base_s.append(float(g["sr"].values[i].mean()))
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "from": days[0], "to": days[-1], "days": len(days),
           "tickers": int(df["t"].nunique()), "split": mid, "tpMovePct": TP * 100, "costPct": COST * 100,
           "baseline": {"long": stats(base_l), "short": stats(base_s)}, "sides": [], "combos": []}
    for (side, rule), s in side_ret.items():
        upd = [reg.get(d) for d in s.index]
        s_up = s[[u is True for u in upd]]
        s_dn = s[[u is False for u in upd]]
        out["sides"].append({"side": "long" if side > 0 else "short", "rule": rule,
                             "label": (LONGS if side > 0 else SHORTS)[rule], "all": stats(s.values),
                             "h1": stats(s[s.index < mid].values), "h2": stats(s[s.index >= mid].values),
                             "up": stats(s_up.values), "down": stats(s_dn.values),
                             "tpRatePct": round(tp_rate[(side, rule)] * 100, 1) if tp_rate[(side, rule)] is not None else None,
                             "alone": curve((s * W * N_PICK).values)})
        print("side", side, rule, out["sides"][-1]["all"], "up", out["sides"][-1]["up"].get("avg"), "down",
              out["sides"][-1]["down"].get("avg"), "tp", out["sides"][-1]["tpRatePct"])
    for lr in LONGS:
        for sr in SHORTS:
            a, b = side_ret[(1, lr)], side_ret[(-1, sr)]
            j = a.index.intersection(b.index)
            acct = (a[j] * W * N_PICK + b[j] * W * N_PICK).values
            out["combos"].append({"long": lr, "short": sr, "acct": curve(acct), "day": stats(acct),
                                  "h1": stats(acct[np.array(j) < mid]), "h2": stats(acct[np.array(j) >= mid])})
    out["combos"].sort(key=lambda c: -(c["acct"].get("cagrPct") or -999))
    good_l = [x for x in out["sides"] if x["side"] == "long" and (x["h1"].get("avg") or -1) > 0 and (x["h2"].get("avg") or -1) > 0]
    good_s = [x for x in out["sides"] if x["side"] == "short" and (x["h1"].get("avg") or -1) > 0 and (x["h2"].get("avg") or -1) > 0]
    out["bestLong"] = max(good_l or [x for x in out["sides"] if x["side"] == "long"], key=lambda x: x["all"].get("t") or -99)["rule"]
    out["bestShort"] = max(good_s or [x for x in out["sides"] if x["side"] == "short"], key=lambda x: x["all"].get("t") or -99)["rule"]
    out["notes"] = ("Bugünkü büyük şirketler (piyasa değeri ≥ 2 milyar $, günde ≥ 50 milyon $ işlem, fiyat ≥ 10 $); borsadan çıkanlar "
                    "veride yok. Giriş ilk saatlik çubuğun açılışında, kâr hedefi saatlik en yüksek/en düşük fiyatlarla kontrol "
                    "edilir, olmazsa kapanış. Gidiş-dönüş maliyet %0,10. Geçmiş sonuç geleceği garanti etmez.")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("done", round(time.time() - t0), "s; best", out["bestLong"], out["bestShort"], out["combos"][:3])
    return 0


if __name__ == "__main__":
    sys.exit(main())
