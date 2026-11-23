"""Scenario tests for the trading engine (v7, the "x10" account) with synthetic market data (no network).

BASE is the live ledger under v6 (13 Nov 2026). The first run (Monday 16 Nov) resets it to a fresh 3.000 $ x10 account:
the old account is summarised in previousAccounts and archived, the index sleeve buys QQQ on margin (up to 10x), the
whole-market opportunity scan reads headlines and trades its plays, and a bust restarts the account.
BASE_V3 / BASE_V4 / BASE_V5 check the older migration chain.

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

import news as N  # noqa: E402

HEADLINES = {}   # ticker -> headline title used by the fake news feed (default: a headline no rule recognises)
N.fetch_yf = lambda t: [{"title": HEADLINES.get(t, f"{t} shares in focus today"), "url": "https://example.com/" + t,
                         "publisher": "Test wire", "at": "2026-11-16T07:30:00-05:00", "summary": "", "tags": []}]
NY = ZoneInfo("America/New_York")
BASE = json.loads((ROOT / "test_fixture_v6.json").read_text())   # live ledger under v6, 13 Nov 2026 close
BASE_V5 = json.loads((ROOT / "test_fixture_v5.json").read_text())   # live ledger under v5, 30 Oct 2026 13:33 ET
BASE_V4 = json.loads((ROOT / "test_fixture_v4.json").read_text())   # live ledger under v4, 23 Oct 2026 15:14 ET
BASE_V3 = json.loads((ROOT / "test_fixture_v3.json").read_text())   # live ledger under v3, 15 Oct 2026 12:41 ET
DEPOSITS = json.loads((ROOT / "deposits.json").read_text())
RESTORE = json.loads((ROOT / "v3_restore.json").read_text())
DAY, NXT = "2026-11-16", "2026-11-17"
SCRATCH = Path(__import__("tempfile").mkdtemp())
T.MOVERS = SCRATCH / "movers.json"
T.ARCHIVE = SCRATCH / "archive"
SPIKES = {   # Friday 13 Nov: Nasdaq stocks that closed 20 %+ higher (movers.py spikeHistory)
    "ZAPP": dict(name="Zapp Therapeutics Inc.", prevClose=8.0, price=11.6, d1Pct=45.0, dollarVolM=60.0),
    "BUYO": dict(name="Buyout Target Corp.", prevClose=20.0, price=27.0, d1Pct=35.0, dollarVolM=80.0, buyout=True),
    "PENY": dict(name="Penny Bio Inc.", prevClose=2.5, price=4.0, d1Pct=60.0, dollarVolM=30.0),
    "THIN": dict(name="Thin Trade Inc.", prevClose=9.0, price=12.6, d1Pct=40.0, dollarVolM=4.0),
    "SMLL": dict(name="Small Jump Inc.", prevClose=10.0, price=12.2, d1Pct=22.0, dollarVolM=50.0),
    "SSRX": dict(name="Down Again Inc.", prevClose=9.0, price=12.0, d1Pct=33.3, dollarVolM=70.0),
    "SMCX": dict(name="Semiconductor Manufacturing International Corp.", prevClose=6.0, price=8.4, d1Pct=40.0, dollarVolM=90.0),
}



def ledger(base=None):
    return copy.deepcopy(base or BASE)


class FakeData:
    def __init__(self, day, paths, bars, divs=None, movers=None):
        self.day, self.paths, self.bars, self.divs, self.movers = day, paths, bars, divs or {}, movers or []

    def screen_movers(self, min_pct=4.0, min_price=5.0):
        return [dict(r) for r in self.movers if abs(r["pct"]) >= min_pct and r["price"] >= min_price]

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


def noisy(end, k, sigma, n=260, seed=5):
    rnd = random.Random(seed)
    raw, p = [], 1.0
    for _ in range(n):
        p *= 1 + k + rnd.gauss(0, sigma)
        raw.append(p)
    return [round(end * r / raw[-1], 4) for r in raw]


def first_daily(deposits=DEPOSITS, bear=False, hhmm="10:35", mutate=None, qqq=None, spy=None, base=None, day=DAY,
                overrides=None, paths_fn=None, movers=None):
    L = ledger(base)
    paths = aligned_paths(L, bear)
    if qqq:
        paths["QQQ"] = qqq(paths["QQQ"][-1])
    if spy:
        paths["SPY"] = spy(paths["SPY"][-1])
    if paths_fn:
        paths_fn(paths)
    if mutate:
        mutate(L)
    ov = overrides(paths, nbars(hhmm)) if overrides else {}
    eng = run(L, hhmm, FakeData(day, paths, bars_for(paths, ov, n=nbars(hhmm)), movers=movers), day=day, deposits=deposits)
    return L, eng, paths


def shock(pc, gap, step, n):
    """Today's 5-minute bars: open `gap` away from the previous close, then drift by `step` per bar."""
    return flat_bars(round(pc * (1 + gap), 4), n=n, step=step)


