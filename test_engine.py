"""Scenario tests for the long/short engine with synthetic market data (no network).

Run: python test_engine.py
"""
import copy
import json
import random
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import trader as T  # noqa: E402
import universe as U  # noqa: E402

NY = ZoneInfo("America/New_York")
BASE = json.loads((ROOT / "ledger.json").read_text())
DEPOSITS = json.loads((ROOT / "deposits.json").read_text())
DAY = "2026-10-12"


def ledger():
    return copy.deepcopy(BASE)


class FakeData:
    def __init__(self, day, paths, bars):
        self.day, self.paths, self.bars = day, paths, bars

    def _daily(self, closes, today_close=None):
        days = pd.bdate_range(end=pd.Timestamp(self.day) - pd.Timedelta(days=1), periods=len(closes), tz=NY)
        df = pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=days)
        if today_close is not None:
            df.loc[pd.Timestamp(self.day, tz=NY)] = [today_close] * 4
        return df

    def daily(self, tickers, period="1y"):
        out = {}
        for t in tickers:
            if t in self.paths:
                last = self.bars.get(t, [None])[-1]
                out[t] = self._daily(self.paths[t], last[4] if last else None)
        return out

    def intraday(self, tickers):
        out = {}
        for t in tickers:
            if t in self.bars:
                idx = [pd.Timestamp(f"{self.day} {b[0]}", tz=NY) for b in self.bars[t]]
                out[t] = pd.DataFrame([b[1:] for b in self.bars[t]], index=idx, columns=["Open", "High", "Low", "Close"])
        return out


def flat_bars(price, start="09:30", n=12, step=0.0):
    h, m = map(int, start.split(":"))
    out, p = [], price
    for i in range(n):
        mm = h * 60 + m + 5 * i
        o = p
        p = p * (1 + step)
        out.append((f"{mm // 60:02d}:{mm % 60:02d}", o, max(o, p) * 1.001, min(o, p) * 0.999, p))
    return out


def trend(end, k, n=260):
    raw = [(1 + k) ** i for i in range(n)]
    return [round(end * r / raw[-1], 2) for r in raw]


def base_paths(bear=False):
    rnd = random.Random(11)
    paths = {}
    for t in set(U.UNIVERSE) | set(U.LEVERAGED) | {"SPY", "DIA", "^VIX", "CL=F", "BZ=F", "^TNX", "GC=F"}:
        drift, p, s = rnd.uniform(-0.0005, 0.0015), rnd.uniform(20, 400), []
        for _ in range(260):
            p *= 1 + drift + rnd.uniform(-0.008, 0.008)
            s.append(round(p, 2))
        paths[t] = s
    for t, k, end in (("AVGO", 0.006, 380), ("VST", 0.005, 160), ("GE", 0.0045, 300), ("PLTR", 0.004, 205),
                      ("DELL", 0.0045, 570), ("CRWD", 0.004, 270), ("SNOW", 0.004, 360)):
        paths[t] = trend(end, k)
    for t, k, end in (("NKE", -0.003, 60), ("INTC", -0.0025, 40), ("BA", -0.002, 150), ("UNH", -0.0018, 300)):
        paths[t] = trend(end, k)
    paths["^VIX"] = [15.0] * 260
    if bear:
        paths["SPY"] = trend(600, -0.0012)
        paths["QQQ"] = trend(500, -0.0015)
        paths["SMH"] = trend(250, -0.001)
    else:
        paths["SPY"] = trend(776, 0.0007)
        paths["QQQ"] = trend(750, 0.0012)
        paths["SMH"] = trend(300, 0.0009)
    for t, (u, f) in U.LEVERAGED.items():
        paths[t] = trend(80 if f > 0 else 20, 0.002 * (1 if f > 0 else -1))
    return paths


def bars_for(paths, overrides, n=12):
    b = {t: flat_bars(paths[t][-1], n=n) for t in paths}
    b.update(overrides)
    return b


def check_books(L):
    P = L["portfolio"]
    assert P["cash"] + P["longValue"] - P["shortValue"] - P["totalValue"] < 0.02
    for p in P["positions"]:
        assert p["shares"] > 0, p
    cash = L["config"]["startCapital"]
    for t in L["trades"]:
        a = t["action"]
        if a == "BUY" or a == "COVER":
            cash -= t["value"]
        elif a in ("SELL", "SHORT"):
            cash += t["value"] - t.get("fees", 0)
        elif a == "DEPOSIT":
            cash += t["value"]
    c = P.get("costs", {})
    cash -= c.get("interest", 0) + c.get("borrow", 0)
    assert abs(cash - P["cash"]) <= 0.05, (round(cash, 2), P["cash"])


