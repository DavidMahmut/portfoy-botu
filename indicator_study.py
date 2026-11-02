"""Research only (never trades): which of the 15 indicators' classic buy/sell signals actually worked on Nasdaq stocks?

For every Nasdaq-listed common stock (today's list, daily bars since 2018, real traded price >= 10 $, >= 20 million $ a
day, no reverse split in the previous 12 months) the signals below are found on day t and traded at the next day's open,
held 5, 20 or 60 trading days, 0.2 % round-trip cost. Sell signals are traded as shorts. Every trade is compared with
QQQ over the same days ("excess") AND with a baseline of buying eligible stocks on random days, because stocks that are
still listed today beat QQQ more often than they should (survivorship bias). A signal is called robust only when it beats
that baseline after costs in both halves of the period (2019-2022, 2023-2026), in most years, with a t-statistic >= 2.

Writes indicator_results.json.
"""
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import indicators as IND
import movers as MV

ROOT = Path(__file__).resolve().parent
START, MID = "2019-01-01", "2023-01-01"
COST = 0.002
HOLDS = (5, 20, 60)
MIN_PRICE, MIN_DV = 10.0, 20e6


def _prev(a):
    p = np.empty_like(a)
    p[0] = np.nan if a.dtype.kind == "f" else False
    p[1:] = a[:-1]
    return p


def up(a, b):
    """a crosses above b today (numpy arrays or scalars)."""
    b = np.broadcast_to(np.asarray(b, dtype=float), a.shape)
    with np.errstate(invalid="ignore"):
        return (a > b) & (_prev(a) <= _prev(b))


def first(cond):
    cond = np.nan_to_num(cond.astype(float), nan=0).astype(bool)
    return cond & ~_prev(cond)


def gt(a, b):
    with np.errstate(invalid="ignore"):
        return np.asarray(a > b)


SIGNALS = {   # name: (side, description, function of a dict of numpy arrays A)
    "ema_stack_up": (1, "Fiyat > EMA20 > EMA50 > EMA200 dizilimi başladı",
                     lambda A: first(gt(A["c"], A["e20"]) & gt(A["e20"], A["e50"]) & gt(A["e50"], A["e200"]))),
    "ema_golden": (1, "EMA50, EMA200'ü yukarı kesti", lambda A: up(A["e50"], A["e200"])),
    "rsi_os_up": (1, "RSI 30'un altından yukarı çıktı", lambda A: up(A["rsi"], 30.0)),
    "rsi_70_up": (1, "RSI 70'in üstüne çıktı (momentum)", lambda A: up(A["rsi"], 70.0)),
    "macd_up": (1, "MACD sinyal çizgisini yukarı kesti", lambda A: up(A["macd"], A["macdS"])),
    "macd_up_below0": (1, "MACD sıfırın altındayken yukarı kesişim", lambda A: up(A["macd"], A["macdS"]) & gt(0, A["macd"])),
    "vwap20_reclaim": (1, "Fiyat 20 günlük VWAP'ın üstüne çıktı", lambda A: up(A["c"], A["vwap20"])),
    "bb_below": (1, "Kapanış alt Bollinger bandının altında", lambda A: first(gt(A["bbL"], A["c"]))),
    "bb_squeeze_break": (1, "Sıkışmadan sonra üst banttan çıkış",
                         lambda A: gt(0.2 + 1e-12, _prev(A["bbWr"])) & up(A["c"], A["bbU"])),
    "supertrend_buy": (1, "Supertrend AL'a döndü", lambda A: (A["stDir"] == 1) & (_prev(A["stDir"]) == -1)),
    "stochrsi_up": (1, "Stoch RSI 20 altından yukarı kesişim", lambda A: up(A["skK"], A["skD"]) & gt(20, _prev(A["skK"]))),
    "ichimoku_break": (1, "Fiyat bulutun üstüne çıktı, Tenkan > Kijun",
                       lambda A: up(A["c"], np.fmax(A["spanA"], A["spanB"])) & gt(A["tenkan"], A["kijun"])),
    "adx_trend_up": (1, "ADX 25'i geçti, +DI > −DI", lambda A: up(A["adx"], 25.0) & gt(A["pdi"], A["mdi"])),
    "obv_up": (1, "OBV 20 günlük ortalamasını yukarı kesti", lambda A: up(A["obv"], A["obvE"])),
    "fib_618_uptrend": (1, "Yükseliş trendinde %61,8 Fibonacci düzeltmesinde yeşil gün",
                        lambda A: first(gt(A["c"], A["e200"]) & gt(A["fib"], 55) & gt(68, A["fib"])) & gt(A["c"], _prev(A["c"]))),
    "avwap_high_reclaim": (1, "52h zirveden Anchored VWAP'ın üstüne çıktı", lambda A: up(A["c"], A["avH"])),
    "pivot_r1_break": (1, "Kapanış R1 pivotunun üstünde", lambda A: first(gt(A["c"], A["pvR1"]))),
    "confluence_up": (1, "EMA200 üstü + Supertrend AL + MACD > sinyal + ADX > 20 ve +DI > −DI",
                      lambda A: first(gt(A["c"], A["e200"]) & (A["stDir"] == 1) & gt(A["macd"], A["macdS"])
                                      & gt(A["adx"], 20) & gt(A["pdi"], A["mdi"]))),
    "supertrend_sell": (-1, "Supertrend SAT'a döndü (short)", lambda A: (A["stDir"] == -1) & (_prev(A["stDir"]) == 1)),
    "ema_stack_down": (-1, "Fiyat < EMA20 < EMA50 < EMA200 dizilimi başladı (short)",
                       lambda A: first(gt(A["e20"], A["c"]) & gt(A["e50"], A["e20"]) & gt(A["e200"], A["e50"]))),
    "rsi_70_down": (-1, "RSI 70'in altına indi (short)", lambda A: up(-A["rsi"], -70.0)),
    "macd_down": (-1, "MACD sinyal çizgisini aşağı kesti (short)", lambda A: up(A["macdS"], A["macd"])),
    "bb_above": (-1, "Kapanış üst Bollinger bandının üstünde (short)", lambda A: first(gt(A["c"], A["bbU"]))),
}