def sleeve(L):
    """Index sleeve: QQQ long/short market values and the exposure as a multiple of the account."""
    P = L["portfolio"]
    lng = sum(p["marketValue"] for p in P["positions"] if p["ticker"] == "QQQ" and p["side"] == "long")
    sht = sum(-p["marketValue"] for p in P["positions"] if p["ticker"] == "QQQ" and p["side"] == "short")
    return {"long": lng, "short": sht}, (lng - sht) / P["totalValue"]


def noisy_qqq(end):
    """QQQ with about 25 % annual volatility over the last 30 days (so the index sleeve stays near 2x)."""
    up = trend(end, 0.0012)
    return [round(x * (1 + (0.016 if i % 2 else -0.016) * (i >= 230)), 4) for i, x in enumerate(up)]
# ---------------------------------------------------------------- tests (v7)
def ev(L):
    return {p["ticker"]: p for p in L["portfolio"]["positions"] if p.get("sleeve") == "event"}


def mover(t, name, price, pct, cap=20.0, dv=300.0, ex="N"):
    return {"ticker": t, "name": name, "price": price, "pct": pct, "capB": cap, "dvM": dv, "ex": ex}


def test_v7_reset_to_x10_account():
    old = ledger()["portfolio"]
    L, eng, _ = first_daily(qqq=noisy_qqq, hhmm="10:05")
    P, M, C = L["portfolio"], L["meta"], L["config"]
    prev = L["previousAccounts"][-1]
    assert M["engine"] == T.ENGINE == "x10 v7" and prev["endValue"] == old["totalValue"] and prev["invested"] == 3000.0, prev
    assert prev["engine"] == "trend-event v6" and not prev["bust"] and prev["end"] == DAY
    assert (T.ARCHIVE / f"ledger_{DAY}.json").exists() and prev["archive"] == f"archive/ledger_{DAY}.json"
    arch = json.loads((T.ARCHIVE / f"ledger_{DAY}.json").read_text())
    assert arch["meta"]["engine"] == "trend-event v6" and len(arch["trades"]) == len(BASE["trades"])
    assert C["startCapital"] == 3000.0 and C["startDate"] == DAY and C["x10"]["maxGross"] == 10.0
    assert all(t["date"] == DAY for t in L["trades"]) and not any(p["ticker"] == "NVDA" for p in P["positions"])
    assert L["evalHistory"][-1]["strategy"].startswith("v6") and L["scorecard"]["strategy"] == "v7" and L["busts"] == []
    mv, expo = sleeve(L)
    assert abs(expo - M["index"]["target"]) < 0.05 and P["bustCount"] == 0 and P["paidInTotal"] == 3000.0, (expo, M["index"])
    assert abs(P["totalValue"] - 3000) < 15 and P["regTInitialReq"] <= P["totalValue"], P["totalValue"]
    print("x10 reset OK: previous account", prev["endValue"], "->", P["totalValue"], "| index", round(expo, 2), "x")


def test_v7_calm_market_goes_to_10x_on_margin():
    L, eng, _ = first_daily(hhmm="10:05")          # a perfectly smooth QQQ history: volatility below the 5 % floor
    P, M = L["portfolio"], L["meta"]
    mv, expo = sleeve(L)
    assert M["index"]["target"] == 10.0 and 9.5 < expo <= 10.0 and P["cash"] < -26000, (M["index"], expo, P["cash"])
    assert P["leverage"] <= 10.0 and 3.5 < P["closeoutMovePct"] < 6.5 and P["buyingPower"] < 600, (P["leverage"], P["closeoutMovePct"])
    assert "krediyle" in L["trades"][-1]["reason"] and any("kredi borcu" in d for d in L["runs"][-1]["decisions"])
    # the next day interest on the borrowed money is charged
    eng = run(L, "09:40", FakeData(NXT, _paths_with(L), bars_for(_paths_with(L), {}, n=3)), day=NXT)
    debit = 27000
    assert L["portfolio"]["costs"]["interest"] >= round(debit * T.MARGIN_RATE / 360, 2) - 0.5, L["portfolio"]["costs"]
    print("calm market OK: exposure", round(expo, 2), "x, debit", round(-P["cash"]), "| interest/day",
          L["portfolio"]["costs"]["interest"])


def _paths_with(L):
    return aligned_paths(L)


def test_v7_high_volatility_shrinks_and_downtrend_shorts():
    def wild(end):
        up = trend(end, 0.0012)
        return [round(x * (1 + (0.03 if i % 2 else -0.03) * (i >= 230)), 4) for i, x in enumerate(up)]
    L, _, _ = first_daily(qqq=wild, hhmm="10:05")
    _, expo = sleeve(L)
    assert L["meta"]["index"]["vol"] > 80 and 0 < expo < 0.7 and L["portfolio"]["cash"] > 0, (L["meta"]["index"], expo)
    L2, _, _ = first_daily(spy=lambda end: trend(end, -0.0012), hhmm="10:05")
    mv, expo2 = sleeve(L2)
    assert L2["meta"]["index"]["dir"] == -1 and mv["long"] == 0 and -3.05 < expo2 < -2.8, (mv, expo2)
    assert L2["trades"][-1]["action"] == "SHORT" and L2["trades"][-1]["ticker"] == "QQQ"
    print("high volatility OK:", round(expo, 2), "x | down-trend OK: short", round(expo2, 2), "x")


