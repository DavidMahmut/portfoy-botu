"""Daily 3 long + 3 short (v8, from 27 Nov 2026): the owner's rule, used by trader.Engine when STRATEGY == "v8".

  1. Before the open (08:30-09:29 New York, from the half-hourly watch job; otherwise at the bot's first run) the bot
     ranks the large US stocks with fixed rules: the opening gap (pre-market price against yesterday's close), the
     trend (200 / 50-day averages) and the last 20 days' momentum, then reads the headlines of the leaders
     (catalyst.py: takeovers are dropped, the news is shown as the reason).
  2. At the open (09:35) it buys the best 3 long candidates and sells short the best 3 short candidates. Together
     they are 3x the account: each position is half the account.
  3. A position is closed as soon as it has made 15 % on its own money (= a 5 % move of the stock at 3x). Everything
     else is closed at 15:55 (12:55 on early-close days). No stop-loss: the owner's choice.
Which ranking is used comes from daytrade_study.py (tested on the last two years before use).
No AI and no paid data: Yahoo Finance through yfinance, headlines from Yahoo.
"""
import json
import math
from datetime import date, datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PICKS = ROOT / "picks.json"
MOVERS = ROOT / "movers.json"
DAY_N = 3                                   # longs and shorts each
DAY_LEV = 3.0                               # total exposure, x the account
DAY_TP = 0.05                               # a 5 % move = +15 % on a 3x position
RESEARCH_FROM, OPEN = dtime(8, 30), dtime(9, 30)
DAY_ENTRY_AFTER, DAY_ENTRY_BY = dtime(9, 31), dtime(10, 30)   # first run after the open; nothing after 10:30
DAY_EXIT = dtime(15, 55)
EARLY_CLOSE = {"2026-11-27": dtime(13, 0), "2026-12-24": dtime(13, 0), "2027-11-26": dtime(13, 0),
               "2027-12-23": dtime(13, 0)}
MIN_CAP, MIN_DV, MIN_PRICE = 2.0, 50.0, 10.0
SSR = 0.10
# daytrade_study.py (1.320 large stocks, Oct 2024 - Nov 2026, 520 days, 6 long x 6 short rankings): following the gap
# lost (long gap-ups -0,35 %, short gap-downs -0,44 % a position a day); going against it earned: long the biggest gap-
# downs that are still above their 200-day line +0,19 %, short the biggest gap-ups +0,27 %. Together at 3x: +0,69 % a
# day, 59 % of days up, both halves positive, worst fall -57 %. Best of 36 pairs, so partly luck.
LONG_RULE, SHORT_RULE = "gapDownTrend", "gapUpFade"
TOP = 10                                    # candidates kept per side (the rest of the list is the bench)
NEWS_READ = 6                               # headlines read for the leaders of each side
LABELS = {
    "long": {"gapUp": "En büyük açılış boşluğu (yukarı)", "gapUpTrend": "Yukarı boşluk + yükselen trend (200 günlüğün üstü)",
             "gapDownTrend": "Aşağı boşluk + yükselen trend (düşüşte al)", "mom": "Son 20 günün en güçlüleri",
             "momGap": "Son 20 günün güçlüleri + yukarı boşluk", "gapUpMid": "Yukarı boşluk %2–8 + yükselen trend"},
    "short": {"gapDown": "En büyük açılış boşluğu (aşağı)", "gapDownTrend": "Aşağı boşluk + düşen trend (200 günlüğün altı)",
              "gapUpFade": "En büyük yukarı boşluk (sönmesine)", "gapUpWeak": "Yukarı boşluk + düşen trend (zayıf hissenin sıçraması)",
              "mom": "Son 20 günün en zayıfları", "momGap": "Son 20 günün zayıfları + aşağı boşluk"},
}


def exit_time(day):
    early = EARLY_CLOSE.get(day)
    return dtime(early.hour - 1, 55) if early else DAY_EXIT


def pct(x, d=2):
    s = f"{abs(x) * 100:.{d}f}".replace(".", ",")
    return ("+" if x > 0 else "−" if x < 0 else "") + "%" + s


