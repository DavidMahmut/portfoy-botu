"""Technical indicators computed from free daily (and optional 5-minute) Yahoo Finance bars.

  1 EMA 20/50/200          trend direction                     9 Stochastic RSI 14/14/3/3  short-term momentum
  2 RSI 14 (Wilder)        overbought / oversold               10 Ichimoku 9/26/52        trend, support/resistance
  3 MACD 12/26/9           momentum change                     11 ADX / DMI 14            trend strength
  4 VWAP                   session VWAP (5-minute bars) and    12 OBV                     volume flow
                           20-day rolling VWAP (daily bars)    13 Fibonacci retracement   52-week swing levels
  5 Volume profile         point of control, last 60 days      14 Anchored VWAP           from the 52-week high / low
  6 Bollinger 20/2         volatility, %B, squeeze             15 Pivot points (classic)  from the previous session
  7 Supertrend 10/3        trend and buy/sell flips
  8 ATR 14 (Wilder)        volatility and stop distance

compute(df) returns the full series (for back-tests); snapshot(df, session_vwap) the latest values for the dashboard.
"""
import numpy as np
import pandas as pd

SNAP_COLS = ["e20", "e50", "e200", "rsi", "macdH", "macdX", "macdUp", "vwap", "vwap20", "poc", "bbU", "bbL", "bbPb",
             "bbSq", "st", "stDir", "stAge", "skK", "skD", "skX", "ichTop", "ichBot", "ichPos", "ichTk", "adx", "pdi",
             "mdi", "obvUp", "fib", "avH", "avL", "pvP", "pvR1", "pvR2", "pvS1", "pvS2", "atrD"]


def ema(x, n):
    return x.ewm(span=n, adjust=False).mean()


def rma(x, n):
    return x.ewm(alpha=1 / n, adjust=False).mean()


def true_range(h, l, c):
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def supertrend(h, l, c, n=10, mult=3.0):
    atr = rma(true_range(h, l, c), n).values
    hl2 = ((h + l) / 2).values
    cv = c.values
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    m = len(cv)
    fub, flb, d = np.full(m, np.nan), np.full(m, np.nan), np.zeros(m, dtype=int)
    for i in range(m):
        if i == 0 or not np.isfinite(atr[i - 1] if i else np.nan):
            fub[i], flb[i], d[i] = ub[i], lb[i], 1
            continue
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or cv[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or cv[i - 1] < flb[i - 1]) else flb[i - 1]
        if d[i - 1] == -1:
            d[i] = 1 if cv[i] > fub[i - 1] else -1
        else:
            d[i] = -1 if cv[i] < flb[i - 1] else 1
    level = np.where(d == 1, flb, fub)
    return pd.Series(level, index=c.index), pd.Series(d, index=c.index)


def anchored_vwap(h, l, c, v, which="high", window=252):
    """VWAP of daily typical prices from the bar of the highest high (or lowest low) in the last `window` bars."""
    tp = ((h + l + c) / 3).values
    vv = v.values.astype(float)
    ctv, cv = np.cumsum(np.nan_to_num(tp * vv)), np.cumsum(np.nan_to_num(vv))
    src = (h if which == "high" else l).values.astype(float)
    m = len(src)
    out = np.full(m, np.nan)
    if m < 2:
        return pd.Series(out, index=c.index)
    w = min(window, m)
    from numpy.lib.stride_tricks import sliding_window_view
    win = sliding_window_view(np.nan_to_num(src, nan=(-np.inf if which == "high" else np.inf)), w)
    arg = (np.argmax(win, axis=1) if which == "high" else np.argmin(win, axis=1)) + np.arange(len(win))
    for j, a in enumerate(arg):
        i = j + w - 1
        prev_tv, prev_v = (ctv[a - 1], cv[a - 1]) if a > 0 else (0.0, 0.0)
        vol = cv[i] - prev_v
        out[i] = (ctv[i] - prev_tv) / vol if vol > 0 else np.nan
    return pd.Series(out, index=c.index)