def test_v7_no_short_after_nasdaq_20pct_fall():
    def qqq(end):
        up = trend(end, 0.0012)
        return [round(x * (0.75 if i >= 230 else 1.0), 4) for i, x in enumerate(up)]
    L, _, _ = first_daily(spy=lambda end: trend(end, -0.0012), qqq=qqq, hhmm="10:05")
    mv, _ = sleeve(L)
    assert L["meta"]["nasdaq"]["active"] and L["meta"]["index"]["target"] == 0 and mv == {"long": 0, "short": 0}
    print("Nasdaq -20% rule OK: no short")


def test_v7_closeout_then_bust_and_restart():
    L, _, paths = first_daily(hhmm="10:05")          # 10x QQQ on margin
    q0 = next(p for p in L["portfolio"]["positions"] if p["ticker"] == "QQQ")["lastPrice"]
    # QQQ falls 6 %: the account is below 5 % of the exposure -> forced close-out, but not yet a bust
    n = nbars("11:30")
    ov = {"QQQ": flat_bars(q0, n=n - 1) + [("11:25", q0, q0, q0 * 0.94, q0 * 0.94)]}
    eng = run(L, "11:30", FakeData(DAY, paths, bars_for(paths, ov, n=n)))
    P = L["portfolio"]
    sold = [t for t in eng.new_trades if t["rule"] == "zorunlu tasfiye"]
    assert sold and not P["positions"] and 900 < P["totalValue"] < 1500 and L["meta"]["closeouts"] == 1, (P["totalValue"], sold)
    assert any(c["rule"] == "zorunlu tasfiye" for c in L["compliance"]) and L["busts"] == [] and eng.status == "alarm"
    # a second account: 10x again, then a 9.7 % fall wipes it out -> bust, archive, fresh 3.000 $
    L2, _, paths2 = first_daily(hhmm="10:05")
    ov = {"QQQ": flat_bars(q0, n=n - 1) + [("11:25", q0, q0, q0 * 0.903, q0 * 0.903)]}
    eng = run(L2, "11:30", FakeData(DAY, paths2, bars_for(paths2, ov, n=n)))
    P, M = L2["portfolio"], L2["meta"]
    assert len(L2["busts"]) == 1 and P["cash"] == 3000.0 and not P["positions"] and P["totalValue"] == 3000.0, (L2["busts"], P["cash"])
    b = L2["busts"][0]
    assert b["n"] == 1 and b["endValue"] < 300 and b["lost"] > 2700 and "batış" in b["text"] and b["date"] == DAY, b
    assert P["bustCount"] == 1 and P["paidInTotal"] == 6000.0 and P["lostToBusts"] == b["lost"]
    assert L2["previousAccounts"][-1]["bust"] and (T.ARCHIVE / f"ledger_{DAY}_batis1.json").exists()
    assert M["pausedOn"] == DAY and M["engine"] == T.ENGINE and L2["trades"] == [] and eng.status == "alarm"
    assert any(c["rule"] == "batış" for c in L2["compliance"]) and L2["checks"][DAY][-1]["kind"] == "batış"
    # no new trade on the day of the bust; the next day the new account trades again
    eng = run(L2, "13:35", FakeData(DAY, paths2, bars_for(paths2, {}, n=nbars("13:35"))))
    assert not eng.new_trades and L2["portfolio"]["cash"] == 3000.0
    eng = run(L2, "10:05", FakeData(NXT, paths2, bars_for(paths2, {}, n=nbars("10:05"))), day=NXT)
    assert any(t["rule"] == "endeks kolu" for t in eng.new_trades) and L2["portfolio"]["bustCount"] == 1
    print("close-out OK:", sold[0]["realizedPL"], "| bust OK:", b["text"][:110])


OPP = {   # ticker: (name, previous close, opening gap, drift per 5-minute bar, headline)
    "NKE": ("NIKE, Inc.", None, -0.10, 0.002, "Nike shares slide after analyst downgrade"),            # gap down, recovers: no trade
    "AVGO": ("Broadcom Inc.", None, 0.10, -0.002, "Broadcom jumps as retail traders pile in"),          # gap up, fades: no trade
    "CONT": ("Contex Corp", 80.0, 0.09, 0.002, "Contex shares in focus today"),                         # gap up, keeps rising: drift
    "MINI": ("Minicap Corp", 12.0, 0.09, 0.002, "Minicap shares in focus today"),                       # same but 1 B$: too small
    "BA": ("The Boeing Company", None, -0.10, -0.002, "Boeing shares in focus today"),                  # keeps falling, no news type
    "FRDX": ("Fraudex Corp", 40.0, -0.03, -0.004, "SEC opens investigation into Fraudex accounting"),   # hard bad news, confirmed
    "GUDE": ("Gudex Inc", 50.0, 0.05, 0.003, "Gudex raises full-year guidance after record quarter"),   # hard good news, confirmed
    "TKOV": ("Takeover Target Inc", 30.0, 0.25, 0.0, "Takeover Target agrees to be acquired for $38 per share"),
    "RUMR": ("Rumorex Inc", 20.0, 0.02, 0.006, "Rumorex is reportedly in talks to sell itself, sources say"),
    "FADE": ("Fadex Inc", 60.0, 0.09, -0.0001, "Fadex wins FDA approval for its heart drug"),           # good news, price gave it back
    "SSRD": ("Sunk Corp", 25.0, -0.12, -0.003, "Sunk Corp cuts guidance, shares plunge"),               # hard bad news but -10 %: SSR
    "TINY": ("Tiny Bio Inc", 8.0, 0.30, 0.004, "Tiny Bio wins FDA approval"),                           # too small: filtered out
}