def features(df):
    f = IND.compute(df)
    c, v = df["Close"].astype(float), df["Volume"].astype(float)
    sp = df["Stock Splits"].astype(float).fillna(0) if "Stock Splits" in df else pd.Series(0.0, index=df.index)
    spv = np.where(sp.values > 0, sp.values, 1.0)
    after = np.ones(len(c))
    acc = 1.0
    for k in range(len(c) - 1, -1, -1):
        after[k] = acc
        acc *= spv[k]
    rs = pd.Series(((sp.values > 0) & (sp.values < 1)).astype(float), index=df.index).rolling(252, min_periods=1).sum() > 0
    f["o"] = df["Open"].astype(float)
    ok = (c * after >= MIN_PRICE) & ((c * v).rolling(20).mean().shift(1) >= MIN_DV) & (~rs) & f["e200"].notna() \
        & (np.arange(len(c)) >= 200)
    # keep only what the signals need, as compact numpy arrays (3.000+ stocks x 8 years must fit in memory)
    A = {k: f[k].values.astype(np.float64 if k in ("c", "o") else np.float32) for k in ["c", "o", "e20", "e50", "e200", "rsi", "macd", "macdS", "vwap20", "bbL", "bbU", "bbWr", "stDir", "skK", "skD", "spanA", "spanB", "tenkan", "kijun", "adx", "pdi", "mdi", "obv", "obvE", "fib", "avH", "pvR1"]}
    return {"A": A, "idx": [str(d.date()) for d in f.index], "ok": ok.values.astype(bool)}


def trades(t, F, sig, side, hold, qo, qc):
    idx = F["idx"]
    o, c = F["A"]["o"], F["A"]["c"]
    out, nxt = [], 0
    for i in np.flatnonzero(sig):
        if i < nxt or i + hold >= len(c) or idx[i] < START:
            continue
        e, x = o[i + 1], c[i + hold]
        if not (e > 0 and x > 0):
            continue
        d_in, d_out = idx[i + 1], idx[i + hold]
        r = side * (x / e - 1) - COST
        q = (qc[d_out] / qo[d_in] - 1) if d_in in qo and d_out in qc else None
        out.append((d_in, r, (r - side * q) if q is not None else None))
        nxt = i + hold
    return out


