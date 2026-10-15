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
BASE = json.loads((ROOT / "test_fixture_v3.json").read_text())   # live ledger under v3, 15 Oct 2026 12:41 ET
DEPOSITS = json.loads((ROOT / "deposits.json").read_text())
RESTORE = json.loads((ROOT / "v3_restore.json").read_text())
DAY, NXT = "2026-10-15", "2026-10-16"



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


def run(L, hhmm, data, day=DAY, deposits=DEPOSITS):
    now = datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=NY)
    eng = T.Engine(L, data, now, deposits, RESTORE)
    eng.run()
    check_books(L)
    return eng


def regt_ok(P, tol=1.0):
    """After the daily rebalance the Reg T initial requirement must not exceed equity."""
    return P["regTInitialReq"] <= P["totalValue"] + tol


def aligned_paths(L, bear=False):
    """Synthetic histories whose last close matches the fixture's position prices (and SPY's)."""
    paths = base_paths(bear)
    for p in L["portfolio"]["positions"]:
        k = p["lastPrice"] / paths[p["ticker"]][-1]
        paths[p["ticker"]] = [round(x * k, 4) for x in paths[p["ticker"]]]
    return paths


def first_daily(deposits=DEPOSITS, bear=False, hhmm="13:35", mutate=None):
    L = ledger()
    paths = aligned_paths(L, bear)
    if mutate:
        mutate(L)
    eng = run(L, hhmm, FakeData(DAY, paths, bars_for(paths, {}, n=nbars(hhmm))), deposits=deposits)
    return L, eng, paths


# ---------------------------------------------------------------- tests (v4)
def basket(L):
    return [p for p in L["portfolio"]["positions"] if p["ticker"] != T.CORE_ETF]


def test_v4_migration():
    L, eng, _ = first_daily()
    P, M = L["portfolio"], L["meta"]
    assert M["engine"] == T.ENGINE and M["lastRebalance"] == "2026-10" and M["trendUp"] is True
    assert all(p["side"] == "long" and p["ticker"] not in U.LEVERAGED for p in P["positions"]), P["positions"]
    assert T.CORE_ETF in {p["ticker"] for p in P["positions"]}
    b = basket(L)
    assert 1 <= len(b) <= T.BASKET_N
    groups = {}
    for p in b:
        groups[U.group_of(p["ticker"])] = groups.get(U.group_of(p["ticker"]), 0) + 1
    assert max(groups.values()) <= T.GROUP_CAP, groups
    assert P["cash"] >= -0.01 and (P["leverage"] or 0) <= 1.02, (P["cash"], P["leverage"])
    assert all(p["stopMode"] == "disaster" and abs(p["stopPrice"] - round(p["avgCost"] * 0.75, 2)) < 0.02 for p in P["positions"])
    assert L["evalHistory"][0]["strategy"].startswith("v3") and L["scorecard"]["strategy"] == "v4"
    assert L["scorecard"]["daysElapsed"] == 1 and L["scorecard"]["end"] == "2027-01-15"
    assert any(c["rule"] == "strateji" for c in L["compliance"])
    assert any(t["action"] == "COVER" for t in eng.new_trades)
    core = next(p for p in P["positions"] if p["ticker"] == T.CORE_ETF)
    assert abs(core["marketValue"] / P["totalValue"] - 0.49) < 0.03, core["marketValue"] / P["totalValue"]
    print("v4 migration OK:", L["runs"][-1]["headline"], "| equity", P["totalValue"], "| cash", P["cash"],
          "| lev", P["leverage"], "|", sorted((p["ticker"], round(p["marketValue"])) for p in P["positions"]))


def test_v4_midmonth_quiet():
    L, _, paths = first_daily()
    n = len(L["trades"])
    eng = run(L, "10:30", FakeData(NXT, paths, bars_for(paths, {}, n=nbars("10:30"))), day=NXT)
    assert len(L["trades"]) == n, [t["ticker"] for t in eng.new_trades]
    assert L["runs"][-1]["headline"].startswith("İşlem yok") and L["runs"][-1]["rebalanced"] is False
    print("mid-month quiet OK:", L["runs"][-1]["decisions"][-1][:90])