def opp_setup():
    def paths_fn(paths):
        for t, (name, pc, *_r) in OPP.items():
            if pc is not None:
                paths[t] = [pc] * 260
        paths["QQQ"] = noisy_qqq(paths["QQQ"][-1])

    def overrides(paths, n):
        out = {t: shock(paths[t][-1], gap, step, n) for t, (_n, _pc, gap, step, _h) in OPP.items()}
        last = out["FADE"][-1]                       # FADE: far below the day's high at scan time
        hi = max(b[2] for b in out["FADE"])
        out["FADE"][-1] = (last[0], last[1], last[2], hi * 0.955, hi * 0.96)
        return out

    def movers(paths, n):
        rows = []
        for t, (name, _pc, gap, step, _h) in OPP.items():
            price = round(paths[t][-1] * (1 + gap) * (1 + step) ** n, 2)
            rows.append(mover(t, name, price, round((price / paths[t][-1] - 1) * 100, 2),
                              cap=0.1 if t == "TINY" else 1.0 if t == "MINI" else 20.0, dv=5.0 if t == "TINY" else 300.0))
        return rows
    HEADLINES.clear()
    HEADLINES.update({t: v[4] for t, v in OPP.items()})
    return paths_fn, overrides, movers


def opp_first(hhmm="10:35"):
    paths_fn, overrides, movers = opp_setup()
    L = ledger()
    paths = aligned_paths(L)
    paths_fn(paths)
    n = nbars(hhmm)
    data = FakeData(DAY, paths, bars_for(paths, overrides(paths, n), n=n), movers=movers(paths, n))
    eng = run(L, hhmm, data)
    return L, eng, paths


def test_v7_opportunity_scan_plays():
    L, eng, _ = opp_first()
    E, scan = ev(L), L["meta"]["oppScan"]
    rows = {r["t"]: r for r in scan["rows"]}
    assert set(E) == {"CONT", "FRDX", "GUDE"}, (set(E), {t: (r.get("why"), r.get("kind")) for t, r in rows.items()})
    assert E["CONT"]["play"] == "drift" and E["CONT"]["side"] == "long" and E["CONT"]["playLabel"] == "Haber devamı"
    assert E["FRDX"]["play"] == "news" and E["FRDX"]["side"] == "short" and E["GUDE"]["play"] == "news" and E["GUDE"]["side"] == "long"
    assert E["FRDX"]["catalyst"]["cat"] in ("probe", "fraud") and E["GUDE"]["catalyst"]["cat"] == "guideraise"
    assert E["CONT"]["exitDate"] == "2026-11-18" and E["FRDX"]["exitDate"] == "2026-11-19" and E["FRDX"]["name"] == "Fraudex Corp"
    assert "TINY" not in rows and scan["liquid"] == len(OPP) - 1 and scan["movers"] == len(OPP)
    why = {t: r.get("why") or "" for t, r in rows.items()}
    assert "tersine döndü" in why["NKE"] and "tersine döndü" in why["AVGO"] and "MINI" not in E, why
    assert "satın alma" in why["TKOV"] and "söylenti" in why["RUMR"] and "onaylamıyor" in why["FADE"], why
    assert "Rule 201" in why["SSRD"] and ("uymuyor" in why["BA"] or "bulunamadı" in why["BA"]), why
    eq = L["portfolio"]["totalValue"]
    for t, p in E.items():
        P_ = T.PLAYS[p["play"]]
        risk = abs(p["stopPrice"] / p["avgCost"] - 1)
        assert abs(p["marketValue"]) <= eq * P_["cap"] + 2 and abs(p["marketValue"]) * risk <= eq * P_["risk"] * 1.15, \
            (t, p["marketValue"], risk)
        assert (p["stopPrice"] < p["avgCost"]) == (p["side"] == "long") and p["stopMode"] == "event"
    fr = E["FRDX"]
    assert T.NEWS_STOP_MIN - 0.003 <= fr["stopPrice"] / fr["avgCost"] - 1 <= T.NEWS_STOP_MAX + 0.003
    tr = {t["ticker"]: t for t in eng.new_trades if t["rule"] in ("haber devamı", "haber takibi")}
    assert len(tr) == 3 and "Haber devamı" in tr["CONT"]["reason"] and "deneysel" in tr["FRDX"]["reason"] and "Haber türü" in tr["FRDX"]["reason"], tr["FRDX"]["reason"]
    assert tr["FRDX"]["sources"][0]["url"] == "https://example.com/FRDX" and tr["FRDX"]["name"] == "Fraudex Corp"
    assert any("Fırsat taraması 10:30" in d for d in L["runs"][-1]["decisions"]) and L["runs"][-1]["opps"][0]["taken"]
    assert L["portfolio"]["leverage"] <= T.MAX_GROSS and L["meta"]["oppDone"] == {DAY: [0]}
    print("opportunity scan OK:", {t: (p["play"], p["side"], round(p["marketValue"])) for t, p in E.items()},
          "| leverage", L["portfolio"]["leverage"])


