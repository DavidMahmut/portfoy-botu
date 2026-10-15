"""Daily-bar backtest of the bot's rules and proposed fixes (research only, never trades).

Runs on GitHub Actions (.github/workflows/backtest.yml) because it needs Yahoo Finance data. Writes
backtest_results.json. Simplifications: decisions at the close, orders at the next open, stops checked
against each day's low/high, current universe only (survivorship bias: today's winners are in the list).
"""
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import universe as U

ROOT = Path(__file__).resolve().parent
START, WARMUP = "2018-01-02", "2016-09-01"
CAPITAL = 3000.0
SLIP, COMM_PS, COMM_MIN, COMM_PCT = 0.0005, 0.0035, 0.35, 0.01
MARGIN_RATE, BORROW = 0.054, 0.005
REGIMES = {"bull": (0.55, 0.20, 0.0, 0.25), "neutral": (0.60, 0.0, 0.0, 0.40), "bear": (0.30, 0.0, 0.25, 0.45)}
GROUP = {}
for g, ts in {
    "semis": "NVDA AMD MU INTC QCOM TSM ASML ARM AVGO MRVL LRCX AMAT KLAC SMH",
    "software": "MSFT ORCL CRM ADBE PLTR NOW CRWD PANW SNOW SHOP DDOG NET APP",
    "ai_hw": "ANET CIEN SMCI VRT DELL",
    "internet": "AAPL AMZN GOOGL META NFLX UBER TSLA",
    "power": "VST CEG NRG GEV XLU URA",
    "fin": "JPM GS MS V MA COIN HOOD SOFI BX XLF KRE IBIT",
    "health": "LLY NVO UNH ISRG VRTX XLV",
    "energy": "XOM CVX OXY COP SLB EOG XLE XOP",
    "industrial": "CAT GE RTX LMT NOC BA DE XLI ITA COPX",
    "consumer": "COST WMT HD MCD NKE SBUX XLY XLP",
    "broad": "QQQ XLK IWM ARKK GLD SLV GDX",
}.items():
    for t in ts.split():
        GROUP[t] = g


def load():
    import yfinance as yf
    tickers = sorted(set(U.UNIVERSE) | set(U.LEVERAGED) | {"SPY", "QQQ", "SMH", "^VIX"})
    df = yf.download(tickers, start=WARMUP, interval="1d", auto_adjust=True, group_by="ticker", progress=False, threads=True)
    out = {}
    for t in tickers:
        try:
            s = df[t].dropna(subset=["Close"])
        except KeyError:
            continue
        if len(s) > 250:
            out[t] = s[["Open", "High", "Low", "Close"]].astype(float)
    return out


def rsi(c, n):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def indicators(data):
    ind = {}
    for t, s in data.items():
        c, h, l = s["Close"], s["High"], s["Low"]
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        r21, r63, r126 = c / c.shift(21) - 1, c / c.shift(63) - 1, c / c.shift(126) - 1
        ind[t] = pd.DataFrame({
            "O": s["Open"], "H": h, "L": l, "C": c, "sma5": c.rolling(5).mean(), "sma50": c.rolling(50).mean(),
            "sma200": c.rolling(200).mean(), "atr": tr.rolling(20).mean(), "rsi2": rsi(c, 2), "rsi3": rsi(c, 3),
            "r5": c / c.shift(5) - 1, "r21": r21, "r63": r63, "r126": r126, "mom121": c.shift(21) / c.shift(252) - 1,
            "score": 100 * (0.2 * r21 + 0.5 * r63 + 0.3 * r126)})
    return ind


def commission(shares, value):
    return min(max(COMM_MIN, COMM_PS * shares), COMM_PCT * value) if value > 0 else 0.0


