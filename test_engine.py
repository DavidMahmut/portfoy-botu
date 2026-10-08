"""Scenario tests for the trading engine with synthetic market data (no network)."""
import copy
import json
import math
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


def ledger():
    return copy.deepcopy(BASE)


class FakeData:
    """daily_paths: ticker -> list of closes (oldest..yesterday); intraday: ticker -> list of (hh:mm, o,h,l,c)."""

    def __init__(self, day, daily_paths, intraday_bars):
        self.day = day
        self.daily_paths = daily_paths
        self.bars = intraday_bars

    def _daily_df(self, closes, today_close=None):
        days = pd.bdate_range(end=pd.Timestamp(self.day) - pd.Timedelta(days=1), periods=len(closes), tz=NY)
        df = pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=days)
        if today_close is not None:
            df.loc[pd.Timestamp(self.day, tz=NY)] = [today_close] * 4
        return df

    def daily(self, tickers, period="1y"):
        out = {}
        for t in tickers:
            if t in self.daily_paths:
                last = self.bars.get(t, [None])[-1]
                out[t] = self._daily_df(self.daily_paths[t], last[4] if last else None)
        return out

    def intraday(self, tickers):
        out = {}
        for t in tickers:
            if t not in self.bars:
                continue
            idx = [pd.Timestamp(f"{self.day} {b[0]}", tz=NY) for b in self.bars[t]]
            out[t] = pd.DataFrame([b[1:] for b in self.bars[t]], index=idx, columns=["Open", "High", "Low", "Close"])
        return out


def flat_bars(price, start="09:30", n=12, step=0.0):
    h, m = map(int, start.split(":"))
    out = []
    p = price
    for i in range(n):
        mm = h * 60 + m + 5 * i
        o = p
        p = p * (1 + step)
        out.append((f"{mm // 60:02d}:{mm % 60:02d}", o, max(o, p) * 1.001, min(o, p) * 0.999, p))
    return out


def base_paths():
    rnd = random.Random(7)
    paths = {}
    for t in set(U.UNIVERSE) | {"SPY", "DIA", "^VIX", "CL=F", "BZ=F", "^TNX", "GC=F"}:
        drift = rnd.uniform(-0.002, 0.003)
        p = rnd.uniform(20, 400)
        series = []
        for _ in range(260):
            p *= 1 + drift + rnd.uniform(-0.01, 0.01)
            series.append(round(p, 2))
        paths[t] = series
    # make some clear leaders
    for t, k, end in (("AVGO", 0.006, 380), ("VST", 0.005, 160), ("GE", 0.0045, 300), ("PLTR", 0.004, 199)):
        raw = [(1 + k) ** i for i in range(260)]
        paths[t] = [round(end * r / raw[-1], 2) for r in raw]
    paths["XOP"] = [round(193 * (1.001 ** (i - 259)), 2) for i in range(260)]
    paths["NVDA"] = [round(230 * (1.0015 ** (i - 259)), 2) for i in range(260)]
    paths["SPY"] = [round(600 * (1.0006 ** i), 2) for i in range(260)]
    return paths


def check_books(L):
    P = L["portfolio"]
    mv = sum(p["marketValue"] for p in P["positions"])
    assert P["cash"] >= 0, P["cash"]
    assert abs(P["cash"] + mv - P["totalValue"]) <= 0.011
    for p in P["positions"]:
        assert p["shares"] > 0
    # cash reconciles with the trade log
    cash = L["config"]["startCapital"]
    for t in L["trades"]:
        cash += -t["value"] if t["action"] == "BUY" else t["value"]
    assert abs(cash - P["cash"]) <= 0.02, (cash, P["cash"])


def run(L, day, hhmm, data):
    now = datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=NY)
    eng = T.Engine(L, data, now)
    eng.run()
    check_books(L)
    return eng


DAY = "2026-10-09"


def bars_for(paths, overrides):
    bars = {t: flat_bars(paths[t][-1], n=12) for t in paths}
    bars.update(overrides)
    return bars


def test_quiet_guard():
    paths = base_paths()
    paths["XOP"] = [190.0] * 260
    paths["NVDA"] = [228.0] * 260
    L = ledger()
    eng = run(L, DAY, "09:45", FakeData(DAY, paths, bars_for(paths, {})))
    assert eng.status in ("normal", "uyari"), eng.status
    assert not eng.new_trades
    assert L["checks"][DAY][-1]["kind"] == "guard"
    print("quiet guard OK:", L["checks"][DAY][-1]["note"])