def run(L, hhmm, data, day=DAY, deposits=DEPOSITS):
    now = datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=NY)
    eng = T.Engine(L, data, now, deposits)
    eng.run()
    check_books(L)
    return eng


def first_daily():
    paths = base_paths()
    L = ledger()
    eng = run(L, "10:15", FakeData(DAY, paths, bars_for(paths, {})))
    return L, eng, paths


def test_migration_deposit_daily_bull():
    L, eng, _ = first_daily()
    P, M = L["portfolio"], L["meta"]
    assert M["engine"] == T.ENGINE and M["lastDailyDate"] == DAY
    assert P["netDeposits"] == 2000 and "2026-10-12-1700" in M["appliedDeposits"]
    assert any(t["action"] == "DEPOSIT" for t in L["trades"])
    assert M["regime"]["name"] == "bull", M["regime"]
    sides = {(p["ticker"], p["side"]) for p in P["positions"]}
    shorts = [p for p in P["positions"] if p["side"] == "short"]
    bull = [p for p in P["positions"] if p["ticker"] in U.LEVERAGED]
    assert shorts and bull, sides
    assert P["leverage"] <= 3.0 + 1e-6, P["leverage"]
    assert P["totalValue"] > P["maintenanceReq"]
    # second run same day does not redo the daily decision or the deposit
    n_tr, n_runs = len(L["trades"]), len(L["runs"])
    run(L, "10:30", FakeData(DAY, base_paths(), bars_for(base_paths(), {}, n=14)))
    assert len(L["runs"]) == n_runs and sum(t["action"] == "DEPOSIT" for t in L["trades"]) == 1
    print("bull daily OK:", L["runs"][-1]["headline"], "| lev", P["leverage"], "| equity", P["totalValue"],
          "| req", P["maintenanceReq"], "| positions", sorted(sides))


def test_short_stop_and_gap():
    L, eng, paths = first_daily()
    shorts = [p for p in L["portfolio"]["positions"] if p["side"] == "short"][:2]
    assert len(shorts) == 2
    a, b = shorts
    L["meta"]["lastDailyDate"] = DAY
    nxt = "2026-10-13"
    pa, pb = a["lastPrice"], b["lastPrice"]
    ov = {a["ticker"]: flat_bars(pa, n=3) + [("09:45", pa * 1.01, a["stopPrice"] + 0.5, pa, pa * 1.05)],
          b["ticker"]: [("09:30", b["stopPrice"] * 1.03, b["stopPrice"] * 1.04, b["stopPrice"] * 1.02, b["stopPrice"] * 1.03)]}
    paths2 = copy.deepcopy(paths)
    eng = run(L, "09:55", FakeData(nxt, paths2, bars_for(paths2, ov, n=5)), day=nxt)
    cov = {t["ticker"]: t for t in eng.new_trades if t["action"] == "COVER"}
    assert abs(cov[a["ticker"]]["quotePrice"] - a["stopPrice"]) < 1e-6, cov
    assert abs(cov[b["ticker"]]["quotePrice"] - round(b["stopPrice"] * 1.03, 4)) < 0.01, cov
    assert L["portfolio"]["costs"]["interest"] >= 0 and L["portfolio"]["costs"]["borrow"] > 0
    print("short stop + gap OK; accrued interest", L["portfolio"]["costs"]["interest"], "borrow",
          L["portfolio"]["costs"]["borrow"])


def test_short_trailing():
    L, eng, paths = first_daily()
    s = [p for p in L["portfolio"]["positions"] if p["side"] == "short"][0]
    L["meta"]["lastDailyDate"] = DAY
    nxt = "2026-10-13"
    p0 = s["lastPrice"]
    low = p0 * 0.80
    ov = {s["ticker"]: [("09:30", p0, p0, low, low * 1.01), ("09:35", low * 1.01, low * 1.05, low, low * 1.04),
                        ("09:40", low * 1.04, low * 1.13, low * 1.03, low * 1.12)]}
    eng = run(L, "09:50", FakeData(nxt, paths, bars_for(paths, ov, n=5)), day=nxt)
    acts = [(t["action"], t["rule"], t["quotePrice"]) for t in eng.new_trades if t["ticker"] == s["ticker"]]
    assert ("COVER", "kâr alma") in [(x[0], x[1]) for x in acts] or ("COVER", "zarar-kes") in [(x[0], x[1]) for x in acts], acts
    print("short trailing/take-profit OK:", acts)