def universe(sanctioned=()):
    """Large, liquid US stocks from the hourly market scan (movers.json): market value >= 2 B$, >= 50 M$ traded a
    day, price >= 10 $, not on the US sanctions list."""
    mv = json.loads(MOVERS.read_text(encoding="utf-8"))
    cols = mv["screen"]["cols"]
    out = []
    for r in mv["screen"]["rows"]:
        x = dict(zip(cols, r))
        if (x.get("cap") or 0) < MIN_CAP or (x.get("dv") or 0) < MIN_DV or (x.get("p") or 0) < MIN_PRICE:
            continue
        if any(n.lower() in (x.get("n") or "").lower() for n in sanctioned):
            continue
        out.append(x)
    return out


def _rank_series(vals, reverse=False):
    order = sorted(range(len(vals)), key=lambda i: vals[i], reverse=reverse)
    r = [0.0] * len(vals)
    for k, i in enumerate(order):
        r[i] = (k + 1) / len(vals)
    return r


def ranked(rows, rule, side):
    """Rows (dicts with gap, mom, a200, a50) ordered by the rule, best first; the same rankings as daytrade_study.py."""
    rows = [r for r in rows if r.get("gap") is not None]
    if side == "short":
        rows = [r for r in rows if r["gap"] > -SSR]          # SEC Rule 201: no new short 10 % below yesterday's close
    if rule in ("momGap",):
        ok = [r for r in rows if (r["gap"] > 0 if side == "long" else r["gap"] < 0) and r.get("mom") is not None]
        if not ok:
            return []
        sgn = 1 if side == "long" else -1
        rm = _rank_series([sgn * r["mom"] for r in ok])
        rg = _rank_series([sgn * r["gap"] for r in ok])
        for r, a, b in zip(ok, rm, rg):
            r["score"] = a + b
        return sorted(ok, key=lambda r: -r["score"])
    key = {
        ("long", "gapUp"): lambda r: r["gap"],
        ("long", "gapUpTrend"): lambda r: r["gap"] if r["a200"] else None,
        ("long", "gapDownTrend"): lambda r: -r["gap"] if r["a200"] else None,
        ("long", "mom"): lambda r: r.get("mom"),
        ("long", "gapUpMid"): lambda r: r["gap"] if r["a200"] and r["a50"] and r["gap"] <= 0.08 else None,
        ("short", "gapDown"): lambda r: -r["gap"],
        ("short", "gapDownTrend"): lambda r: -r["gap"] if not r["a200"] else None,
        ("short", "gapUpFade"): lambda r: r["gap"],
        ("short", "gapUpWeak"): lambda r: r["gap"] if not r["a200"] else None,
        ("short", "mom"): lambda r: -r["mom"] if r.get("mom") is not None else None,
    }[(side, rule)]
    out = []
    for r in rows:
        s = key(r)
        if s is not None:
            out.append({**r, "score": s})
    return sorted(out, key=lambda r: -r["score"])