def test_v7_later_scan_only_follows_news_and_runs_once():
    L, eng, paths = opp_first(hhmm="12:05")         # the 10:30 scan was missed: at 12:00 only the news play runs
    E = ev(L)
    why = {r["t"]: r.get("why") or "" for r in L["meta"]["oppScan"]["rows"]}
    # by noon FRDX has fallen more than 10 %: SEC Rule 201 blocks the short; the gap play is 10:30-only
    assert set(E) == {"GUDE"} and L["meta"]["oppScan"]["slot"] == 1 and "Rule 201" in why["FRDX"], (set(E), why)
    n0 = len(L["trades"])
    paths_fn, overrides, movers = opp_setup()
    k = nbars("12:20")
    eng = run(L, "12:20", FakeData(DAY, paths, bars_for(paths, overrides(paths, k), n=k), movers=movers(paths, k)))
    assert len(L["trades"]) == n0 and L["meta"]["oppDone"] == {DAY: [1]}
    print("later scan OK:", sorted(E), "| once per slot")


def test_v7_opportunity_stop_and_timed_exit():
    L, _, paths = opp_first()
    nke, gude = ev(L)["CONT"], ev(L)["GUDE"]
    paths["CONT"] = paths["CONT"][:-1] + [nke["lastPrice"]]
    for t in ("FRDX", "GUDE"):
        paths[t] = paths[t][:-1] + [ev(L)[t]["lastPrice"]]
    ov = {"CONT": flat_bars(nke["lastPrice"], n=3) + [("09:45", nke["lastPrice"], nke["lastPrice"], nke["stopPrice"] * 0.99,
                                                       nke["stopPrice"] * 0.995)]}
    eng = run(L, "09:55", FakeData(NXT, paths, bars_for(paths, ov, n=6)), day=NXT)
    sold = [t for t in eng.new_trades if t["ticker"] == "CONT"]
    assert sold and sold[0]["rule"] == "haber devamı zarar-kesi" and sold[0]["play"] == "drift", sold
    st = L["playStats"]["drift"]
    assert st["closed"] == 1 and st["wins"] == 0 and st["pl"] == sold[0]["realizedPL"] < 0, st
    n = nbars("15:55")
    eng = run(L, "15:55", FakeData("2026-11-19", paths, bars_for(paths, {"GUDE": flat_bars(gude["lastPrice"] * 1.06, n=n)}, n=n)),
              day="2026-11-19")
    out = {t["ticker"]: t for t in eng.new_trades}
    assert out["GUDE"]["rule"] == "haber takibi çıkış" and out["GUDE"]["realizedPL"] > 0 and "FRDX" in out, out.keys()
    assert L["playStats"]["news"]["closed"] == 2 and L["playStats"]["news"]["wins"] >= 1 and not ev(L), L["playStats"]
    print("opportunity stop + timed exit OK: CONT", sold[0]["realizedPL"], "| GUDE", out["GUDE"]["realizedPL"],
          "| stats", {k: (v["closed"], v["pl"]) for k, v in L["playStats"].items()})


def run_r(L, hhmm, data, R, day=DAY):
    now = datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=NY)
    eng = T.Engine(L, data, now, DEPOSITS, RESTORE, R)
    eng.run()
    check_books(L)
    return eng


