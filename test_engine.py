"""Scenario tests for the Reg T long/short engine (v3) with synthetic market data (no network).

BASE is the live ledger as it was under v2 (12 Oct 2026, 13:12 ET). The first run migrates it: the v2 trades of
13:01 are cancelled, the positions held before them are restored and the compliant v3 daily decision runs.

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
BASE = json.loads((ROOT / "test_fixture_ledger.json").read_text())   # live ledger under v2, 12 Oct 13:12 ET
DEPOSITS = json.loads((ROOT / "deposits.json").read_text())
RESTORE = json.loads((ROOT / "v3_restore.json").read_text())
DAY, NXT = "2026-10-12", "2026-10-13"
V2_DEPOSITS = [d for d in DEPOSITS if d["id"] == "2026-10-12-1700"]   # capital at the v2 cut-over: 2.000 $
BUFFER = DEPOSITS                                                     # deposits.json: 3.000 $


def ledger():
    return copy.deepcopy(BASE)


class FakeData:
    def __init__(self, day, paths, bars, divs=None):
        self.day, self.paths, self.bars, self.divs = day, paths, bars, divs or {}

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

    def dividends(self, tickers):
        return {t: s for t, s in self.divs.items() if t in tickers}


def nbars(hhmm):
    h, m = map(int, hhmm.split(":"))
    return (h * 60 + m - 570) // 5 + 1


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
    assert abs(P["cash"] + P["longValue"] - P["shortValue"] - P["totalValue"]) < 0.02
    for p in P["positions"]:
        assert p["shares"] > 0, p
    cash = L["config"]["startCapital"]
    for t in L["trades"]:
        if t.get("cancelled"):
            continue
        a, comm, fees = t["action"], T.num(t.get("commission")), T.num(t.get("fees"))
        if a in ("BUY", "COVER"):
            cash -= t["value"] + comm
        elif a in ("SELL", "SHORT"):
            cash += t["value"] - fees - comm
        elif a == "DEPOSIT":
            cash += t["value"]
    c = P.get("costs", {})
    cash -= c.get("interest", 0) + c.get("borrow", 0) + c.get("dividendsPaid", 0) - c.get("dividendsReceived", 0)
    assert abs(cash - P["cash"]) <= 0.05, (round(cash, 2), P["cash"])
    # realized P/L in the portfolio equals the sum over live closing trades
    rp = sum(T.num(t.get("realizedPL")) for t in L["trades"] if not t.get("cancelled") and t["action"] in ("SELL", "COVER"))
    assert abs(rp - P["realizedPL"]) <= 0.05, (rp, P["realizedPL"])


def run(L, hhmm, data, day=DAY, deposits=V2_DEPOSITS):
    now = datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=NY)
    eng = T.Engine(L, data, now, deposits, RESTORE)
    eng.run()
    check_books(L)
    return eng


def regt_ok(P, tol=1.0):
    """After the daily rebalance the Reg T initial requirement must not exceed equity."""
    return P["regTInitialReq"] <= P["totalValue"] + tol


def first_daily(deposits=V2_DEPOSITS, bear=False, hhmm="13:35", mutate=None):
    paths = base_paths(bear)
    L = ledger()
    if mutate:
        mutate(L)
    eng = run(L, hhmm, FakeData(DAY, paths, bars_for(paths, {}, n=nbars(hhmm))), deposits=deposits)
    return L, eng, paths


# ---------------------------------------------------------------- tests
def test_rollback_and_first_v3_daily():
    v2_trades = [t for t in BASE["trades"] if t["ts"] >= RESTORE["v2Start"] and t["action"] in ("BUY", "SELL", "SHORT", "COVER")]
    L, eng, _ = first_daily()
    P, M = L["portfolio"], L["meta"]
    assert M["engine"] == T.ENGINE and M["lastDailyDate"] == DAY
    cancelled = [t for t in L["trades"] if t.get("cancelled")]
    assert len(cancelled) == len(v2_trades) == 8, (len(cancelled), len(v2_trades))
    assert all(t.get("cancelReason") for t in cancelled)
    assert sum(t["action"] == "DEPOSIT" for t in L["trades"]) == 1
    assert all(r.get("cancelled") for r in L["runs"] if r["at"] >= RESTORE["v2Start"] and r["by"] == "bot"
               and r is not L["runs"][-1])
    old = [t for t in L["trades"] if t["ts"] < RESTORE["v2Start"] and t["action"] in ("BUY", "SELL")]
    assert old and all(t.get("commissionBackfilled") and t["commission"] > 0 for t in old)
    rules = [c["rule"] for c in L["compliance"]]
    assert "geçiş" in rules and "Reg T" in rules and "aracı kurum ücreti" in rules, rules
    assert regt_ok(P), (P["regTInitialReq"], P["totalValue"])
    assert P["leverage"] <= 3.0 + 1e-6
    assert P["totalValue"] > P["maintenanceReq"]
    if not P["marginEligible"]:
        assert P["cash"] >= -0.01, P["cash"]
    print("rollback + v3 daily OK:", L["runs"][-1]["headline"], "| equity", P["totalValue"], "| eligible",
          P["marginEligible"], "| init req", P["regTInitialReq"], "| cash", P["cash"],
          "|", sorted((p["ticker"], p["side"], round(abs(p["marketValue"]))) for p in P["positions"]))
    # same day again: no re-migration, no second daily decision
    n_tr, n_runs, n_c = len(L["trades"]), len(L["runs"]), len(L["compliance"])
    paths = base_paths()
    run(L, "13:40", FakeData(DAY, paths, bars_for(paths, {}, n=nbars("13:40"))))
    assert len(L["runs"]) == n_runs and len(L["trades"]) == n_tr and len(L["compliance"]) == n_c


def test_regt_with_buffer():
    L, eng, _ = first_daily(BUFFER)
    P = L["portfolio"]
    assert P["marginEligible"] and P["netDeposits"] == 3000
    shorts = [p for p in P["positions"] if p["side"] == "short"]
    lev = [p for p in P["positions"] if p["ticker"] in U.LEVERAGED]
    assert shorts and lev, [p["ticker"] for p in P["positions"]]
    assert regt_ok(P), (P["regTInitialReq"], P["totalValue"])
    assert P["regTInitialReq"] >= 0.80 * P["totalValue"], "budget should be mostly used"
    assert all(t.get("commission", 0) >= 0.35 or t["value"] * 0.01 <= 0.35 for t in eng.new_trades
               if t["action"] in ("BUY", "SELL", "SHORT", "COVER"))
    assert all(t.get("locate") for t in eng.new_trades if t["action"] == "SHORT")
    assert any(c["rule"] == "para yatırma" for c in L["compliance"])
    assert P["totalValue"] - T.MIN_MARGIN_EQUITY > 900, P["totalValue"]
    print("Reg T with 3.000 $ OK: lev", P["leverage"], "| equity", P["totalValue"], "| init req", P["regTInitialReq"],
          "| buying power", P["buyingPower"], "|", sorted((p["ticker"], p["side"]) for p in P["positions"]))


def test_below_2000_cash_only():
    def poorer(L):
        L["config"]["startCapital"] = 250      # 50 $ less cash after the rollback -> equity ~1.950 $
    L, eng, _ = first_daily(mutate=poorer)
    P = L["portfolio"]
    assert not P["marginEligible"], P["totalValue"]
    assert not [p for p in P["positions"] if p["side"] == "short"]
    assert P["cash"] >= -0.01, P["cash"]
    assert any(c["rule"] == "FINRA 4210(b)(4)" for c in L["compliance"])
    print("below 2.000 $ OK: equity", P["totalValue"], "cash", P["cash"], "| no shorts, no margin loan")


def test_ssr_blocks_short():
    paths = base_paths()
    ov = {}
    for t in ("NKE", "INTC", "BA", "UNH"):
        prev = paths[t][-1]
        bars = flat_bars(prev * 0.95, n=nbars("13:35"))
        o, h, l, c = bars[3][1:]
        bars[3] = (bars[3][0], o, h, prev * 0.88, c)
        ov[t] = bars
    L = ledger()
    eng = run(L, "13:35", FakeData(DAY, paths, bars_for(paths, ov, n=nbars("13:35"))), deposits=BUFFER)
    held = {p["ticker"] for p in L["portfolio"]["positions"] if p["side"] == "short"}
    assert not held & {"NKE", "INTC", "BA", "UNH"}, held
    ssr = [c for c in L["compliance"] if c["rule"] == "SEC Rule 201"]
    assert ssr, [c["rule"] for c in L["compliance"]]
    print("SSR OK:", [c["title"] for c in ssr], "| shorts instead:", sorted(held))


def test_wash_sale():
    L, eng, paths = first_daily(BUFFER)
    p = next(p for p in L["portfolio"]["positions"] if p["side"] == "long" and p["ticker"] not in U.LEVERAGED)
    t = p["ticker"]
    now = datetime.fromisoformat(f"{NXT}T10:30:00").replace(tzinfo=NY)
    e = T.Engine(L, FakeData(NXT, paths, {}), now, BUFFER, RESTORE)
    ts = pd.Timestamp(now)
    e.close(p, p["shares"], p["avgCost"] * 0.9, ts, "test: zararla satış", "test")
    loss_tr = L["trades"][-1]
    assert loss_tr["realizedPL"] < 0
    e.open_long(t, 100, p["avgCost"] * 0.9, ts, "test: yeniden alım", "test")
    assert loss_tr.get("washSale") and loss_tr["washSale"]["disallowed"] == round(-loss_tr["realizedPL"], 2)
    assert L["tax"]["2026"]["washDisallowed"] >= -loss_tr["realizedPL"] - 0.01
    assert e._pos(t, "long")["taxBasisAdj"] > 0
    assert loss_tr["holdingDays"] >= 0 and loss_tr["taxTerm"] == "kısa vade"
    assert any(c["rule"] == "wash sale" for c in L["compliance"])
    print("wash sale OK:", t, loss_tr["washSale"], "| tax", L["tax"]["2026"])


def test_short_stop_and_gap():
    L, eng, paths = first_daily(BUFFER)
    shorts = [p for p in L["portfolio"]["positions"] if p["side"] == "short"][:2]
    assert len(shorts) == 2, [p["ticker"] for p in L["portfolio"]["positions"]]
    a, b = shorts
    pa = a["lastPrice"]
    ov = {a["ticker"]: flat_bars(pa, n=3) + [("09:45", pa * 1.01, a["stopPrice"] + 0.5, pa, pa * 1.05)],
          b["ticker"]: [("09:30", b["stopPrice"] * 1.03, b["stopPrice"] * 1.04, b["stopPrice"] * 1.02, b["stopPrice"] * 1.03)]}
    eng = run(L, "09:55", FakeData(NXT, paths, bars_for(paths, ov, n=6)), day=NXT, deposits=BUFFER)
    cov = {t["ticker"]: t for t in eng.new_trades if t["action"] == "COVER"}
    assert abs(cov[a["ticker"]]["quotePrice"] - a["stopPrice"]) < 1e-6, cov
    assert abs(cov[b["ticker"]]["quotePrice"] - round(b["stopPrice"] * 1.03, 4)) < 0.01, cov
    assert cov[a["ticker"]]["commission"] > 0 and cov[a["ticker"]]["holdingDays"] == 1
    assert L["portfolio"]["costs"]["borrow"] > 0
    print("short stop + gap OK; interest", L["portfolio"]["costs"]["interest"], "borrow", L["portfolio"]["costs"]["borrow"],
          "| tax", L["tax"]["2026"])


def test_squeeze_and_crash_brakes():
    L, eng, paths = first_daily(BUFFER)
    spy_up = flat_bars(paths["SPY"][-1] * 1.035, n=6)
    run(L, "09:55", FakeData(NXT, paths, bars_for(paths, {"SPY": spy_up}, n=6)), day=NXT, deposits=BUFFER)
    P = L["portfolio"]
    short_exp = sum(p["exposure"] for p in P["positions"] if T.direction(p) < 0)
    assert short_exp <= 0.5 * P["totalValue"] * 1.01, (short_exp, P["totalValue"])
    L2, _, paths2 = first_daily(BUFFER)
    spy_dn = flat_bars(paths2["SPY"][-1] * 0.955, n=6)
    run(L2, "09:55", FakeData(NXT, paths2, bars_for(paths2, {"SPY": spy_dn}, n=6)), day=NXT, deposits=BUFFER)
    P2 = L2["portfolio"]
    long_exp = sum(p["exposure"] for p in P2["positions"] if T.direction(p) > 0)
    assert long_exp <= 1.0 * P2["totalValue"] * 1.01, (long_exp, P2["totalValue"])
    print("squeeze brake OK | crash brake OK")


def test_maintenance_call():
    L, _, paths = first_daily(BUFFER)
    L["portfolio"]["cash"] -= 1400
    L["config"]["startCapital"] -= 1400   # keep the cash reconciliation consistent
    eng = run(L, "09:55", FakeData(NXT, paths, bars_for(paths, {}, n=6)), day=NXT, deposits=BUFFER)
    P = L["portfolio"]
    assert "teminat çağrısı" in {t["rule"] for t in eng.new_trades}
    assert P["totalValue"] >= P["maintenanceReq"], (P["totalValue"], P["maintenanceReq"])
    assert any(c["rule"] == "FINRA 4210" for c in L["compliance"])
    print("maintenance call OK: equity", P["totalValue"], "req", P["maintenanceReq"])


def test_bear_regime():
    L, _, _ = first_daily(BUFFER, bear=True)
    P, M = L["portfolio"], L["meta"]
    assert M["regime"]["name"] == "bear", M["regime"]
    inv = [p for p in P["positions"] if p["ticker"] in U.LEVERAGED]
    assert inv and all(U.LEVERAGED[p["ticker"]][1] < 0 for p in inv), [p["ticker"] for p in inv]
    short_exp = sum(p["exposure"] for p in P["positions"] if T.direction(p) < 0)
    long_exp = sum(p["exposure"] for p in P["positions"] if T.direction(p) > 0)
    assert short_exp > long_exp and regt_ok(P), (short_exp, long_exp)
    print("bear regime OK: lev", P["leverage"], [p["ticker"] + ":" + p["side"] for p in P["positions"]])


def test_dividend():
    L, eng, paths = first_daily(BUFFER)
    longs = [p for p in L["portfolio"]["positions"] if p["side"] == "long"]
    shorts = [p for p in L["portfolio"]["positions"] if p["side"] == "short"]
    divs = {longs[0]["ticker"]: pd.Series([1.0], index=[pd.Timestamp(NXT, tz=NY)]),
            shorts[0]["ticker"]: pd.Series([0.5], index=[pd.Timestamp(NXT, tz=NY)])}
    run(L, "10:15", FakeData(NXT, paths, bars_for(paths, {}, n=nbars("10:15")), divs), day=NXT, deposits=BUFFER)
    tax = L["tax"]["2026"]
    assert tax.get("dividendsReceived", 0) > 0 and tax.get("dividendsPaidShort", 0) > 0, tax
    print("dividends OK:", tax)


def test_scorecard():
    L, eng, paths = first_daily(BUFFER, hhmm="14:10")
    sc = L["scorecard"]
    assert sc["daysElapsed"] == 1 and sc["uptime"]["pct"] == 100.0, sc["uptime"]
    assert abs(sc["returnPct"]) < 1 and sc["maxDrawdownPct"] <= 0
    assert [c["key"] for c in sc["criteria"]] == ["spy", "dd", "margin", "uptime"]
    # next day: every long falls 6 % -> drawdown is recorded, SPY flat -> behind the index
    ov = {p["ticker"]: flat_bars(p["lastPrice"] * 0.94, n=nbars("10:15")) for p in L["portfolio"]["positions"]
          if T.direction(p) > 0}
    run(L, "10:15", FakeData(NXT, paths, bars_for(paths, ov, n=nbars("10:15"))), day=NXT, deposits=BUFFER)
    sc = L["scorecard"]
    assert sc["daysElapsed"] == 2 and sc["uptime"]["ran"] == 2
    assert sc["maxDrawdownPct"] < -3, sc["maxDrawdownPct"]
    assert sc["excessPct"] < 0 and not sc["criteria"][0]["ok"]
    assert sc["monthly"] and sc["monthly"][0]["month"] == "2026-10"
    assert sc["daysTotal"] == len(T.trading_days(T.EVAL_START, T.EVAL_END))
    print("scorecard OK:", {k: sc[k] for k in ("returnPct", "spyReturnPct", "excessPct", "maxDrawdownPct", "daysElapsed",
                                                 "daysTotal")}, sc["trades"], sc["monthly"])


def test_fees_and_commission():
    assert T.sale_fees(100, 0.5) == 0.02
    assert T.sale_fees(1_000_000, 10_000) == round(20.6 + 1.95, 2)
    assert T.sale_fees(10, 100_000) == round(0.01 + 9.79, 2)
    assert T.commission(1, 100) == 0.35          # minimum
    assert T.commission(0.5, 20) == 0.2           # capped at 1 % of value
    assert T.commission(200, 1000) == 0.7         # 0.0035 $/share
    assert T.init_rate("TQQQ", "long") == 0.75 and T.init_rate("AAPL", "long") == 0.5
    assert T.init_rate("AAPL", "short") == 0.5    # 150 % incl. the sale proceeds
    assert T.maint_req("AAPL", "short", 10, 20) == 60.0   # 30 % of 200 < 10 x 5 $? no: max(60, 50)
    assert T.maint_req("AAPL", "short", 10, 12) == 50.0   # 5 $/share minimum
    assert T.maint_req("SOXL", "long", 10, 100) == 750.0
    print("fees, commission and margin rates OK")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL TESTS PASSED")
