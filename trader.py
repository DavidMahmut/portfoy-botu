"""Rule-based paper-trading bot for the 300 $ portfolio.

Runs every 5 minutes during US market hours (a GitHub Actions job loops until the close). Each run:
  1. marks the portfolio to market with Yahoo Finance 5-minute bars,
  2. replays the bars since the last run against each position's standing stop order,
  3. applies take-profit, single-stock shock and market crash-brake rules,
  4. once per day after 10:00 New York time, ranks the universe by momentum and
     rebalances (sell what fell out of favour, buy the leaders),
  5. writes everything (trades with reasons, risk checks, daily report) to ledger.json.

No AI and no paid service is involved; all decisions follow the fixed rules below.
"""
import json
import math
import os
import sys
import traceback
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import universe as U

NY = ZoneInfo("America/New_York")
LEDGER = Path(__file__).resolve().parent / "ledger.json"

SLIP = 0.0005           # 0.05 % slippage on every fill
TOP_N = 4               # target number of positions
KEEP_RANK = 10          # a holding is kept while it stays in the top 10
INVEST_FULL = 0.95      # max invested share in a healthy market
INVEST_WEAK = 0.50      # max invested share when SPY < 200-day average
STOP_COST = 0.92        # initial stop: 8 % below cost
STOP_TRAIL = 0.88       # trailing stop: 12 % below the highest price since entry
TAKE_PROFIT = 1.20      # sell half at +20 %
SHOCK_PCT = -7.0        # single-stock shock threshold (day change, %)
BRAKE1, BRAKE2 = -3.0, -5.0   # SPY day change thresholds (%)
CASH_BRAKE1, CASH_BRAKE2 = 0.50, 0.80
MIN_TRADE = 10.0
STALE_MIN = 35          # minutes; older quotes are not traded on
DAILY_AFTER = dtime(10, 0)
CHECK_DAYS_KEPT = 5
SCHEDULE_TEXT = ("Piyasa açıkken 5 dakikada bir (GitHub Actions döngüsü). Günlük sıralama ve alımlar "
                 "10:00'dan (New York) sonraki ilk çalışmada.")

HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
}

YF_SOURCE = {"title": "yfinance (Yahoo Finance verileri)", "url": "https://github.com/ranaroussi/yfinance"}


# ---------------------------------------------------------------- formatting
def trn(x, d=2):
    s = f"{abs(x):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("−" if x < 0 else "") + s


def trpct(x, d=2):
    return ("+" if x > 0 else "−" if x < 0 else "") + "%" + trn(abs(x), d)


def usd(x):
    return trn(x, 2) + " $"


def iso(ts):
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    return ts.astimezone(NY).isoformat(timespec="seconds")


def hm(ts):
    return pd.Timestamp(ts).tz_convert(NY).strftime("%H:%M")


def src(t):
    return {"title": f"Yahoo Finance: {t}", "url": U.quote_url(t)}