def test_v71_shadow_book_health_and_reviews():
    paths_fn, overrides, movers = opp_setup()
    L = ledger()
    paths = aligned_paths(L)
    paths_fn(paths)
    R = {}
    n = nbars("10:35")
    run_r(L, "10:35", FakeData(DAY, paths, bars_for(paths, overrides(paths, n), n=n), movers=movers(paths, n)), R)
    sh = {r[2]: r for r in R["shadow"]["rows"]}
    k = {t: r[11] for t, r in sh.items()}
    assert k["CONT"] == "drift" and k["NKE"] == "gapTurn" and k["AVGO"] == "gapTurn" and k["MINI"] == "gapSmall", k
    assert k["TKOV"] == "pinned" and k["FRDX"] == "news" and k["GUDE"] == "news" and sh["CONT"][13] == "al", k
    assert sh["FRDX"][12] == -1 and sh["GUDE"][12] == 1 and sh["CONT"][12] == 1 and sh["BA"][12] is None
    assert len(R["days"]) == 1 and R["days"][0]["date"] == DAY and R["days"][0]["exact"]
    d0 = R["days"][0]
    assert abs(sum(d0["plays"].values()) - d0["pl"]) < 0.02, d0
    snap = L["snapshots"][-1]
    assert snap.get("qqqPrice") and all("u" in h and h.get("play") for h in snap["holdings"]), snap["holdings"]
    # next morning: yesterday's closes are filled in, the rules are judged
    for t in list(OPP):
        if t in paths:
            p_ = next((p for p in L["portfolio"]["positions"] if p["ticker"] == t), None)
            last = p_["lastPrice"] if p_ else sh[t][4] if t in sh else paths[t][-1]
            paths[t] = paths[t][:-1] + [last * 1.02]
    m = nbars("10:05")
    run_r(L, "10:05", FakeData(NXT, paths, bars_for(paths, {}, n=m)), R, day=NXT)
    rows = {r[2]: r for r in R["shadow"]["rows"]}
    assert rows["CONT"][16].get("0") is not None and abs(rows["BA"][16]["0"] - ((paths["BA"][-1] / rows["BA"][4] - 1) * 100)) < 0.05
    H = L["meta"]["health"]
    assert set(H) >= {"drift", "news"} and H["drift"]["status"] == "açık" and H["drift"]["n"] == 0, H
    # ten losing drift signals in the shadow book -> the rule is paused; ten winners -> back on
    def inject(sign, day0):
        for i in range(12):
            R["shadow"]["rows"].append([day0, "10:30", f"X{i}", "x", 10.0, 9.0, 9.0, 1.0, 0.9, 5.0, 80.0, "drift", 1,
                                        "atla", "drift", "", {"0": sign * 1.0, "1": sign * 2.0, "2": sign * 3.0}])
    inject(-1, "2026-11-12")
    eng = T.Engine(L, FakeData(NXT, paths, bars_for(paths, {}, n=m)), datetime.fromisoformat(f"{NXT}T10:10:00").replace(tzinfo=NY),
                   DEPOSITS, RESTORE, R)
    texts = eng.play_health()
    assert eng.play_paused("drift") and any("DURDURULDU" in t for t in texts) and L["meta"]["healthLog"][-1]["play"] == "drift"
    inject(1, "2026-11-13")
    R["shadow"]["rows"] = [r for r in R["shadow"]["rows"] if not (r[2].startswith("X") and r[0] == "2026-11-12")]
    texts = eng.play_health()
    assert not eng.play_paused("drift") and any("AÇIK" in t for t in texts), texts
    eng.build_reviews()
    assert R["weeks"] and R["weeks"][-1]["days"] == 2 and "Hafta" in R["weeks"][-1]["text"], R["weeks"]
    json.dumps(R)
    print("shadow + health + reviews OK:", R["days"][-1]["text"][:140])


def rebound_paths(L):
    paths = aligned_paths(L)
    up = [500 * (1.0012 ** i) for i in range(250)]
    paths["SPY"] = [round(x, 2) for x in up] + [round(up[-1] * (0.985 ** k), 2) for k in range(1, 11)]   # below the 200-day, 50 > 200
    q = [600 * (1 + 0.004 * ((i % 7) - 3) / 3) for i in range(258)]
    paths["QQQ"] = [round(x, 2) for x in q] + [round(q[-1] * 0.96, 2), round(q[-1] * 0.96 * 0.955, 2)]       # two hard down days
    paths["QQQM"] = [round(x / 2.4, 2) for x in paths["QQQ"]]
    return paths


def test_v71_rebound_rule():
    L = ledger()
    for t_ in list(L["portfolio"]["positions"]):
        pass
    paths = rebound_paths(L)
    n = nbars("09:40")
    eng = run(L, "09:40", FakeData(DAY, paths, bars_for(paths, {}, n=n)))
    M, P = L["meta"], L["portfolio"]
    pos = next((p for p in P["positions"] if p["ticker"] == "QQQM"), None)
    assert M["rebound"]["rsi2"] < 5 and not M["rebound"]["baseOn"], M["rebound"]
    assert pos and pos["play"] == "rebound" and pos["stopMode"] == "managed", (M["rebound"], [p["ticker"] for p in P["positions"]])
    eq = P["totalValue"]
    assert 2.7 * eq <= pos["marketValue"] <= 3.05 * eq, (pos["marketValue"], eq)
    assert any(t["rule"] == "tepki kuralı" for t in eng.new_trades) and M["rebDate"] == DAY
    # later the same day nothing happens again; at 10:05 the index rule stays in cash and does not touch QQQM
    eng = run(L, "10:05", FakeData(DAY, paths, bars_for(paths, {}, n=nbars("10:05"))))
    assert not [t for t in eng.new_trades if t["ticker"] == "QQQM"] and any(p["ticker"] == "QQQM" for p in L["portfolio"]["positions"])
    # next morning: QQQ jumped, RSI(2) > 50 -> sold at the open
    paths2 = {k: list(v) for k, v in paths.items()}
    paths2["QQQ"] = paths["QQQ"][1:] + [round(paths["QQQ"][-1] * 1.06, 2)]
    paths2["QQQM"] = [round(x / 2.4, 2) for x in paths2["QQQ"]]
    paths2["SPY"] = paths["SPY"][1:] + [paths["SPY"][-1]]
    eng = run(L, "09:40", FakeData(NXT, paths2, bars_for(paths2, {}, n=n)), day=NXT)
    sold = [t for t in eng.new_trades if t["ticker"] == "QQQM"]
    assert sold and sold[0]["rule"] == "tepki çıkış" and sold[0]["realizedPL"] > 0 and L["playStats"]["rebound"]["closed"] == 1, sold
    # a signal seen only after 10:00 is not traded (the rule buys at the open)
    L2 = ledger()
    p3 = rebound_paths(L2)
    run(L2, "10:20", FakeData(DAY, p3, bars_for(p3, {}, n=nbars("10:20"))))
    assert not any(p["ticker"] == "QQQM" for p in L2["portfolio"]["positions"]) and "kaçtı" in L2["meta"]["rebound"]["text"]
    print("rebound rule OK: RSI2", M["rebound"]["rsi2"], "| bought", round(pos["marketValue"]), "| sold", sold[0]["realizedPL"])