def research(data, now, sanctioned=(), news_fn=None, classify=None, long_rule=None, short_rule=None):
    """Rank the universe and pick DAY_N longs and DAY_N shorts. Works before the open (pre-market prices) and after
    it (today's opening prices). Returns the picks dict that the engine stores and the page shows."""
    long_rule, short_rule = long_rule or LONG_RULE, short_rule or SHORT_RULE
    uni = universe(sanctioned)
    ts = [u["t"] for u in uni]
    pm = data.premarket(ts) if ts else {}
    hist = data.daily(ts, period="1y") if ts else {}
    today = now.date()
    rows = []
    for u in uni:
        q, h = pm.get(u["t"]), hist.get(u["t"])
        if not q or h is None or not len(h) or q.get("day") != today.isoformat():
            continue
        past = h[h.index.date < today]["Close"].astype(float)
        if len(past) < 200:
            continue
        pc = float(q["prevClose"])
        ref = q["open"] if (q.get("open") and now.time() >= OPEN) else q["price"]
        if pc <= 0 or not ref:
            continue
        closes = [float(x) for x in past.values]
        rows.append({"t": u["t"], "n": u.get("n") or u["t"], "ex": u.get("ex"), "cap": u.get("cap"), "dv": u.get("dv"),
                     "prevClose": round(pc, 4), "ref": round(float(ref), 4), "gap": float(ref) / pc - 1,
                     "mom": float(pc / closes[-21] - 1) if len(closes) > 21 else None,
                     "a200": bool(pc > sum(closes[-200:]) / 200), "a50": bool(pc > sum(closes[-50:]) / 50),
                     "pmVolume": float(q["pmVolume"]) if q.get("pmVolume") is not None else None})
    src = "açılış fiyatları" if now.time() >= OPEN else "seans öncesi fiyatlar"
    picks = {"date": today.isoformat(), "at": now.isoformat(timespec="minutes"), "source": src, "universe": len(uni),
             "priced": len(rows), "rules": {"long": long_rule, "short": short_rule},
             "ruleLabels": {"long": LABELS["long"].get(long_rule), "short": LABELS["short"].get(short_rule)}}
    for side, rule in (("long", long_rule), ("short", short_rule)):
        lst = ranked([dict(r) for r in rows], rule, side)[:TOP + 4]
        keep = []
        for i, r in enumerate(lst):
            r["headline"], r["cat"], r["kind"] = None, None, None
            if news_fn and classify and i < NEWS_READ + 2:
                try:
                    c = classify(r["t"], r["n"], news_fn(r["t"]), r["gap"] * 100, now)
                    if c and c.get("cat"):
                        r["cat"], r["kind"] = c.get("label"), c.get("kind")
                        r["headline"] = c.get("headline")
                except Exception:
                    pass
            if r["kind"] == "pinned":            # a takeover target only moves to the offer price
                continue
            r["why"] = (f"{'Açılış' if src == 'açılış fiyatları' else 'Seans öncesi'} boşluk {pct(r['gap'])}"
                        + (f", son 20 gün {pct(r['mom'], 1)}" if r.get("mom") is not None else "")
                        + (", 200 günlük ortalamanın " + ("üstünde" if r["a200"] else "altında"))
                        + (f"; haber: {r['cat']}" if r.get("cat") else ""))
            keep.append({k: (round(v, 5) if isinstance(v, float) else v) for k, v in r.items()})
            if len(keep) >= TOP:
                break
        picks[side] = keep
    return picks