def summarize(tr, base=None):
    if not tr:
        return {"trades": 0}
    r = np.array([x[1] for x in tr])
    ex = np.array([x[2] for x in tr if x[2] is not None])
    b = base or 0.0
    h1 = [x[2] - b for x in tr if x[2] is not None and x[0] < MID]
    h2 = [x[2] - b for x in tr if x[2] is not None and x[0] >= MID]
    years = {}
    for x in tr:
        if x[2] is not None:
            years.setdefault(x[0][:4], []).append(x[2] - b)
    vs = ex - b
    return {"trades": int(len(r)), "avgPct": round(float(r.mean() * 100), 2), "medianPct": round(float(np.median(r) * 100), 2),
            "winRatePct": round(float((r > 0).mean() * 100), 1),
            "avgExcessPct": round(float(ex.mean() * 100), 2) if len(ex) else None,
            "vsBaselinePct": round(float(vs.mean() * 100), 2) if len(vs) else None,
            "tStat": round(float(vs.mean() / (vs.std(ddof=1) / math.sqrt(len(vs)))), 2) if len(vs) > 2 and vs.std() > 0 else None,
            "firstHalfPct": round(float(np.mean(h1) * 100), 2) if h1 else None,
            "secondHalfPct": round(float(np.mean(h2) * 100), 2) if h2 else None,
            "yearsPositive": f"{sum(1 for v in years.values() if np.mean(v) > 0)}/{len(years)}",
            "byYearPct": {y: round(float(np.mean(v) * 100), 2) for y, v in sorted(years.items())}}


def robust(s):
    if s.get("trades", 0) < 200 or s.get("vsBaselinePct") is None:
        return False
    pos, tot = map(int, s["yearsPositive"].split("/"))
    return (s["vsBaselinePct"] > 0 and (s["firstHalfPct"] or -1) > 0 and (s["secondHalfPct"] or -1) > 0
            and (s["tStat"] or 0) >= 2 and pos >= math.ceil(tot * 0.6))


def main():
    names = MV.nasdaq_symbols()
    data = MV.download(sorted(set(names) | {"QQQ"}), start="2018-01-01", interval="1d", actions=True)
    q = data.pop("QQQ")
    qo = {str(d.date()): float(v) for d, v in q["Open"].items()}
    qc = {str(d.date()): float(v) for d, v in q["Close"].items()}
    feats = {}
    for t in list(data):
        df = data.pop(t)
        if len(df) < 260:
            continue
        df = df.copy()
        try:
            feats[t] = features(df)
        except Exception as e:
            print("features failed", t, e)
    print("tickers", len(feats))
    rng = np.random.default_rng(7)
    base = {}
    for hold in HOLDS:                               # baseline: eligible stocks bought on random days (about 1 in 20)
        tr = []
        for t, f in feats.items():
            pick = f["ok"] & (rng.random(len(f["ok"])) < 0.05)
            tr += trades(t, f, pick, 1, hold, qo, qc)
        s = summarize(tr)
        base[hold] = s
        print("baseline", hold, s.get("trades"), s.get("avgPct"), s.get("avgExcessPct"))
    res = {}
    for name, (side, desc, fn) in SIGNALS.items():
        sigs = {}
        for t, f in feats.items():
            try:
                with np.errstate(invalid="ignore"):
                    sigs[t] = np.asarray(fn(f["A"]), dtype=bool) & f["ok"]
            except Exception as e:
                print("signal failed", name, t, e)
        for hold in HOLDS:
            tr = []
            for t, f in feats.items():
                if t in sigs:
                    tr += trades(t, f, sigs[t], side, hold, qo, qc)
            be = (base[hold].get("avgExcessPct") or 0) / 100      # shorting random eligible days: about -be - 2 x cost
            b = be if side == 1 else -be - 2 * COST
            s = summarize(tr, b)
            s.update(side=side, description=desc, robust=robust(s))
            res[f"{name}|h{hold}"] = s
            print(name, hold, s.get("trades"), s.get("avgPct"), s.get("avgExcessPct"), s.get("vsBaselinePct"),
                  s.get("firstHalfPct"), s.get("secondHalfPct"), s.get("yearsPositive"), s["robust"])
    rob = sorted([k for k, s in res.items() if s["robust"]], key=lambda k: -(res[k]["tStat"] or 0))
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "tickers": len(feats),
           "filters": {"minPrice": MIN_PRICE, "minDollarVolM": MIN_DV / 1e6, "costPct": COST * 100},
           "baseline": {f"h{h}": s for h, s in base.items()}, "results": res, "robust": rob,
           "notes": "Al sinyalleri rastgele günlerde alınan uygun hisselere (baseline) göre, sat sinyalleri short olarak "
                    "QQQ'ya göre değerlendirildi. Bugün işlem gören hisseler (batan şirketler yok)."}
    (ROOT / "indicator_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("robust:", rob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