def test_v4_disaster_stop_and_cooldown():
    L, _, paths = first_daily()
    p = basket(L)[0]
    t, stop = p["ticker"], p["stopPrice"]
    ov = {t: flat_bars(p["lastPrice"], n=3) + [("09:45", p["lastPrice"], p["lastPrice"], stop * 0.98, stop * 0.99)]}
    eng = run(L, "09:55", FakeData(NXT, paths, bars_for(paths, ov, n=6)), day=NXT)
    sold = [x for x in eng.new_trades if x["ticker"] == t and x["action"] == "SELL"]
    assert sold and sold[0]["rule"] == "felaket zarar-kesi" and abs(sold[0]["quotePrice"] - stop) < 1e-6, sold
    assert L["meta"]["cooldown"][t] == NXT
    # next month's rebalance must not buy it back within 30 days
    run(L, "10:30", FakeData("2026-11-02", paths, bars_for(paths, {}, n=nbars("10:30"))), day="2026-11-02")
    assert L["meta"]["lastRebalance"] == "2026-11"
    assert t not in {p["ticker"] for p in L["portfolio"]["positions"]}
    row = next(r for r in L["runs"][-1]["ranking"] if r["ticker"] == t) if any(r["ticker"] == t for r in L["runs"][-1]["ranking"]) else None
    assert row is None or "bekleme" in row["why"], row
    print("disaster stop + cooldown OK:", t, "sold at", sold[0]["quotePrice"])


def test_v4_trend_off_goes_to_cash():
    L, _, paths = first_daily()
    paths2 = copy.deepcopy(paths)
    paths2["SPY"] = paths2["SPY"][:-3] + [round(x * 0.88, 2) for x in paths2["SPY"][-3:]]
    eng = run(L, "10:30", FakeData(NXT, paths2, bars_for(paths2, {}, n=nbars("10:30"))), day=NXT)
    P = L["portfolio"]
    assert not P["positions"], [p["ticker"] for p in P["positions"]]
    assert L["meta"]["trendUp"] is False and abs(P["cash"] - P["totalValue"]) < 0.01
    assert all(x["rule"] == "trend filtresi" for x in eng.new_trades)
    # still down the day after: nothing to do
    run(L, "10:30", FakeData("2026-10-19", paths2, bars_for(paths2, {}, n=nbars("10:30"))), day="2026-10-19")
    assert not L["portfolio"]["positions"]
    print("trend filter OK: all cash", P["cash"])


def test_scorecard_v4():
    L, _, paths = first_daily()
    run(L, "10:30", FakeData(NXT, paths, bars_for(paths, {}, n=nbars("10:30"))), day=NXT)
    sc = L["scorecard"]
    assert sc["daysElapsed"] == 2 and sc["uptime"]["pct"] == 100.0 and sc["history"], sc
    print("scorecard OK:", {k: sc[k] for k in ("start", "end", "returnPct", "spyReturnPct", "maxDrawdownPct")})


def test_extended_watch():
    import watch as W
    L, _, paths = first_daily()
    day = DAY
    def bars(ref, ext):
        idx = [pd.Timestamp(f"{day} {m // 60:02d}:{m % 60:02d}", tz=NY) for m in range(570, 1081, 5)]
        return pd.DataFrame([[ref if m < 960 else ext] * 4 for m in range(570, 1081, 5)], index=idx,
                            columns=["Open", "High", "Low", "Close"])
    b = {p["ticker"]: bars(p["lastPrice"], p["lastPrice"] * 1.05) for p in L["portfolio"]["positions"]}
    now = datetime.fromisoformat(f"{day}T18:00:00").replace(tzinfo=NY)
    assert W.session_of(now, T.HOLIDAYS) == "post"
    out = W.build(L, b, now, "post")
    assert out["totalImpact"] > 0 and all(abs(r["changePct"] - 5) < 0.01 for r in out["items"])
    print("extended watch OK: impact", out["totalImpact"])