class Sim:
    def __init__(self, ind, dates, cfg):
        self.ind, self.dates, self.cfg = ind, dates, cfg
        self.cash, self.pos, self.orders = CAPITAL, {}, []
        self.curve, self.trades, self.cool, self.costs = [], [], {}, 0.0

    # ---- helpers
    def px(self, t, d, f="C"):
        try:
            v = self.ind[t].at[d, f]
            return None if pd.isna(v) else float(v)
        except KeyError:
            return None

    def equity(self, d):
        eq = self.cash
        for t, p in self.pos.items():
            c = self.px(t, d) or p["last"]
            p["last"] = c
            eq += p["sh"] * c * (1 if p["side"] == "long" else -1)
        return eq

    def fill(self, t, side, sh, price, d, why, opening):
        value = sh * price
        c = commission(sh, value)
        self.costs += c + value * SLIP
        if (side == "long") == opening:   # buy
            self.cash -= value * (1 + SLIP) + c
        else:                              # sell / short sale
            self.cash += value * (1 - SLIP) - c
        return c

    def open_pos(self, t, side, mv, d, price, i):
        if mv < 10 or price is None or price <= 0:
            return
        sh = mv / price
        self.fill(t, side, sh, price, d, "open", True)
        p = self.pos.get(t)
        atr = self.px(t, self.dates[i - 1], "atr") or price * 0.03
        lev = abs(U.LEVERAGED[t][1]) if t in U.LEVERAGED else 1
        if p:
            tot = p["sh"] + sh
            p["entry"] = (p["entry"] * p["sh"] + price * sh) / tot
            p["sh"] = tot
            return
        if self.cfg.get("no_stops"):
            stop = price * (1 - self.cfg["disaster"]) if self.cfg.get("disaster") and side == "long" else \
                (0.0 if side == "long" else float("inf"))
        elif self.cfg.get("atr_stops"):
            k = self.cfg.get("atr_k", 2.5)
            stop = price - k * atr if side == "long" else price + k * atr
        else:
            stop = price * (0.85 if lev > 1 else 0.92) if side == "long" else price * 1.08
        self.pos[t] = {"side": side, "sh": sh, "entry": price, "stop": stop, "ext": price, "lev": lev, "last": price,
                       "day": i, "tp": False, "atr": atr}

    def close_pos(self, t, frac, price, d, i, why):
        p = self.pos[t]
        sh = p["sh"] * frac
        self.fill(t, p["side"], sh, price, d, why, False)
        pl = sh * (price - p["entry"]) * (1 if p["side"] == "long" else -1)
        self.trades.append({"t": t, "side": p["side"], "pl": pl, "ret": pl / (sh * p["entry"]), "days": i - p["day"],
                            "why": why, "d": str(d.date())})
        if why == "stop":
            self.cool[(t, p["side"])] = i
        if frac >= 0.999:
            del self.pos[t]
        else:
            p["sh"] -= sh

    # ---- one day
    def step(self, i):
        d = self.dates[i]
        # 1) orders decided at yesterday's close, filled at today's open
        for o in self.orders:
            o_px = self.px(o["t"], d, "O")
            if o_px is None:
                continue
            if o["kind"] == "close" and o["t"] in self.pos:
                self.close_pos(o["t"], o.get("frac", 1.0), o_px, d, i, o["why"])
            elif o["kind"] == "open":
                self.open_pos(o["t"], o["side"], o["mv"], d, o_px, i)
        self.orders = []
        # 2) stop orders during the day
        for t in ([] if self.cfg.get("no_stops") and not self.cfg.get("disaster") else list(self.pos)):
            p = self.pos[t]
            o, h, l = self.px(t, d, "O"), self.px(t, d, "H"), self.px(t, d, "L")
            if o is None:
                continue
            if p["side"] == "long" and l <= p["stop"]:
                self.close_pos(t, 1.0, min(o, p["stop"]), d, i, "stop")
            elif p["side"] == "short" and h >= p["stop"]:
                self.close_pos(t, 1.0, max(o, p["stop"]), d, i, "stop")
        # 3) trailing stops and take-profit at the close
        for t in ([] if self.cfg.get("no_stops") else list(self.pos)):
            p = self.pos[t]
            c, h, l, atr = self.px(t, d), self.px(t, d, "H"), self.px(t, d, "L"), self.px(t, d, "atr")
            if c is None:
                continue
            if p["side"] == "long":
                p["ext"] = max(p["ext"], h)
                trail = (p["ext"] - self.cfg.get("atr_trail", 3.0) * (atr or p["atr"])) if self.cfg.get("atr_stops") \
                    else p["ext"] * (0.80 if p["lev"] > 1 else 0.88)
                p["stop"] = max(p["stop"], trail)
                gain = c / p["entry"] - 1
            else:
                p["ext"] = min(p["ext"], l)
                trail = (p["ext"] + self.cfg.get("atr_trail", 3.0) * (atr or p["atr"])) if self.cfg.get("atr_stops") \
                    else p["ext"] * 1.12
                p["stop"] = min(p["stop"], trail)
                gain = 1 - c / p["entry"]
            if self.cfg.get("take_profit", True) and not p["tp"] and gain >= 0.20 and not self.cfg.get("meanrev"):
                p["tp"] = True
                self.close_pos(t, 0.5, c, d, i, "tp")
        # 4) financing
        if self.cash < 0:
            self.cash -= -self.cash * MARGIN_RATE / 360
        shorts = sum(p["sh"] * (self.px(t, d) or p["last"]) for t, p in self.pos.items() if p["side"] == "short")
        self.cash -= shorts * BORROW / 360
        eq = self.equity(d)
        self.curve.append((d, eq))
        if eq <= 0:
            return False
        # 5) decisions for tomorrow
        if self.cfg.get("meanrev"):
            self.decide_meanrev(i, d, eq)
        elif self.cfg.get("index"):
            self.decide_index(i, d, eq)
        elif self.cfg.get("basket"):
            if i + 1 >= len(self.dates) or self.dates[i + 1].month != d.month or i == 1:
                self.decide_basket(i, d, eq)
        else:
            self.decide_momentum(i, d, eq)
        return True

    def cooled(self, t, side, i):
        k = self.cool.get((t, side))
        return k is not None and self.cfg.get("cooldown", 0) and i - k <= self.cfg["cooldown"]

    def decide_meanrev(self, i, d, eq):
        cfg = self.cfg
        for t, p in self.pos.items():
            c, s5 = self.px(t, d), self.px(t, d, "sma5")
            if c is None:
                continue
            if (s5 and c > s5) or i - p["day"] >= cfg.get("max_days", 10):
                self.orders.append({"kind": "close", "t": t, "why": "exit"})
        slots = cfg.get("slots", 5) - len(self.pos) + len(self.orders)
        if slots <= 0:
            return
        cands = []
        for t in U.UNIVERSE:
            r = self.ind.get(t)
            if r is None or d not in r.index or t in self.pos:
                continue
            x = r.loc[d]
            if any(pd.isna(x[k]) for k in ("sma200", "rsi2", "r126")):
                continue
            if x["C"] >= 10 and x["C"] > x["sma200"] and x["rsi2"] < cfg.get("rsi_max", 10) and x["r126"] > 0 \
                    and not self.cooled(t, "long", i):
                cands.append((x["rsi2"], t))
        cands.sort()
        for _, t in cands[:slots]:
            self.orders.append({"kind": "open", "t": t, "side": "long", "mv": eq * cfg.get("weight", 0.2)})

    def trend_up(self, d):
        spy = self.ind["SPY"].loc[d]
        return spy["C"] > spy["sma200"]

    def decide_index(self, i, d, eq):
        """Hold an index ETF while SPY is above its 200-day average, cash otherwise."""
        t, lev = self.cfg.get("etf", "SPY"), self.cfg.get("lev", 1.0)
        up = self.trend_up(d) if self.cfg.get("trend_filter", True) else True
        have = self.pos[t]["sh"] * self.px(t, d) if t in self.pos else 0.0
        want = eq * lev if up else 0.0
        if have and not up:
            self.orders.append({"kind": "close", "t": t, "why": "trend"})
        elif up and have < 0.9 * want:
            self.orders.append({"kind": "open", "t": t, "side": "long", "mv": want - have})

    def decide_basket(self, i, d, eq):
        """Monthly: equal-weight the top-N momentum names (12-1 month), only while the market trend is up."""
        cfg = self.cfg
        n, lev = cfg.get("n", 10), cfg.get("lev", 1.0)
        up = self.trend_up(d) if cfg.get("trend_filter", True) else True
        expo = lev if up else cfg.get("down_expo", 0.0)
        key = cfg.get("rank_by", "mom121")
        rows = []
        for t in U.UNIVERSE:
            if t in U.ETFS and not cfg.get("etfs", False):
                continue
            r = self.ind.get(t)
            if r is None or d not in r.index:
                continue
            x = r.loc[d]
            if pd.isna(x[key]) or pd.isna(x["sma200"]) or x["C"] < 5:
                continue
            if x[key] > 0 and x["C"] > x["sma200"]:
                rows.append((x[key], t))
        rows.sort(reverse=True)
        rank = {t: j + 1 for j, (_, t) in enumerate(rows)}
        keep = [t for t in self.pos if rank.get(t, 999) <= cfg.get("keep_rank", 2 * n)] if expo > 0 else []
        for t in list(self.pos):
            if t not in keep:
                self.orders.append({"kind": "close", "t": t, "why": "rebalance"})
        cap = cfg.get("group_cap")
        groups = {}
        for t in keep:
            groups[GROUP.get(t, "other")] = groups.get(GROUP.get(t, "other"), 0) + 1
        chosen = list(keep)
        for _, t in rows:
            if len(chosen) >= n:
                break
            g = GROUP.get(t, "other")
            if t in chosen or (cap and groups.get(g, 0) >= cap) or self.cooled(t, "long", i):
                continue
            chosen.append(t)
            groups[g] = groups.get(g, 0) + 1
        if not chosen or expo <= 0:
            return
        each = eq * expo / len(chosen)
        for t in chosen:
            have = self.pos[t]["sh"] * self.px(t, d) if t in self.pos else 0.0
            if have > 1.25 * each:
                self.orders.append({"kind": "close", "t": t, "frac": 1 - each / have, "why": "trim"})
            elif have < 0.8 * each:
                self.orders.append({"kind": "open", "t": t, "side": "long", "mv": each - have})

    def decide_momentum(self, i, d, eq):
        cfg = self.cfg
        spy = self.ind["SPY"].loc[d]
        vix = self.px("^VIX", d) or 20
        name = "bear" if spy["C"] < spy["sma200"] else ("bull" if spy["C"] > spy["sma50"] and vix < 25 else "neutral")
        ls, be, bre, sh = REGIMES[name]
        if cfg.get("no_lev_etf"):          # give the leveraged-ETF slice to the stock sleeves instead
            ls, sh, be, bre = ls + be, sh + bre, 0.0, 0.0
        budget = 0.95 * eq * (0.6 if vix >= 35 else 1.0)
        n_long, n_short = 4, 3
        long_t = budget * ls / 0.5 / n_long
        short_t = budget * sh / 0.5 / n_short if cfg.get("shorts", True) else 0.0
        rows, srows = [], []
        for t in U.UNIVERSE:
            r = self.ind.get(t)
            if r is None or d not in r.index:
                continue
            x = r.loc[d]
            if any(pd.isna(x[k]) for k in ("sma50", "r126", "atr")):
                continue
            elig = x["C"] >= 5 and x["C"] > x["sma50"] and x["r63"] > 0
            rows.append((x["score"], t, elig, x))
            if t not in U.ETFS and x["C"] >= 10 and x["C"] < x["sma50"] and x["r21"] < 0 and x["r63"] < 0:
                srows.append((x["score"], t, x))
        rows.sort(key=lambda z: -z[0])
        rank, k = {}, 0
        for s, t, e, x in rows:
            if e:
                k += 1
                rank[t] = k
        srows.sort(key=lambda z: z[0])
        srank = {t: j + 1 for j, (s, t, x) in enumerate(srows)}
        idx = []
        for u in ("QQQ", "SPY", "SMH"):
            x = self.ind[u].loc[d] if d in self.ind[u].index else None
            if x is not None and not pd.isna(x["score"]):
                idx.append((x["score"], u))
        bull_etf = U.BULL_ETF[max(idx)[1]] if be and idx else None
        bear_etf = U.BEAR_ETF[min(idx)[1]] if bre and idx else None
        # exits
        keep = {}
        for t, p in list(self.pos.items()):
            if t in U.LEVERAGED:
                ok = t in (bull_etf, bear_etf)
            elif p["side"] == "long":
                ok = rank.get(t, 99) <= 10
            else:
                ok = srank.get(t, 99) <= 10
            if ok:
                keep[t] = p
            else:
                self.orders.append({"kind": "close", "t": t, "why": "rank"})
        # vol-scaled targets
        def tgt(t, base):
            if not cfg.get("vol_size"):
                return base
            x = self.ind[t].loc[d]
            v = x["atr"] / x["C"]
            return base * min(1.5, max(0.5, cfg.get("vol_ref", 0.025) / v)) if v > 0 else base
        groups = {}
        for t, p in keep.items():
            if p["side"] == "long" and t not in U.LEVERAGED:
                groups[GROUP.get(t, "other")] = groups.get(GROUP.get(t, "other"), 0) + 1
        n_l = sum(1 for t, p in keep.items() if p["side"] == "long" and t not in U.LEVERAGED)
        n_s = sum(1 for t, p in keep.items() if p["side"] == "short")
        targets = {}
        for t, p in keep.items():
            targets[t] = (p["side"], tgt(t, bull_t_or(t, bull_etf, bear_etf, budget, be, bre, long_t, short_t, p["side"])))
        for s, t, e, x in rows:
            if n_l >= n_long:
                break
            if not e or t in self.pos or self.cooled(t, "long", i):
                continue
            g = GROUP.get(t, "other")
            if cfg.get("group_cap") and groups.get(g, 0) >= cfg["group_cap"]:
                continue
            if cfg.get("pullback") and not (x["rsi3"] < cfg.get("pb_rsi", 30)):
                continue
            targets[t] = ("long", tgt(t, long_t))
            groups[g] = groups.get(g, 0) + 1
            n_l += 1
        if short_t:
            for s, t, x in srows:
                if n_s >= n_short:
                    break
                if t in self.pos or self.cooled(t, "short", i):
                    continue
                targets[t] = ("short", tgt(t, short_t))
                n_s += 1
        for etf in (bull_etf, bear_etf):
            if etf and etf not in self.pos and not self.cooled(etf, "long", i):
                targets[etf] = ("long", budget * (be if etf == bull_etf else bre) / 0.75)
        for t, (side, mv) in targets.items():
            have = self.pos[t]["sh"] * (self.px(t, d) or 0) if t in self.pos else 0.0
            if t in self.pos and have > 1.5 * mv:
                self.orders.append({"kind": "close", "t": t, "frac": 1 - mv / have, "why": "trim"})
            elif have < 0.6 * mv:
                self.orders.append({"kind": "open", "t": t, "side": side, "mv": mv - have})