def compute(df, avwap=True):
    h, l, c = (df[k].astype(float) for k in ("High", "Low", "Close"))
    v = df["Volume"].astype(float).fillna(0)
    f = pd.DataFrame(index=df.index)
    f["c"] = c
    f["e20"], f["e50"], f["e200"] = ema(c, 20), ema(c, 50), ema(c, 200)
    d = c.diff()
    up, dn = rma(d.clip(lower=0), 14), rma((-d).clip(lower=0), 14)
    f["rsi"] = np.where(dn > 0, 100 - 100 / (1 + up / dn.replace(0, np.nan)), 100.0)
    macd = ema(c, 12) - ema(c, 26)
    f["macd"], f["macdS"] = macd, ema(macd, 9)
    f["macdH"] = f["macd"] - f["macdS"]
    tp = (h + l + c) / 3
    f["vwap20"] = (tp * v).rolling(20).sum() / v.rolling(20).sum().replace(0, np.nan)
    mid, sd = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    f["bbU"], f["bbL"], f["bbM"] = mid + 2 * sd, mid - 2 * sd, mid
    f["bbPb"] = (c - f["bbL"]) / (f["bbU"] - f["bbL"]).replace(0, np.nan)
    f["bbW"] = (f["bbU"] - f["bbL"]) / mid
    f["bbWr"] = f["bbW"].rolling(126, min_periods=60).rank(pct=True)
    tr = true_range(h, l, c)
    f["atr"] = rma(tr, 14)
    f["st"], f["stDir"] = supertrend(h, l, c)
    r = pd.Series(f["rsi"].values, index=df.index)
    lo, hi = r.rolling(14).min(), r.rolling(14).max()
    sk = ((r - lo) / (hi - lo).replace(0, np.nan) * 100).rolling(3).mean()
    f["skK"], f["skD"] = sk, sk.rolling(3).mean()
    tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2
    kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
    f["tenkan"], f["kijun"] = tenkan, kijun
    f["spanA"] = ((tenkan + kijun) / 2).shift(26)
    f["spanB"] = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
    upm, dnm = h.diff(), -l.diff()
    pdm = pd.Series(np.where((upm > dnm) & (upm > 0), upm, 0.0), index=df.index)
    mdm = pd.Series(np.where((dnm > upm) & (dnm > 0), dnm, 0.0), index=df.index)
    atrw = rma(tr, 14).replace(0, np.nan)
    f["pdi"], f["mdi"] = 100 * rma(pdm, 14) / atrw, 100 * rma(mdm, 14) / atrw
    f["adx"] = rma(100 * (f["pdi"] - f["mdi"]).abs() / (f["pdi"] + f["mdi"]).replace(0, np.nan), 14)
    obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
    f["obv"], f["obvE"] = obv, ema(obv, 20)
    hi52, lo52 = h.rolling(252, min_periods=60).max(), l.rolling(252, min_periods=60).min()
    f["hi52"], f["lo52"] = hi52, lo52
    f["fib"] = (hi52 - c) / (hi52 - lo52).replace(0, np.nan) * 100      # 0 = at the high, 100 = at the low
    if avwap:
        f["avH"], f["avL"] = anchored_vwap(h, l, c, v, "high"), anchored_vwap(h, l, c, v, "low")
    ph, pl, pc = h.shift(1), l.shift(1), c.shift(1)
    p = (ph + pl + pc) / 3
    f["pvP"], f["pvR1"], f["pvS1"], f["pvR2"], f["pvS2"] = p, 2 * p - pl, 2 * p - ph, p + (ph - pl), p - (ph - pl)
    return f


def poc(df, days=60, bins=24):
    """Volume profile from daily bars: each day's volume spread evenly over its high-low range; returns the price of
    the busiest bin (point of control)."""
    d = df.tail(days)
    a, b = d["Low"].astype(float).values, d["High"].astype(float).values
    vv = np.nan_to_num(d["Volume"].astype(float).values)
    lo, hi = np.nanmin(a), np.nanmax(b)
    if not (hi > lo > 0):
        return None
    edges = np.linspace(lo, hi, bins + 1)
    ov = np.clip(np.minimum(edges[1:][None, :], b[:, None]) - np.maximum(edges[:-1][None, :], a[:, None]), 0, None)
    span = ov.sum(axis=1)
    flat = span <= 0                                   # a day with no range: all volume into its price's bin
    w = np.where(flat[:, None], 0.0, ov / np.where(span > 0, span, 1)[:, None])
    if flat.any():
        idx = np.clip(np.searchsorted(edges, a[flat]) - 1, 0, bins - 1)
        w[np.flatnonzero(flat), idx] = 1.0
    vol = (w * vv[:, None]).sum(axis=0)
    k = int(vol.argmax())
    return float((edges[k] + edges[k + 1]) / 2)