def test_stop_and_gap():
    paths = base_paths()
    L = ledger()
    # XOP stop 177.77: a bar dips to 176 -> filled at stop
    xop = flat_bars(190, n=6) + [("10:00", 185, 186, 176.0, 179), ("10:05", 179, 180, 178, 179.5)]
    # NVDA stop 211.82: gap open at 205 -> filled at open 205
    nvda = [("09:30", 205.0, 207, 204, 206)] + flat_bars(206, start="09:35", n=6)
    eng = Engine_run_before_daily(L, paths, {"XOP": xop, "NVDA": nvda})
    sold = {t["ticker"]: t for t in eng.new_trades}
    assert "XOP" in sold and abs(sold["XOP"]["quotePrice"] - 177.77) < 1e-6, sold.get("XOP")
    assert "NVDA" in sold and abs(sold["NVDA"]["quotePrice"] - 205.0) < 1e-6, sold.get("NVDA")
    assert eng.status == "alarm"
    print("stop + gap OK:", sold["XOP"]["reason"][:80], "|", sold["NVDA"]["reason"][-70:])


def Engine_run_before_daily(L, paths, overrides):
    L["meta"]["lastDailyDate"] = DAY  # suppress the daily decision for pure guard tests
    return run(L, DAY, "10:08", FakeData(DAY, paths, bars_for(paths, overrides)))


def test_trailing_sequence():
    paths = base_paths()
    L = ledger()
    # PLTR: early dip to 190 (above stop 181.35), then rally to 230 (trail stop -> 202.4), then drop to 200
    pltr = [("09:30", 197, 198, 190, 196), ("09:35", 196, 230, 196, 229), ("09:40", 229, 229, 215, 216),
            ("09:45", 216, 216, 200, 201), ("09:50", 201, 202, 199, 200)]
    eng = Engine_run_before_daily(L, paths, {"PLTR": pltr})
    sold = {t["ticker"]: t for t in eng.new_trades}
    assert "PLTR" in sold, eng.new_trades
    assert abs(sold["PLTR"]["quotePrice"] - round(230 * 0.88, 2)) < 1e-6, sold["PLTR"]["quotePrice"]
    print("trailing OK: sold at", sold["PLTR"]["quotePrice"])


def test_take_profit_once():
    paths = base_paths()
    L = ledger()
    pltr = flat_bars(240, n=6)  # avgCost ~197.12 -> +21.7 %
    eng = Engine_run_before_daily(L, paths, {"PLTR": pltr})
    tp = [t for t in eng.new_trades if t["rule"] == "kâr alma"]
    assert len(tp) == 1 and abs(tp[0]["shares"] - 0.2282) < 1e-9, tp
    p = next(p for p in L["portfolio"]["positions"] if p["ticker"] == "PLTR")
    assert p["tookProfit"] is True
    eng2 = run(L, DAY, "10:23", FakeData(DAY, paths, bars_for(paths, {"PLTR": pltr + flat_bars(242, start="10:00", n=4)})))
    assert not [t for t in eng2.new_trades if t["rule"] == "kâr alma"]
    print("take profit OK: sold", tp[0]["shares"], "shares once")


def test_crash_brake():
    paths = base_paths()
    L = ledger()
    prev = paths["SPY"][-1]
    spy = flat_bars(prev * 0.965, n=6)
    eng = Engine_run_before_daily(L, paths, {"SPY": spy})
    P = L["portfolio"]
    assert P["cash"] >= P["totalValue"] * 0.5 - 0.6, (P["cash"], P["totalValue"])
    assert L["meta"]["brakeDate"] == DAY
    print("crash brake OK: cash", P["cash"], "of", P["totalValue"])


def test_stale_data():
    paths = base_paths()
    L = ledger()
    xop = flat_bars(150, n=3)  # far below stop but last bar 09:40, run at 11:00 -> stale
    spy = flat_bars(paths["SPY"][-1], n=18)
    L["meta"]["lastDailyDate"] = DAY
    eng = run(L, DAY, "11:00", FakeData(DAY, paths, bars_for(paths, {"XOP": xop, "SPY": spy})))
    assert not [t for t in eng.new_trades if t["ticker"] == "XOP"]
    print("stale OK:", L["checks"][DAY][-1].get("note"))


def test_daily_rebalance():
    paths = base_paths()
    paths["XOP"] = [200 - i * 0.2 for i in range(260)]  # falling -> below SMA50, negative 3m
    L = ledger()
    bars = bars_for(paths, {"XOP": flat_bars(paths["XOP"][-1], n=12)})
    eng = run(L, DAY, "10:15", FakeData(DAY, paths, bars))
    tick = [p["ticker"] for p in L["portfolio"]["positions"]]
    run_log = L["runs"][-1]
    assert run_log["date"] == DAY and run_log["ranking"], run_log.keys()
    assert "XOP" not in tick, tick
    assert len(tick) == 4, tick
    P = L["portfolio"]
    assert P["positionsValue"] <= P["totalValue"] * 0.95 + 1, (P["positionsValue"], P["totalValue"])
    # second run the same day must not re-run the daily decision
    n = len(L["runs"])
    run(L, DAY, "10:30", FakeData(DAY, paths, bars))
    assert len(L["runs"]) == n
    print("daily OK:", run_log["headline"])
    print("  summary:", run_log["marketSummary"][:160])
    print("  top3:", [(r["ticker"], r["score"]) for r in run_log["ranking"][:3]], "holdings:", tick)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL TESTS PASSED")