def test_news_parsing():
    import news as N
    from datetime import timedelta, timezone
    now = datetime(2026, 10, 15, 17, 0, tzinfo=timezone.utc)
    new_fmt = {"content": {"title": "Barclays downgrades Dell on enterprise AI timing", "summary": "<p>x</p>",
                           "pubDate": "2026-10-14T13:05:00Z", "provider": {"displayName": "Reuters"},
                           "canonicalUrl": {"url": "https://example.com/a"}}}
    old_fmt = {"title": "Fed's Williams sees one more rate hike", "publisher": "Bloomberg", "link": "https://example.com/b",
               "providerPublishTime": int((now - timedelta(hours=5)).timestamp())}
    N.fetch_yf = lambda t: [n for n in (N.normalize(x) for x in [new_fmt, old_fmt]) if n]
    seen = set()
    r = N.collect("DELL", now, 4, seen)
    assert [x["publisher"] for x in r] == ["Bloomberg", "Reuters"] and "Analysts" in r[1]["tags"] and "Fed & rates" in r[0]["tags"]
    assert N.collect("SPY", now, 4, seen) == []
    # automatic daily brief
    L, _, paths = first_daily()
    snap = {"SPY": ("S&P 500", 770.0, 774.0), "QQQ": ("Nasdaq 100", 715.0, 726.0), "DIA": ("Dow", 517.0, 515.5),
            "IWM": ("Russell 2000", 272.0, 271.5), "^VIX": ("VIX", 21.3, 17.4), "^TNX": ("10-yr Treasury yield", 5.18, 5.06),
            "CL=F": ("WTI crude", 95.1, 94.4), "GC=F": ("Gold", 4200.0, 4176.0)}
    secs = [{"ticker": p["ticker"], "role": "long", "dayChangePct": 1.5 - i, "items": [
        {"title": f"{p['ticker']} headline", "url": "https://example.com/" + p["ticker"], "publisher": "Reuters",
         "at": "2026-10-15T11:00-04:00", "summary": "", "tags": ["AI & chips"]}]} for i, p in enumerate(L["portfolio"]["positions"])]
    market = [{"title": "Fed's Williams sees another hike", "url": "https://example.com/m", "publisher": "Bloomberg",
               "at": "2026-10-15T09:00-04:00", "summary": "Rates may rise again. More text.", "tags": ["Fed & rates"]}]
    dg = N.build_digest(L, secs, market, snap, 724.2, [{"ticker": "MU", "date": "2026-10-28"}],
                        datetime(2026, 10, 15, 17, 0, tzinfo=timezone.utc))
    heads = [b["head"] for b in dg["bullets"]]
    assert heads[:3] == ["Markets (latest session)", "Trend filter", "Our portfolio"], heads
    assert "Tech lagged" in dg["bullets"][0]["text"] and "elevated" in dg["bullets"][0]["text"] and "jumped" in dg["bullets"][0]["text"]
    assert "above its 200-day" in dg["bullets"][1]["text"] and "Upcoming earnings (next 3 weeks)" in heads
    assert dg["headlines"][0]["summary"] == "Rates may rise again." and len(dg["movers"]) == 3
    print("news parsing + daily brief OK:", dg["title"], "|", dg["bullets"][0]["text"][:120])


def test_fees_and_commission():
    assert T.sale_fees(100, 0.5) == 0.02
    assert T.sale_fees(1_000_000, 10_000) == round(20.6 + 1.95, 2)
    assert T.commission(1, 100) == 0.35 and T.commission(0.5, 20) == 0.2 and T.commission(200, 1000) == 0.7
    assert T.init_rate("TQQQ", "long") == 0.75 and T.init_rate("AAPL", "long") == 0.5
    print("fees and commission OK")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL TESTS PASSED")