def bull_t_or(t, bull_etf, bear_etf, budget, be, bre, long_t, short_t, side):
    if t == bull_etf:
        return budget * be / 0.75
    if t == bear_etf:
        return budget * bre / 0.75
    return long_t if side == "long" else short_t


def metrics(curve, trades, costs, spy=None):
    s = pd.Series({d: v for d, v in curve})
    r = s.pct_change().dropna()
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    cagr = (s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1 if s.iloc[-1] > 0 else -1
    dd = (s / s.cummax() - 1).min()
    vol = r.std() * math.sqrt(252)
    out = {"cagrPct": round(cagr * 100, 2), "volPct": round(vol * 100, 2),
           "sharpe": round(r.mean() / r.std() * math.sqrt(252), 2) if r.std() > 0 else None,
           "maxDrawdownPct": round(dd * 100, 2), "endValue": round(float(s.iloc[-1]), 2),
           "worstDayPct": round(r.min() * 100, 2), "worstMonthPct": round(((1 + r).resample("ME").prod() - 1).min() * 100, 2),
           "costs": round(costs, 2)}
    if trades:
        w = [t for t in trades if t["pl"] > 0]
        lo = [t for t in trades if t["pl"] <= 0]
        out.update(trades=len(trades), winRatePct=round(len(w) / len(trades) * 100, 1),
                   avgWinPct=round(np.mean([t["ret"] for t in w]) * 100, 2) if w else None,
                   avgLossPct=round(np.mean([t["ret"] for t in lo]) * 100, 2) if lo else None,
                   profitFactor=round(sum(t["pl"] for t in w) / -sum(t["pl"] for t in lo), 2) if lo and sum(t["pl"] for t in lo) < 0 else None,
                   stopsPct=round(sum(1 for t in trades if t["why"] == "stop") / len(trades) * 100, 1),
                   avgDays=round(np.mean([t["days"] for t in trades]), 1))
    yearly = (1 + r).groupby(r.index.year).prod() - 1
    out["yearly"] = {str(k): round(v * 100, 1) for k, v in yearly.items()}
    periods = {"2018 Q4": ("2018-10-01", "2018-12-31"), "2020 Covid": ("2020-02-19", "2020-03-23"),
               "2022": ("2022-01-03", "2022-12-30"), "2025 Şubat-Nisan": ("2025-02-19", "2025-04-08"),
               "Son 3 ay": ("2026-07-15", "2026-10-31"), "Ekim 2026": ("2026-10-01", "2026-10-31")}
    out["periods"] = {}
    for k, (a, b) in periods.items():
        seg = s[(s.index >= a) & (s.index <= b)]
        if len(seg) > 2:
            out["periods"][k] = round((seg.iloc[-1] / seg.iloc[0] - 1) * 100, 1)
    return out, s


VARIANTS2 = [
    ("idx_spy", "SPY, yalnızca 200 günlük ortalamanın üstündeyken", {"index": True, "etf": "SPY", "no_stops": True}),
    ("idx_qqq", "QQQ, SPY 200 günlük ortalamanın üstündeyken", {"index": True, "etf": "QQQ", "no_stops": True}),
    ("idx_qqq15", "QQQ 1,5x kredili, aynı trend filtresiyle", {"index": True, "etf": "QQQ", "lev": 1.5, "no_stops": True}),
    ("bk10", "Aylık momentum sepeti: en güçlü 10 hisse, trend filtresi", {"basket": True, "n": 10, "no_stops": True}),
    ("bk10_nf", "Aylık momentum sepeti: en güçlü 10 hisse, filtresiz", {"basket": True, "n": 10, "no_stops": True,
                                                                        "trend_filter": False}),
    ("bk10_l13", "Aylık momentum sepeti, 1,3x kredili, trend filtresi", {"basket": True, "n": 10, "lev": 1.3, "no_stops": True}),
    ("bk15", "Aylık momentum sepeti: en güçlü 15 hisse, trend filtresi", {"basket": True, "n": 15, "no_stops": True}),
]

VARIANTS3 = [
    ("bk10_g3", "Aylık sepet, sektör başına en fazla 3 hisse", {"basket": True, "n": 10, "no_stops": True, "group_cap": 3}),
    ("bk10_g3_ds", "Aylık sepet, sektör sınırı + %25 felaket zarar-kesi", {"basket": True, "n": 10, "no_stops": True,
                                                                           "group_cap": 3, "disaster": 0.25, "cooldown": 21}),
]

VARIANTS = [
    ("v0", "Mevcut kurallar (v3)", {}),
    ("v1", "+ zarar-kes sonrası 10 gün bekleme", {"cooldown": 10}),
    ("v2", "+ oynaklığa göre zarar-kes (ATR) ve pozisyon boyutu", {"cooldown": 10, "atr_stops": True, "vol_size": True}),
    ("v3", "+ sektör başına en fazla 2 long", {"cooldown": 10, "atr_stops": True, "vol_size": True, "group_cap": 2}),
    ("v4", "+ 3x ETF yok", {"cooldown": 10, "atr_stops": True, "vol_size": True, "group_cap": 2, "no_lev_etf": True}),
    ("v5", "+ yalnızca geri çekilmede alım", {"cooldown": 10, "atr_stops": True, "vol_size": True, "group_cap": 2,
                                             "no_lev_etf": True, "pullback": True}),
    ("mr", "Düşüşte alım (yükseliş trendinde aşırı satım)", {"meanrev": True, "cooldown": 5, "atr_stops": True,
                                                          "atr_k": 3.0, "atr_trail": 99.0, "slots": 5, "weight": 0.2}),
]


def main(data=None):
    data = data or load()
    ind = indicators(data)
    dates = [d for d in ind["SPY"].index if d >= pd.Timestamp(START)]
    res, curves = {}, {}
    for key, label, cfg in VARIANTS + VARIANTS2 + VARIANTS3:
        sim = Sim(ind, dates, cfg)
        for i in range(1, len(dates)):
            if not sim.step(i):
                break
        m, s = metrics(sim.curve, sim.trades, sim.costs)
        m["label"] = label
        res[key] = m
        curves[key] = s
        print(key, label, m["cagrPct"], m["maxDrawdownPct"], m.get("sharpe"))
    spy = ind["SPY"]["C"].loc[dates[1]:]
    spy_curve = list(zip(spy.index, (spy / spy.iloc[0] * CAPITAL).values))
    m, s = metrics(spy_curve, [], 0.0)
    m["label"] = "SPY al-tut"
    res["spy"] = m
    curves["spy"] = s
    blends = [("mix_bk_mr", "Karma: %70 aylık sepet + %30 düşüşte alım", {"bk10": 0.7, "mr": 0.3}),
              ("mix_core", "Karma: %50 QQQ trend + %50 aylık sepet", {"idx_qqq": 0.5, "bk10": 0.5}),
              ("mix_core3", "Karma: %40 QQQ trend + %40 aylık sepet + %20 düşüşte alım",
               {"idx_qqq": 0.4, "bk10": 0.4, "mr": 0.2}),
              ("mix_v4", "ÖNERİ: %50 QQQ trend + %50 sektör sınırlı sepet", {"idx_qqq": 0.5, "bk10_g3": 0.5}),
              ("mix_v4ds", "ÖNERİ + felaket zarar-kesi", {"idx_qqq": 0.5, "bk10_g3_ds": 0.5}),
              ("mix_v4spy", "%50 SPY trend + %50 sektör sınırlı sepet", {"idx_spy": 0.5, "bk10_g3": 0.5})]
    for key, label, wts in blends:
        r = sum(w * curves[k].pct_change() for k, w in wts.items())
        c = (1 + r.fillna(0)).cumprod() * CAPITAL
        m, _ = metrics(list(zip(c.index, c.values)), [], 0.0)
        m["label"] = label
        res[key] = m
    for w in (0.3, 0.5):
        best = "v4"
        r = (1 - w) * curves[best].pct_change() + w * curves["mr"].pct_change()
        c = (1 + r.fillna(0)).cumprod() * CAPITAL
        m, _ = metrics(list(zip(c.index, c.values)), [], 0.0)
        m["label"] = f"Karma: %{int((1 - w) * 100)} {best} + %{int(w * 100)} düşüşte alım"
        res[f"mix{int(w * 100)}"] = m
    # today's oversold names in long-term uptrends
    last = dates[-1]
    oversold = []
    for t in U.UNIVERSE:
        r = ind.get(t)
        if r is None or last not in r.index:
            continue
        x = r.loc[last]
        if not pd.isna(x["sma200"]) and x["C"] > x["sma200"] and x["r126"] > 0 and x["rsi2"] < 15:
            oversold.append({"ticker": t, "close": round(x["C"], 2), "rsi2": round(x["rsi2"], 1), "r5Pct": round(x["r5"] * 100, 1),
                             "r126Pct": round(x["r126"] * 100, 1), "aboveSma200Pct": round((x["C"] / x["sma200"] - 1) * 100, 1)})
    oversold.sort(key=lambda z: z["rsi2"])
    corr = None
    try:
        rr = pd.concat({k: curves[k].pct_change() for k in ("v4", "mr")}, axis=1).dropna()
        corr = round(float(rr.corr().iloc[0, 1]), 2)
    except Exception:
        pass
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "start": START, "end": str(last.date()),
           "capital": CAPITAL, "universe": len(U.UNIVERSE), "results": res, "oversoldNow": oversold, "corrV4MR": corr,
           "notes": "Günlük mumlar, kapanışta karar, ertesi açılışta işlem; zarar-kes gün içi en düşük/en yüksekle kontrol "
                    "edilir. Evren bugünkü hisse listesidir (geçmişte batan şirketler yok), bu yüzden sonuçlar iyimserdir."}
    (ROOT / "backtest_results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