def session_vwap(bars):
    """VWAP of the latest session from 5-minute bars."""
    if bars is None or len(bars) == 0:
        return None
    last_day = bars.index[-1].date()
    b = bars[[d == last_day for d in bars.index.date]]
    v = b["Volume"].astype(float).fillna(0)
    if v.sum() <= 0:
        return None
    tp = (b["High"].astype(float) + b["Low"].astype(float) + b["Close"].astype(float)) / 3
    return float((tp * v).sum() / v.sum())


def _cross(a, b, look=3):
    """+1 if a crossed above b within the last `look` bars, -1 if below, else 0."""
    s = np.sign((a - b).values)
    for i in range(1, min(look, len(s) - 1) + 1):
        if np.isfinite(s[-i]) and np.isfinite(s[-i - 1]) and s[-i] != s[-i - 1] and s[-i] != 0:
            return int(s[-i])
    return 0


def snapshot(df, svwap=None):
    """Latest indicator values for the dashboard, in SNAP_COLS order (None when there is not enough history)."""
    if len(df) < 30:
        return None
    f = compute(df)
    x = f.iloc[-1]
    price = float(x["c"])

    def g(k, d=2):
        v = x.get(k)
        if v is None or not np.isfinite(v):
            return None
        return round(float(v), d if abs(v) >= 1 else max(d, 4))

    st_dir = int(x["stDir"]) if np.isfinite(x["stDir"]) else None
    dirs = f["stDir"].values
    age = None
    if st_dir is not None:
        k = len(dirs) - 1
        while k > 0 and dirs[k - 1] == dirs[-1]:
            k -= 1
        age = int(len(dirs) - 1 - k)
    sk_x = 0
    kk, dd = f["skK"], f["skD"]
    xs = _cross(kk, dd)
    if xs == 1 and np.nanmin(kk.tail(4).values) < 20:
        sk_x = 1
    elif xs == -1 and np.nanmax(kk.tail(4).values) > 80:
        sk_x = -1
    top = np.nanmax([x["spanA"], x["spanB"]]) if np.isfinite(x["spanA"]) or np.isfinite(x["spanB"]) else np.nan
    bot = np.nanmin([x["spanA"], x["spanB"]]) if np.isfinite(x["spanA"]) or np.isfinite(x["spanB"]) else np.nan
    ich = None if not np.isfinite(top) else (1 if price > top else -1 if price < bot else 0)
    try:
        pc = poc(df)
    except Exception:
        pc = None
    vals = {
        "e20": g("e20"), "e50": g("e50"), "e200": g("e200") if len(df) >= 200 else None, "rsi": g("rsi", 1),
        "macdH": round(float(x["macdH"]) / price * 100, 3) if np.isfinite(x["macdH"]) and price else None,
        "macdX": _cross(f["macd"], f["macdS"]), "macdUp": int(x["macd"] > 0) if np.isfinite(x["macd"]) else None,
        "vwap": round(svwap, 4 if svwap < 1 else 2) if svwap else None, "vwap20": g("vwap20"),
        "poc": round(pc, 4 if pc < 1 else 2) if pc else None, "bbU": g("bbU"), "bbL": g("bbL"), "bbPb": g("bbPb", 2),
        "bbSq": int(x["bbWr"] <= 0.1) if np.isfinite(x["bbWr"]) else None,
        "st": g("st"), "stDir": st_dir, "stAge": age, "skK": g("skK", 1), "skD": g("skD", 1), "skX": sk_x,
        "ichTop": round(float(top), 2) if np.isfinite(top) else None, "ichBot": round(float(bot), 2) if np.isfinite(bot) else None,
        "ichPos": ich, "ichTk": int(x["tenkan"] > x["kijun"]) if np.isfinite(x["tenkan"]) and np.isfinite(x["kijun"]) else None,
        "adx": g("adx", 1), "pdi": g("pdi", 1), "mdi": g("mdi", 1),
        "obvUp": int(x["obv"] > x["obvE"]) if np.isfinite(x["obvE"]) else None, "fib": g("fib", 1),
        "avH": g("avH"), "avL": g("avL"), "pvP": g("pvP"), "pvR1": g("pvR1"), "pvR2": g("pvR2"), "pvS1": g("pvS1"),
        "pvS2": g("pvS2"), "atrD": g("atr")}
    return [vals[k] for k in SNAP_COLS]