def market_open(now):
    d = now.date().isoformat()
    if now.weekday() >= 5 or d in HOLIDAYS:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 10)


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, ledger, data, now):
        self.L = ledger
        self.data = data
        self.now = now
        self.today = now.date().isoformat()
        self.P = ledger["portfolio"]
        self.C = ledger["config"]
        self.M = ledger.setdefault("meta", {})
        self.C["schedule"] = SCHEDULE_TEXT
        self.actions = []
        self.notes = []
        self.status = "normal"
        self.quotes = {}      # ticker -> {price, at, prevClose, dayChangePct, sma50}
        self.new_trades = []

    # ---------------- bookkeeping
    def _trade_id(self):
        prefix = "t-" + self.today.replace("-", "") + "-"
        used = [int(t["id"][-2:]) for t in self.L["trades"] if t.get("id", "").startswith(prefix)]
        return prefix + f"{(max(used) if used else 0) + 1:02d}"

    def _log_trade(self, action, t, shares, quote, quote_at, ex, value, reason, rule, realized=None, extra_src=None):
        tr = {
            "id": self._trade_id(), "ts": iso(self.now), "date": self.today, "action": action, "ticker": t,
            "name": U.name_of(t), "shares": shares, "quotePrice": round(quote, 4), "quoteAt": iso(quote_at),
            "slippagePct": SLIP * 100, "execPrice": ex, "value": value,
            "priceSource": {"title": f"Yahoo Finance 5 dk mum ({hm(quote_at)} ET)", "url": U.quote_url(t)},
            "reason": reason, "rule": rule, "sources": [src(t), YF_SOURCE] + (extra_src or []),
            "runId": self.today, "by": "bot",
        }
        if realized is not None:
            tr["realizedPL"] = realized
        self.L["trades"].append(tr)
        self.new_trades.append(tr)
        return tr

    def _pos(self, t):
        for p in self.P["positions"]:
            if p["ticker"] == t:
                return p
        return None

    def sell(self, p, shares, quote, quote_at, reason, rule):
        shares = min(round(shares, 4), p["shares"])
        if shares <= 0:
            return None
        full = p["shares"] - shares < 0.0001
        if full:
            shares = p["shares"]
        ex = round(quote * (1 - SLIP), 4)
        value = round(shares * ex, 2)
        realized = round(shares * (ex - p["avgCost"]), 2)
        self.P["cash"] = round(self.P["cash"] + value, 2)
        self.P["realizedPL"] = round(self.P.get("realizedPL", 0) + realized, 2)
        tr = self._log_trade("SELL", p["ticker"], shares, quote, quote_at, ex, value, reason, rule, realized)
        if full:
            self.P["positions"].remove(p)
        else:
            remaining = round(p["shares"] - shares, 4)
            p["costBasis"] = round(p["costBasis"] * remaining / p["shares"], 2)
            p["shares"] = remaining
        self.actions.append(f"{p['ticker']} {'satıldı' if full else 'kısmen satıldı'} ({usd(value)})")
        return tr

    def buy(self, t, amount, quote, quote_at, reason, rule, extra_src=None):
        ex = round(quote * (1 + SLIP), 4)
        amount = min(amount, self.P["cash"])
        shares = math.floor(amount / ex * 10000) / 10000
        value = round(shares * ex, 2)
        while value > self.P["cash"] and shares > 0:
            shares = round(shares - 0.0001, 4)
            value = round(shares * ex, 2)
        if shares <= 0 or value < MIN_TRADE:
            return None
        self.P["cash"] = round(self.P["cash"] - value, 2)
        p = self._pos(t)
        if p:
            tot = p["shares"] + shares
            p["avgCost"] = round((p["avgCost"] * p["shares"] + ex * shares) / tot, 4)
            p["costBasis"] = round(p["costBasis"] + value, 2)
            p["shares"] = round(tot, 4)
        else:
            self.P["positions"].append({
                "ticker": t, "name": U.name_of(t), "kind": U.kind_of(t), "shares": shares, "avgCost": ex,
                "costBasis": value, "lastPrice": quote, "lastPriceAt": iso(quote_at),
                "priceSource": {"title": "Yahoo Finance", "url": U.quote_url(t)},
                "marketValue": round(shares * quote, 2), "unrealized": 0.0, "unrealizedPct": 0.0,
                "openedAt": self.today, "openedTs": iso(quote_at), "stopPrice": round(ex * STOP_COST, 2),
                "highSinceEntry": quote, "tookProfit": False, "thesis": reason,
            })
        self._log_trade("BUY", t, shares, quote, quote_at, ex, value, reason, rule, extra_src=extra_src)
        self.actions.append(f"{t} alındı ({usd(value)})")
        return True

    def revalue(self):
        for p in self.P["positions"]:
            q = self.quotes.get(p["ticker"])
            if q:
                p["lastPrice"] = round(q["price"], 4)
                p["lastPriceAt"] = iso(q["at"])
                p["priceSource"] = {"title": f"Yahoo Finance 5 dk mum ({hm(q['at'])} ET)", "url": U.quote_url(p["ticker"])}
            p["marketValue"] = round(p["shares"] * p["lastPrice"], 2)
            p["unrealized"] = round(p["marketValue"] - p["costBasis"], 2)
            p["unrealizedPct"] = round(p["unrealized"] / p["costBasis"] * 100, 2) if p["costBasis"] else 0.0
        pv = round(sum(p["marketValue"] for p in self.P["positions"]), 2)
        total = round(self.P["cash"] + pv, 2)
        start = self.C["startCapital"]
        self.P.update(positionsValue=pv, totalValue=total, totalPL=round(total - start, 2),
                      totalPLPct=round((total - start) / start * 100, 2), asOf=iso(self.now))
        spy = self.quotes.get("SPY")
        if spy:
            self.P["benchmarkPrice"] = round(spy["price"], 2)
            self.P["benchmarkValue"] = round(self.C["benchmark"]["shares"] * spy["price"], 2)
        assert self.P["cash"] >= -0.005, "cash negative"
        assert abs(self.P["cash"] + sum(p["marketValue"] for p in self.P["positions"]) - total) <= 0.011

    # ---------------- quotes
    def load_quotes(self, tickers):
        tickers = sorted(set(tickers) | {"SPY"})
        bars = self.data.intraday(tickers)
        hist = self.data.daily(tickers, period="6mo")
        today = self.now.date()
        for t in tickers:
            b = bars.get(t)
            h = hist.get(t)
            if b is None or b.empty or h is None or h.empty:
                self.notes.append(f"{t}: güncel veri alınamadı")
                continue
            b = b[b.index.date == today]
            if b.empty:
                self.notes.append(f"{t}: bugüne ait veri yok")
                continue
            past = h[h.index.date < today]["Close"]
            if past.empty:
                continue
            prev = float(past.iloc[-1])
            last_ts = b.index[-1]
            price = float(b["Close"].iloc[-1])
            closes = list(past.iloc[-49:]) + [price]
            sma50 = sum(closes) / len(closes)
            age = (pd.Timestamp(self.now) - last_ts).total_seconds() / 60
            self.quotes[t] = {"price": price, "at": last_ts, "prevClose": prev, "bars": b,
                              "dayChangePct": (price / prev - 1) * 100, "sma50": sma50, "stale": age > STALE_MIN}

    # ---------------- risk guard
    def guard(self):
        for p in list(self.P["positions"]):
            q = self.quotes.get(p["ticker"])
            if not q or q["stale"]:
                self.notes.append(f"{p['ticker']}: veri gecikmeli, işlem yapılmadı")
                continue
            b = q["bars"]
            if p.get("openedTs"):
                b = b[b.index > pd.Timestamp(p["openedTs"])]
            if p.get("lastBarTs"):
                b = b[b.index >= pd.Timestamp(p["lastBarTs"])]
            high, stop = p["highSinceEntry"], p["stopPrice"]
            hit = None
            for ts, row in b.iterrows():
                eff = round(max(stop, high * STOP_TRAIL), 2)
                if row["Low"] <= eff:
                    fill = float(row["Open"]) if row["Open"] < eff else eff
                    hit = (ts, eff, float(row["Low"]), fill)
                    break
                high = max(high, float(row["High"]))
                stop = round(max(stop, high * STOP_TRAIL), 2)
            if hit:
                ts, eff, low, fill = hit
                gap = fill < eff
                reason = (f"Bekleyen zarar-kes emri tetiklendi: {hm(ts)} ET mumunda fiyat {trn(low)} $'a indi, "
                          f"zarar-kes seviyesi {trn(eff)} $. "
                          + (f"Mum seviyenin altında açıldığı için açılış fiyatından ({trn(fill)} $) satıldı."
                             if gap else f"Emir {trn(fill)} $'dan gerçekleşti."))
                self.sell(p, p["shares"], fill, ts, reason, "zarar-kes")
                self.status = "alarm"
                continue
            p["highSinceEntry"] = round(high, 4)
            p["stopPrice"] = round(max(stop, high * STOP_TRAIL), 2)
            if len(b):
                p["lastBarTs"] = iso(b.index[-1])
            price = q["price"]
            # take profit
            if not p.get("tookProfit") and price >= p["avgCost"] * TAKE_PROFIT:
                half = math.floor(p["shares"] / 2 * 10000) / 10000
                p["tookProfit"] = True
                self.sell(p, half, price, q["at"],
                          f"Kâr alma: fiyat {trn(price)} $, ortalama maliyetin ({trn(p['avgCost'])} $) %20 üstünde; "
                          f"pozisyonun yarısı satıldı, kalanı izleyen zarar-kes ile taşınıyor.", "kâr alma")
                self.status = "alarm" if self.status == "alarm" else "uyari"
                continue
            # single-stock shock
            if q["dayChangePct"] <= SHOCK_PCT and price < q["sma50"]:
                self.sell(p, p["shares"], price, q["at"],
                          f"Tek hisse şoku: gün içinde {trpct(q['dayChangePct'])} düştü ve 50 günlük ortalamanın "
                          f"({trn(q['sma50'])} $) altına indi; zarar-kes beklenmeden satıldı.", "tek hisse şoku")
                self.status = "alarm"
        self.crash_brake()

    def crash_brake(self):
        spy = self.quotes.get("SPY")
        if not spy or spy["stale"]:
            return
        chg = spy["dayChangePct"]
        if chg > BRAKE1:
            if chg <= -1.5 and self.status == "normal":
                self.status = "uyari"
            return
        target = CASH_BRAKE2 if chg <= BRAKE2 else CASH_BRAKE1
        self.M["brakeDate"] = self.today
        self.status = "alarm"
        self.revalue()
        need = self.P["totalValue"] * target - self.P["cash"]
        if need <= 0:
            self.notes.append(f"Çöküş freni: SPY {trpct(chg)}, nakit zaten %{int(target*100)} üstünde")
            return
        self.actions.append(f"Çöküş freni: SPY {trpct(chg)} → nakit hedefi %{int(target * 100)}")
        order = sorted(self.P["positions"], key=lambda p: self.quotes.get(p["ticker"], {}).get("dayChangePct", 0))
        for p in order:
            if need <= 0.5:
                break
            q = self.quotes.get(p["ticker"])
            if not q or q["stale"]:
                continue
            per_share = q["price"] * (1 - SLIP)
            shares = min(p["shares"], math.ceil(need / per_share * 10000) / 10000)
            tr = self.sell(p, shares, q["price"], q["at"],
                           f"Çöküş freni: SPY önceki kapanışa göre {trpct(chg)} düştü; nakit en az "
                           f"%{int(target*100)}'e çıkarılıyor. Günün en çok düşen pozisyonu önce satıldı "
                           f"({p['ticker']} {trpct(q['dayChangePct'])}).", "çöküş freni")
            if tr:
                need -= tr["value"]

    # ---------------- daily momentum decision
    def rank(self, hist):
        rows = []
        for t in U.UNIVERSE:
            h = hist.get(t)
            if h is None or len(h) < 130:
                continue
            c = list(h["Close"])
            price = c[-1]
            r21, r63, r126 = (price / c[-22] - 1) * 100, (price / c[-64] - 1) * 100, (price / c[-127] - 1) * 100
            sma50 = sum(c[-50:]) / 50
            score = 0.2 * r21 + 0.5 * r63 + 0.3 * r126
            reasons = []
            if price < 5:
                reasons.append("fiyat 5 $ altında")
            if price <= sma50:
                reasons.append("50 günlük ortalamanın altında")
            if r63 <= 0:
                reasons.append("3 aylık getiri negatif")
            rows.append({"ticker": t, "name": U.name_of(t), "price": round(price, 2), "r21": round(r21, 2),
                         "r63": round(r63, 2), "r126": round(r126, 2), "sma50": round(sma50, 2),
                         "score": round(score, 2), "eligible": not reasons, "why": ", ".join(reasons)})
        rows.sort(key=lambda r: r["score"], reverse=True)
        rank = 0
        for r in rows:
            if r["eligible"]:
                rank += 1
                r["rank"] = rank
        return rows

    def indicators(self, hist):
        out = []
        for t, label in U.INDICATORS:
            h = hist.get(t)
            if h is None or len(h) < 2:
                continue
            last, prev = float(h["Close"].iloc[-1]), float(h["Close"].iloc[-2])
            if t == "^TNX":
                val = "%" + trn(last, 2)
                chg = ("+" if last >= prev else "−") + trn(abs(last - prev) * 100, 0) + " baz puan"
            elif t == "^VIX":
                val, chg = trn(last, 2), trpct((last / prev - 1) * 100)
            else:
                val, chg = trn(last, 2) + " $", trpct((last / prev - 1) * 100)
            out.append({"label": label, "value": val, "change": chg, "ticker": t, "last": last, "prev": prev})
        return out

    def daily(self):
        held = [p["ticker"] for p in self.P["positions"]]
        hist = self.data.daily(U.UNIVERSE + held + [t for t, _ in U.INDICATORS], period="1y")
        ranking = self.rank(hist)
        inds = self.indicators(hist)
        spy_h = hist.get("SPY")
        spy_c = list(spy_h["Close"]) if spy_h is not None else []
        sma200 = sum(spy_c[-200:]) / len(spy_c[-200:]) if len(spy_c) >= 200 else None
        healthy = sma200 is None or spy_c[-1] > sma200
        max_inv = INVEST_FULL if healthy else INVEST_WEAK
        by_t = {r["ticker"]: r for r in ranking}
        decisions, rejected, watch = [], [], []
        sources = [YF_SOURCE] + [src(t) for t, _ in U.INDICATORS]

        # 1) sells: momentum faded
        for p in list(self.P["positions"]):
            r = by_t.get(p["ticker"])
            q = self.quotes.get(p["ticker"])
            if r is None:
                decisions.append(f"{p['ticker']} tut: momentum verisi yok (evren dışı), yalnızca zarar-kes ile korunuyor.")
                continue
            if r["eligible"] and r.get("rank", 99) <= KEEP_RANK:
                decisions.append(f"{p['ticker']} tut: momentum sıralamasında {r['rank']}. (puan {trn(r['score'])}), "
                                 f"zarar-kes {trn(p['stopPrice'])} $.")
                continue
            if not q or q["stale"]:
                decisions.append(f"{p['ticker']} satılmalıydı ama güncel fiyat yok; sonraki çalışmaya bırakıldı.")
                continue
            why = r["why"] if not r["eligible"] else f"sıralamada {r['rank']}. sıraya düştü (ilk {KEEP_RANK} dışında)"
            reason = (f"Momentum zayıfladı: {why}. 1 ay {trpct(r['r21'])}, 3 ay {trpct(r['r63'])}, "
                      f"6 ay {trpct(r['r126'])}; daha güçlü adaylara yer açmak için satıldı.")
            self.sell(p, p["shares"], q["price"], q["at"], reason, "momentum")
            decisions.append(f"{p['ticker']} SAT: {why}.")
            sources.append(src(p["ticker"]))

        # 2) buys: fill free slots with the leaders
        eligible = [r for r in ranking if r["eligible"]]
        brake_today = self.M.get("brakeDate") == self.today
        slots = TOP_N - len(self.P["positions"])
        cands = [r for r in eligible if not self._pos(r["ticker"])]
        if brake_today:
            decisions.append("Bugün çöküş freni devreye girdiği için yeni alım yapılmadı.")
        elif slots <= 0:
            decisions.append(f"Portföy dolu ({len(self.P['positions'])}/{TOP_N}); yeni alım yok.")
        else:
            picks = cands[:slots]
            if picks:
                self.quotes_for([r["ticker"] for r in picks])
            self.revalue()
            for r in picks:
                q = self.quotes.get(r["ticker"])
                if not q or q["stale"]:
                    rejected.append(f"{r['ticker']}: güncel 5 dakikalık fiyat alınamadı, alım ertelendi.")
                    continue
                total = self.P["totalValue"]
                room = total * max_inv - self.P["positionsValue"]
                amount = min(total * max_inv / TOP_N, room, self.P["cash"])
                if amount < MIN_TRADE:
                    rejected.append(f"{r['ticker']}: yatırım sınırına ulaşıldı (en fazla %{int(max_inv*100)}).")
                    continue
                reason = (f"Momentum lideri: sıralamada {r['rank']}. (puan {trn(r['score'])}). Getiriler: 1 ay "
                          f"{trpct(r['r21'])}, 3 ay {trpct(r['r63'])}, 6 ay {trpct(r['r126'])}; fiyat 50 günlük "
                          f"ortalamanın ({trn(r['sma50'])} $) üstünde. Hedef ağırlık ~%{trn(max_inv/TOP_N*100,1)}. "
                          f"Tez bozulur: fiyat zarar-kese inerse ya da sıralamada ilk {KEEP_RANK}'dan çıkarsa.")
                if self.buy(r["ticker"], amount, q["price"], q["at"], reason, "momentum"):
                    decisions.append(f"{r['ticker']} AL: sıralamada {r['rank']}., {usd(amount)} civarı.")
                    sources.append(src(r["ticker"]))
                    self.revalue()
        for r in cands[max(slots, 0):max(slots, 0) + 4]:
            watch.append({"ticker": r["ticker"], "note": f"Sıra {r['rank']}, puan {trn(r['score'])}; 3 ay {trpct(r['r63'])}."})
        for r in [x for x in ranking if not x["eligible"] and x["score"] > 0][:4]:
            rejected.append(f"{r['ticker']}: puan {trn(r['score'])}, ama alım koşulunu sağlamıyor ({r['why']}).")

        self.revalue()
        self.M["lastDailyDate"] = self.today
        spy_i = next((i for i in inds if i["ticker"] == "SPY"), None)
        qqq_i = next((i for i in inds if i["ticker"] == "QQQ"), None)
        iwm_i = next((i for i in inds if i["ticker"] == "IWM"), None)
        parts = []
        if spy_i:
            parts.append(f"S&P 500 (SPY) önceki kapanışa göre {spy_i['change']}")
        if qqq_i:
            parts.append(f"Nasdaq 100 (QQQ) {qqq_i['change']}")
        if iwm_i:
            parts.append(f"Russell 2000 (IWM) {iwm_i['change']}")
        others = [f"{i['label']} {i['value']} ({i['change']})" for i in inds if i["ticker"] in ("^VIX", "CL=F", "^TNX")]
        regime = (f"Piyasa rejimi: SPY 200 günlük ortalamanın ({trn(sma200)} $) {'üstünde' if healthy else 'altında'}, "
                  f"en fazla %{int(max_inv*100)} yatırım." if sma200 else "Piyasa rejimi hesaplanamadı.")
        leaders = ", ".join(f"{r['ticker']} ({trn(r['score'])})" for r in eligible[:3])
        summary = (", ".join(parts) + ". " + "; ".join(others) + ". " + regime +
                   (f" Momentum sıralamasında ilk üç: {leaders}." if leaders else ""))
        buys = [a for a in self.actions if "alındı" in a]
        sells = [a for a in self.actions if "satıldı" in a]
        headline = (f"{len(buys)} alım, {len(sells)} satış" if (buys or sells) else "İşlem yok: portföy sıralamayla uyumlu")
        if buys or sells:
            headline += ": " + "; ".join(sells + buys)
        uniq, seen = [], set()
        for s in sources:
            if s["url"] not in seen:
                seen.add(s["url"])
                uniq.append({**s, "publisher": "Yahoo Finance" if "yahoo" in s["url"] else "GitHub",
                             "usedFor": "Fiyat ve geçmiş veriler" if "yahoo" in s["url"] else "Veri kütüphanesi"})
        self.L["runs"].append({
            "date": self.today, "at": iso(self.now), "by": "bot", "headline": headline, "marketSummary": summary,
            "indicators": [{"label": i["label"], "value": i["value"], "change": i["change"]} for i in inds],
            "decisions": decisions, "rejected": rejected, "watchlist": watch, "sources": uniq,
            "ranking": ranking[:15],
            "nextSteps": "Zarar-kes emirleri, kâr alma ve çöküş freni piyasa açıkken 5 dakikada bir kontrol "
                         "ediliyor. Bir sonraki sıralama yarın 10:00'dan sonraki ilk çalışmada.",
        })

    def quotes_for(self, tickers):
        missing = [t for t in tickers if t not in self.quotes]
        if missing:
            self.load_quotes(missing)

    # ---------------- run
    def run(self):
        held = [p["ticker"] for p in self.P["positions"]]
        self.load_quotes(held + ["SPY"])
        if "SPY" not in self.quotes:
            self.status = "uyari"
            self.notes.append("SPY verisi alınamadı; bu çalışmada işlem yapılmadı")
            self.log_check("guard")
            return
        self.guard()
        self.revalue()
        kind = "guard"
        if (self.now.time() >= DAILY_AFTER and self.M.get("lastDailyDate") != self.today
                and not self.quotes["SPY"]["stale"]):
            self.daily()
            kind = "daily"
        self.revalue()
        if self.status == "normal" and any(
                p["lastPrice"] / p["stopPrice"] - 1 < 0.03 for p in self.P["positions"] if p.get("stopPrice")):
            self.status = "uyari"
            self.notes.append("Bir pozisyon zarar-kes seviyesine %3'ten yakın")
        self.log_check(kind)
        self.snapshot()

    def log_check(self, kind):
        spy = self.quotes.get("SPY", {})
        entry = {
            "at": iso(self.now), "kind": kind, "status": self.status,
            "spyPrice": round(spy["price"], 2) if spy else None,
            "spyChangePct": round(spy["dayChangePct"], 2) if spy else None,
            "totalValue": self.P["totalValue"], "benchmarkValue": self.P.get("benchmarkValue"),
            "positions": [{
                "ticker": p["ticker"], "price": p["lastPrice"],
                "dayChangePct": round(self.quotes[p["ticker"]]["dayChangePct"], 2) if p["ticker"] in self.quotes else None,
                "stopPrice": p["stopPrice"], "distancePct": round((p["lastPrice"] / p["stopPrice"] - 1) * 100, 2),
            } for p in self.P["positions"]],
            "actions": self.actions,
        }
        if self.notes:
            entry["note"] = "; ".join(self.notes)
        elif not self.actions:
            dist = [e["distancePct"] for e in entry["positions"]]
            entry["note"] = ("Normal: " + (f"pozisyonlar zarar-kese %{trn(min(dist),1)}–%{trn(max(dist),1)} uzakta, " if dist else "")
                             + (f"SPY {trpct(spy['dayChangePct'])}; " if spy else "") + "işlem gerekmedi.")
        checks = self.L.setdefault("checks", {})
        checks.setdefault(self.today, []).append(entry)
        for d in sorted(checks)[:-CHECK_DAYS_KEPT]:
            del checks[d]
        self.M["lastRunAt"] = iso(self.now)

    def snapshot(self):
        snaps = [s for s in self.L["snapshots"] if s["date"] != self.today]
        snaps.append({"date": self.today, "at": iso(self.now), "totalValue": self.P["totalValue"],
                      "cash": self.P["cash"], "positionsValue": self.P["positionsValue"], "plPct": self.P["totalPLPct"],
                      "benchmarkPrice": self.P.get("benchmarkPrice"), "benchmarkValue": self.P.get("benchmarkValue"),
                      "holdings": [{"ticker": p["ticker"], "value": p["marketValue"]} for p in self.P["positions"]]})
        snaps.sort(key=lambda s: s["date"])
        self.L["snapshots"] = snaps


def load_ledger(path=LEDGER):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_ledger(ledger, path=LEDGER):
    Path(path).write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    now = datetime.fromisoformat(os.environ["BOT_NOW"]).astimezone(NY) if os.environ.get("BOT_NOW") else datetime.now(NY)
    if not market_open(now) and os.environ.get("BOT_FORCE") != "1":
        print(f"{now:%Y-%m-%d %H:%M} ET: piyasa kapalı, çıkılıyor.")
        return 0
    import data
    ledger = load_ledger()
    eng = Engine(ledger, data, now)
    try:
        eng.run()
        ledger["meta"]["lastError"] = None
    except Exception as e:
        traceback.print_exc()
        ledger = load_ledger()  # discard partial changes
        ledger.setdefault("meta", {})["lastError"] = f"{iso(now)}: {type(e).__name__}: {e}"
        ledger["meta"]["lastRunAt"] = iso(now)
    save_ledger(ledger)
    if eng.new_trades:
        Path(".traded").write_text("1")
    print(f"{now:%H:%M} ET | durum={eng.status} | toplam={ledger['portfolio']['totalValue']} | işlemler={eng.actions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