def test_squeeze_and_crash_brakes():
    L, eng, paths = first_daily()
    L["meta"]["lastDailyDate"] = DAY
    nxt = "2026-10-13"
    spy_up = flat_bars(paths["SPY"][-1] * 1.035, n=5)
    run(L, "09:55", FakeData(nxt, paths, bars_for(paths, {"SPY": spy_up}, n=5)), day=nxt)
    P = L["portfolio"]
    short_exp = sum(p["exposure"] for p in P["positions"] if T.direction(p) < 0)
    assert short_exp <= 0.5 * P["totalValue"] * 1.01, (short_exp, P["totalValue"])
    assert L["meta"]["squeezeDate"] == nxt
    L2, _, paths2 = first_daily()
    L2["meta"]["lastDailyDate"] = DAY
    spy_dn = flat_bars(paths2["SPY"][-1] * 0.955, n=5)
    run(L2, "09:55", FakeData(nxt, paths2, bars_for(paths2, {"SPY": spy_dn}, n=5)), day=nxt)
    P2 = L2["portfolio"]
    long_exp = sum(p["exposure"] for p in P2["positions"] if T.direction(p) > 0)
    assert long_exp <= 1.0 * P2["totalValue"] * 1.01, (long_exp, P2["totalValue"])
    print("squeeze brake OK (short exp", round(short_exp), "/", P["totalValue"], ") | crash brake OK (long exp",
          round(long_exp), "/", P2["totalValue"], ")")


def test_margin_call():
    L, eng, paths = first_daily()
    L["meta"]["lastDailyDate"] = DAY
    nxt = "2026-10-13"
    # regime limit cut to 1.5x while the book is at ~2.85x -> leverage breach -> forced reduction
    L["meta"]["regime"]["maxLev"] = 1.5
    eng = run(L, "09:55", FakeData(nxt, paths, bars_for(paths, {}, n=5)), day=nxt)
    P = L["portfolio"]
    rules = {t["rule"] for t in eng.new_trades}
    assert "teminat çağrısı" in rules, rules
    assert P["leverage"] <= 2.0 + 1e-6, P["leverage"]
    # equity below maintenance: wipe most of the cash
    L2, _, paths2 = first_daily()
    L2["meta"]["lastDailyDate"] = DAY
    L2["portfolio"]["cash"] -= 1400
    L2["config"]["startCapital"] -= 1400   # keep the cash reconciliation consistent
    eng2 = run(L2, "09:55", FakeData(nxt, paths2, bars_for(paths2, {}, n=5)), day=nxt)
    P2 = L2["portfolio"]
    assert "teminat çağrısı" in {t["rule"] for t in eng2.new_trades}
    assert P2["totalValue"] >= P2["maintenanceReq"], (P2["totalValue"], P2["maintenanceReq"])
    print("margin call OK: lev", P["leverage"], "| equity", P2["totalValue"], "req", P2["maintenanceReq"])


def test_bear_regime():
    paths = base_paths(bear=True)
    L = ledger()
    run(L, "10:15", FakeData(DAY, paths, bars_for(paths, {})))
    P, M = L["portfolio"], L["meta"]
    assert M["regime"]["name"] == "bear", M["regime"]
    inv = [p for p in P["positions"] if p["ticker"] in U.LEVERAGED]
    assert inv and all(U.LEVERAGED[p["ticker"]][1] < 0 for p in inv), [p["ticker"] for p in inv]
    short_exp = sum(p["exposure"] for p in P["positions"] if T.direction(p) < 0)
    long_exp = sum(p["exposure"] for p in P["positions"] if T.direction(p) > 0)
    assert P["leverage"] <= 1.5 + 1e-6 and short_exp > long_exp, (P["leverage"], short_exp, long_exp)
    print("bear regime OK: lev", P["leverage"], "short exp", round(short_exp), "long exp", round(long_exp),
          [p["ticker"] + ":" + p["side"] for p in P["positions"]])


def test_fees():
    assert T.sale_fees(100, 0.5) == 0.02
    assert T.sale_fees(1_000_000, 10_000) == round(20.6 + 1.95, 2)
    assert T.sale_fees(10, 100_000) == round(0.01 + 9.79, 2)
    print("fees OK")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL TESTS PASSED")