def test_v7_screener_fallback_to_hourly_scan():
    paths_fn, overrides, movers = opp_setup()
    L = ledger()
    paths = aligned_paths(L)
    paths_fn(paths)
    n = nbars("10:35")
    rows = movers(paths, n)
    cols = ["t", "n", "p", "d1", "dv", "cap", "ex"]
    T.MOVERS.write_text(json.dumps({"asOf": f"{DAY}T09:50-05:00", "barDay": DAY, "screen": {
        "cols": cols, "rows": [[r["ticker"], r["name"], r["price"], r["pct"], r["dvM"], r["capB"], "N"] for r in rows]}}))
    eng = run(L, "10:35", FakeData(DAY, paths, bars_for(paths, overrides(paths, n), n=n), movers=[]))
    assert "saatlik" in L["meta"]["oppScan"]["source"] and {"CONT", "FRDX"} <= set(ev(L)), L["meta"]["oppScan"]["source"]
    T.MOVERS.write_text("{}")
    print("screener fallback OK:", L["meta"]["oppScan"]["source"])


def test_catalyst_reading():
    import catalyst as C
    from datetime import timezone
    now = datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc)
    def one(t, name, title, mv, summary=""):
        return C.classify(t, name, [{"title": title, "summary": summary, "publisher": "Wire", "url": "https://example.com/x",
                                     "at": "2026-11-16T13:00:00+00:00"}], mv, now)
    r = one("ACME", "Acme Corp", "Hindenburg takes short position in Acme, alleges accounting fraud", -18)
    assert r["cat"] in ("shortreport", "fraud") and r["kind"] == "hard" and r["dir"] == -1 and not r["conflict"], r
    r = one("ACME", "Acme Corp", "Acme beats estimates but cuts full-year guidance", -9)
    assert r["cat"] == "guidecut" and r["mixed"], r
    r = one("ACME", "Acme Corp", "Acme to be acquired by Globex in all-cash deal", 30)
    assert r["kind"] == "pinned", r
    r = one("ACME", "Acme Corp", "Acme is said to explore a sale, people familiar say", 12)
    assert r["kind"] == "rumor" and r["dir"] == 1, r
    r = one("ACME", "Acme Corp", "SHAREHOLDER ALERT: Pomerantz Law Firm investigates claims on behalf of investors of Acme", -15)
    assert r["cat"] is None and r["relevant"] == 0, r
    r = one("ACME", "Acme Corp", "Globex wins FDA approval for new drug", 8)          # about another company
    assert r["cat"] is None and r["relevant"] == 0, r
    r = one("ACME", "Acme Corp", "Acme wins FDA approval for new drug", -6)            # good news, price falls
    assert r["cat"] == "approval" and r["conflict"], r
    print("catalyst reading OK")


def test_old_ledgers_migrate_to_v7():
    for base, day, hh in ((BASE_V3, "2026-10-15", "13:35"), (BASE_V4, "2026-10-23", "15:20"), (BASE_V5, "2026-11-02", "10:05")):
        L, _, _ = first_daily(base=base, day=day, hhmm=hh)
        assert L["meta"]["engine"] == T.ENGINE and L["previousAccounts"] and L["portfolio"]["leverage"] <= T.MAX_GROSS
    print("v3/v4/v5 -> v7 chain OK")


def test_scorecard_v7():
    L, _, paths = first_daily(qqq=noisy_qqq, hhmm="10:05")
    run(L, "10:35", FakeData(NXT, paths, bars_for(paths, {}, n=nbars("10:35"))), day=NXT)
    sc = L["scorecard"]
    assert sc["daysElapsed"] == 2 and sc["uptime"]["pct"] == 100.0 and sc["strategy"] == "v7" and len(sc["history"]) == 4, sc
    assert L["meta"]["oppScan"]["date"] == NXT and L["meta"]["oppScan"]["taken"] == []
    print("scorecard OK:", {k: sc[k] for k in ("start", "end", "returnPct", "spyReturnPct")})


def test_extended_watch():
    import watch as W
    L, _, paths = first_daily(qqq=noisy_qqq, hhmm="10:05")
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


def test_fees_and_commission():
    assert T.sale_fees(100, 0.5) == 0.02
    assert T.sale_fees(1_000_000, 10_000) == round(20.6 + 1.95, 2)
    assert T.commission(1, 100) == 0.35 and T.commission(0.5, 20) == 0.2 and T.commission(200, 1000) == 0.7
    assert abs(T.init_rate("TQQQ", "long") - 0.30) < 1e-9 and T.init_rate("AAPL", "long") == 0.10
    assert abs(T.maint_req("AAPL", "short", 10, 100.0) - 50.0) < 1e-6 and abs(T.maint_req("TQQQ", "long", 10, 100.0) - 150.0) < 1e-6
    print("fees, commission and x10 margin rates OK")