class DayMixin:
    """Mixed into trader.Engine (v8)."""

    def day_load_picks(self):
        """Picks written before the open by the watch job (picks.json), if they are today's."""
        if (self.M.get("picks") or {}).get("date") == self.today:
            return True
        try:
            p = json.loads(PICKS.read_text(encoding="utf-8"))
        except Exception:
            return False
        if p.get("date") == self.today and p.get("long") is not None:
            self.M["picks"] = p
            return True
        return False

    def day_research(self):
        import catalyst as CAT
        from trader import SANCTIONED_NAMES
        from zoneinfo import ZoneInfo
        now_utc = self.now.astimezone(ZoneInfo("UTC"))
        self.M["picks"] = research(self.data, self.now, SANCTIONED_NAMES, self._news_items,
                                   lambda t, n, items, mv, _now: CAT.classify(t, n, items, mv, now_utc))
        return self.M["picks"]

    def day_positions(self):
        return [p for p in self.P["positions"] if p.get("sleeve") == "day"]

    def day_entry(self):
        """Open the day's 3 + 3 (each half the account) at the first run after 09:35."""
        from trader import usd, trn, MIN_TRADE
        P_ = self.M.get("picks") or {}
        eq = self.P["totalValue"]
        each = eq * DAY_LEV / (2 * DAY_N)
        held = {p["ticker"] for p in self.P["positions"]}
        lines, opened = [], {"long": [], "short": []}
        for side in ("long", "short"):
            cands = P_.get(side) or []
            self.quotes_for([c["t"] for c in cands[:DAY_N + 4]])
            for rank, c in enumerate(cands):
                if len(opened[side]) >= DAY_N:
                    break
                t = c["t"]
                q = self.quotes.get(t)
                if t in held or not q or q["stale"]:
                    c["skip"] = "güncel fiyat yok" if not q or (q and q["stale"]) else "zaten portföyde"
                    continue
                if side == "short" and q["ssr"]:
                    c["skip"] = "SEC Rule 201: önceki kapanışa göre %10 düştü, açığa satılamaz"
                    continue
                amount, _ = self._allowed_mv(t, side, each)
                if amount < MIN_TRADE:
                    c["skip"] = "teminat sınırı"
                    continue
                self.names[t] = c.get("n") or t
                tp_word = "yükselirse" if side == "long" else "düşerse"
                reason = (f"Günlük 3+3 ({'long' if side == 'long' else 'short'} #{len(opened[side]) + 1}): {t} ({c.get('n')}). "
                          f"Seçim kuralı: {P_.get('ruleLabels', {}).get(side)}. {c.get('why')}. Büyüklük hesabın "
                          f"{trn(amount / eq, 2)} katı ({usd(amount)}); toplam 3x. Hisse girişten %{trn(DAY_TP * 100, 0)} "
                          f"{tp_word} pozisyon %{trn(DAY_TP * 100 * DAY_LEV, 0)} kârla kapanır; olmazsa "
                          f"{exit_time(self.today):%H:%M}'te kapanışta kapanır. Zarar-kes yok.")
                tr = (self.open_long(t, amount, q["price"], q["at"], reason, "günlük 3+3") if side == "long" else
                      self.open_short(t, amount, q["price"], q["at"], reason, "günlük 3+3"))
                if not tr:
                    c["skip"] = "emir gerçekleşmedi"
                    continue
                pos = self._pos(t, side)
                ent = tr["execPrice"]
                tp = round(ent * (1 + DAY_TP), 4) if side == "long" else round(ent * (1 - DAY_TP), 4)
                hl = c.get("headline")
                if hl and hl.get("url"):
                    tr["sources"].insert(0, {"title": hl["title"], "url": hl["url"]})
                pos.update(sleeve="day", play="dayLong" if side == "long" else "dayShort",
                           playLabel="Günlük long" if side == "long" else "Günlük short", stopMode="day", stopPrice=None,
                           tpPrice=tp, exitDate=self.today, name=c.get("n"), rank=rank + 1, pick=c, borrowRate=0.0)
                c["taken"] = True
                held.add(t)
                opened[side].append(t)
                lines.append(f"{'LONG' if side == 'long' else 'SHORT'} {t} {usd(amount)}, hedef {trn(tp)} $ ({c.get('why')}).")
        self.M["dayEntered"] = self.today
        return lines, opened

    def day_tp(self, p, q):
        """Take-profit check on the 5-minute bars since the last check (no stop)."""
        b = q["bars"]
        if p.get("openedTs"):
            b = b[b.index > __import__("pandas").Timestamp(p["openedTs"])]
        if p.get("lastBarTs"):
            b = b[b.index >= __import__("pandas").Timestamp(p["lastBarTs"])]
        tp = p.get("tpPrice")
        hit = None
        for ts, row in b.iterrows():
            if p["side"] == "long" and row["High"] >= tp:
                hit = (ts, max(tp, float(row["Open"])) if row["Open"] >= tp else tp)
                break
            if p["side"] == "short" and row["Low"] <= tp:
                hit = (ts, min(tp, float(row["Open"])) if row["Open"] <= tp else tp)
                break
        if len(b):
            p["lastBarTs"] = b.index[-1].isoformat()
        if hit:
            from trader import trn
            ts, px = hit
            self.close(p, p["shares"], px, ts,
                       f"Kâr hedefi: {p['ticker']} girişten %{trn(DAY_TP * 100, 0)} {'yükseldi' if p['side'] == 'long' else 'düştü'} "
                       f"({trn(p['avgCost'])} $ → {trn(px)} $); pozisyon %{trn(DAY_TP * DAY_LEV * 100, 0)} kârla kapatıldı.",
                       "günlük 3+3 kâr hedefi")
            return True
        return False

    def day_close_all(self, why):
        n = 0
        for p in list(self.day_positions()):
            q = self.quotes.get(p["ticker"])
            if not q:
                self.quotes_for([p["ticker"]])
                q = self.quotes.get(p["ticker"])
            if not q:
                self.notes.append(f"{p['ticker']}: kapanış için fiyat alınamadı")
                continue
            self.close(p, p["shares"], q["price"], q["at"], why, "günlük 3+3 gün sonu")
            n += 1
        return n