# ---------------------------------------------------------------- tests (sert yükseliş short'u)
def write_movers(final=True, day="2026-11-13"):
    rows = [{"ticker": t, "gapPct": 5.0, "buyout": False, "headline": None, **v} for t, v in SPIKES.items()]
    T.MOVERS.write_text(json.dumps({"spikeHistory": {day: {"final": final, "at": day + "T16:25-05:00", "rows": rows}}}))


def spike_paths(paths):
    for t, v in SPIKES.items():
        paths[t] = [v["prevClose"]] * 259 + [v["price"]]
    paths["QQQ"] = noisy_qqq(paths["QQQ"][-1])


def spike_open(paths, n):
    """ZAPP opens at 11.00 (a bit below Friday's close); SSRX opens 12 % lower, so SEC Rule 201 blocks a short."""
    return {"ZAPP": flat_bars(11.0, n=n), "SSRX": flat_bars(10.5, n=n)}


def spike_first(hhmm="09:40"):
    write_movers()
    out = first_daily(hhmm=hhmm, overrides=spike_open, paths_fn=spike_paths)
    T.MOVERS.write_text("{}")
    return out


def test_spike_short_entry():
    L, eng, _ = spike_first()
    sp = {p["ticker"]: p for p in L["portfolio"]["positions"] if p.get("spike")}
    scan = L["meta"]["spikeScan"]
    assert set(sp) == {"ZAPP"}, (set(sp), scan["skipped"])
    z = sp["ZAPP"]
    assert z["side"] == "short" and z["sleeve"] == "event" and z["play"] == "spike" and z["borrowRate"] == T.SPIKE_BORROW
    assert abs(z["stopPrice"] - 11.0 * (1 + T.SPIKE_STOP)) < 0.02 and z["exitDate"] == "2026-11-30", (z["stopPrice"], z["exitDate"])
    assert z["name"] == "Zapp Therapeutics Inc." and z["event"]["spikePct"] == 45.0
    assert abs(abs(z["marketValue"]) - L["portfolio"]["totalValue"] * T.SPIKE_SIZE) < 3, z["marketValue"]
    why = {x["ticker"]: x["why"] for x in scan["skipped"]}
    assert "satın alma" in why["BUYO"] and "5 $" in why["PENY"] and "hacmi" in why["THIN"], why
    assert "Rule 201" in why["SSRX"] and "yaptırım" in why["SMCX"] and "SMLL" not in scan["candidates"], why
    tr = [t for t in eng.new_trades if t["rule"] == "sert yükseliş short"]
    assert len(tr) == 1 and tr[0]["action"] == "SHORT" and "Zor ödünç" in tr[0]["locate"] and tr[0]["name"] == "Zapp Therapeutics Inc."
    print("spike short OK:", tr[0]["ticker"], tr[0]["value"], "| skipped:", sorted(why))


def test_spike_stop_borrow_and_time_exit():
    L, _, paths = spike_first()
    z = next(p for p in L["portfolio"]["positions"] if p.get("spike"))
    paths["ZAPP"] = paths["ZAPP"][:-1] + [11.0]
    L2 = copy.deepcopy(L)
    ov = {"ZAPP": flat_bars(11.5, n=2) + [("09:40", 12.0, z["stopPrice"] * 1.02, 11.9, 16.0)]}
    eng = run(L, "09:45", FakeData(NXT, paths, bars_for(paths, ov, n=nbars("09:45"))), day=NXT)
    cov = [t for t in eng.new_trades if t["ticker"] == "ZAPP"]
    assert cov and cov[0]["action"] == "COVER" and cov[0]["rule"] == "sert yükseliş zarar-kesi" and cov[0]["realizedPL"] < 0, cov
    assert L["portfolio"]["costs"]["borrow"] >= round(abs(z["marketValue"]) * T.SPIKE_BORROW / 360, 2) - 0.01
    assert L["playStats"]["spike"]["closed"] == 1
    n = nbars("15:55")
    eng = run(L2, "15:55", FakeData("2026-11-30", paths, bars_for(paths, {"ZAPP": flat_bars(8.0, n=n)}, n=n)), day="2026-11-30")
    out = [t for t in eng.new_trades if t["ticker"] == "ZAPP"]
    assert out and out[0]["rule"] == "sert yükseliş çıkış" and out[0]["realizedPL"] > 0, out
    print("spike stop OK:", cov[0]["realizedPL"], "| timed exit OK:", out[0]["realizedPL"])


def test_spike_missing_list():
    T.MOVERS.write_text("{}")
    L, _, _ = first_daily(hhmm="09:40", overrides=spike_open, paths_fn=spike_paths)
    assert not any(p.get("spike") for p in L["portfolio"]["positions"]) and L["meta"]["spikeScan"]["candidates"] == []
    print("spike scan without a list OK")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL TESTS PASSED")
