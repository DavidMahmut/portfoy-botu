"""Daily and weekly evaluation of the x10 account (v7.1, from 24 Nov 2026), used by trader.Engine.

  shadow book   every stock the opportunity scan looks at is written down with its price at the scan, the setup it
                fitted and whether the bot traded it; every morning the closes of the following days are filled in.
                This shows what each kind of move really did afterwards, traded or not ("kaçırılan fırsatlar").
  rule health   each opportunity rule is judged every morning on its last signals (shadow, so skipped ones count
                too): a rule whose recent signals lose money is paused, and switched back on when they recover.
                The thresholds come from intraday_study.py (kill-switch test), not from feeling.
  reviews       per trading day: account change, the market, profit or loss of every rule, trades, what the scan's
                risers and fallers did. Per week: the same summed up, plus the rule decisions of the week.

Everything is written to review.json (the page reads it); the engine's own state stays in ledger.json.
"""
import json
import math
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REVIEW = ROOT / "review.json"
SHADOW_DAYS = 12                     # trading days of scan observations kept
REVIEW_DAYS, REVIEW_WEEKS = 30, 12
HORIZONS = (0, 1, 2, 3, 4)           # trading days after the scan day whose close is filled in
SHADOW_COLS = ["d", "s", "t", "n", "p", "mv", "gap", "h1", "pos", "cap", "dv", "k", "side", "dec", "play", "why", "o"]
COST = 0.15                          # % round trip used when judging shadow signals
HEALTH_N, HEALTH_MIN = 20, 10        # judge on the last 20 signals, need at least 10 with a known outcome
KEY_TR = {"drift": "Haber devamı adayı", "gapTurn": "Açılış boşluğu tersine döndü", "gapSmall": "Boşluk sürüyor (küçük şirket)",
          "news": "Haber takibi adayı", "pinned": "Satın alma haberi", "up": "Diğer yükselenler", "down": "Diğer düşenler"}


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def tstat(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 3:
        return None
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    return m / (sd / math.sqrt(len(xs))) if sd > 0 else None


def r2(x, d=2):
    return None if x is None else round(x, d)


def tnum(x, d=2):
    s = f"{abs(x):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("−" if x < 0 else "") + s


def tpct(x, d=2):
    return ("+" if x > 0 else "−" if x < 0 else "") + "%" + tnum(abs(x), d)


def tusd(x):
    return ("+" if x > 0 else "−" if x < 0 else "") + tnum(abs(x)) + " $"


def week_key(d):
    y, w, _ = date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


class ReviewMixin:
    """Mixed into trader.Engine; uses self.L, self.M, self.P, self.data, self.today, self.now, self.quotes."""

    # ------------------------------------------------------------------ shadow book
    def shadow_add(self, scan_rows, slot_time, feats):
        """Write the stocks of one scan into the shadow book (one entry per stock and scan)."""
        R = self.R
        cols = SHADOW_COLS
        rows = R.setdefault("shadow", {"cols": cols, "rows": []})["rows"]
        byt = {f["ticker"]: f for f in feats}
        seen = {(x[0], x[2], x[11]) for x in rows if x[0] == self.today}
        for r in scan_rows:
            f = byt.get(r["t"], {})
            k = r.get("setup") or ("up" if (r.get("mv") or 0) > 0 else "down")
            if (self.today, r["t"], k) in seen:      # one entry per stock, day and setup: its first scan
                continue
            seen.add((self.today, r["t"], k))
            side = r.get("side") or ("long" if k == "drift" else None)
            side = 1 if side in ("long", 1) else -1 if side in ("short", -1) else None
            rows.append([self.today, slot_time, r["t"], (r.get("n") or "")[:40], r.get("p"), r.get("mv"), r.get("gap"),
                         r.get("h1"), r2(f.get("pos"), 2), r.get("cap"), r.get("dv"), k, side, r.get("dec"),
                         r.get("play"), (r.get("why") or "")[:90], {}])
        days = sorted({x[0] for x in rows})
        keep = set(days[-SHADOW_DAYS:])
        R["shadow"]["rows"] = [x for x in rows if x[0] in keep]

    def shadow_fill(self):
        """Once a day: fill in the closes of the days after each observation (long return in %, from the scan price)."""
        rows = (self.R.get("shadow") or {}).get("rows") or []
        need = sorted({x[2] for x in rows if x[0] < self.today and len(x[16]) < len(HORIZONS)})
        if not need:
            return 0
        hist = self.data.daily(need, period="1mo")
        filled = 0
        for x in rows:
            if x[0] >= self.today or len(x[16]) >= len(HORIZONS):
                continue
            h = hist.get(x[2])
            if h is None or not len(h) or not x[4]:
                continue
            ds = [d.isoformat() for d in h.index.date]
            if x[0] not in ds:
                continue
            i = ds.index(x[0])
            closes = h["Close"].values
            for k in HORIZONS:
                j = i + k
                if str(k) in x[16] or j >= len(ds) or ds[j] >= self.today:
                    continue
                x[16][str(k)] = round((float(closes[j]) / x[4] - 1) * 100, 2)
                filled += 1
        return filled

    def _shadow(self):
        sh = self.R.get("shadow") or {}
        return [dict(zip(sh.get("cols") or SHADOW_COLS, x)) for x in sh.get("rows") or []]

    @staticmethod
    def _signal_ret(x, hold):
        """Return of a shadow observation traded in its rule's direction until the close `hold` days later, after costs."""
        o = (x.get("o") or {}).get(str(hold))
        if o is None or not x.get("side"):
            return None
        return x["side"] * o - COST

    # ------------------------------------------------------------------ rule health (kill switch)
    def play_health(self, gate_on=True):
        """Every morning: pause a rule whose last HEALTH_N signals lost money on average, switch it back on when they
        earn again. Signals = every stock that fitted the rule at a scan, traded or not (the shadow book)."""
        from trader import PLAYS
        H = self.M.setdefault("health", {})
        log = self.M.setdefault("healthLog", [])
        sh = self._shadow()
        texts = []
        rules = {k: (k, P_.get("hold", 2)) for k, P_ in PLAYS.items() if P_.get("enabled", True)}
        for play, (key, hold) in rules.items():
            sig = [x for x in sh if x["k"] == key]
            sig.sort(key=lambda x: (x["d"], x["s"]))
            rets = [self._signal_ret(x, hold) for x in sig]
            rets = [r for r in rets if r is not None][-HEALTH_N:]
            live = [t for t in self.L["trades"] if t.get("play") == play and t.get("action") in ("SELL", "COVER")
                    and not t.get("cancelled")]
            st = H.get(play) or {"status": "açık", "since": self.today}
            m, tt = mean(rets), tstat(rets)
            st.update(n=len(rets), avg=r2(m), t=r2(tt), hold=hold,
                      live={"n": len(live), "wins": sum(1 for t in live if (t.get("realizedPL") or 0) > 0),
                            "pl": round(sum(t.get("realizedPL") or 0 for t in live), 2)})
            old = st["status"]
            if gate_on and len(rets) >= HEALTH_MIN and m is not None:
                if old == "açık" and m < 0:
                    st.update(status="durduruldu", since=self.today,
                              reason=f"son {len(rets)} sinyalin ortalaması %{m:.2f} (masraf sonrası): kural kaybettiriyor")
                elif old == "durduruldu" and m > 0:
                    st.update(status="açık", since=self.today,
                              reason=f"son {len(rets)} sinyalin ortalaması yeniden pozitif (%{m:.2f})")
            if old != st["status"]:
                log.append({"date": self.today, "play": play, "to": st["status"], "reason": st.get("reason")})
                texts.append(f"Kural sağlığı: {self._play_label(play)} {st['status'].upper()} ({st.get('reason')}).")
            H[play] = st
        self.M["healthLog"] = log[-60:]
        return texts

    def play_paused(self, play):
        return ((self.M.get("health") or {}).get(play) or {}).get("status") == "durduruldu"

    def _play_label(self, play):
        from trader import PLAYS
        return {"index": "Endeks kolu", "spike": "Sert yükseliş short'u", "other": "Diğer", "fees": "Masraf ve diğer"}.get(
            play, (PLAYS.get(play) or {}).get("label", play))

    # ------------------------------------------------------------------ reviews
    def save_marks(self):
        """Unrealized profit of every open position at this run, kept per day (the last run of a day = its close)."""
        from trader import INDEX_SLEEVE
        marks = self.R.setdefault("marks", {})
        marks[self.today] = {f"{p['ticker']}|{p['side']}": [p.get("unrealized"),
                                                           p.get("play") or ("index" if p["ticker"] in INDEX_SLEEVE else "other")]
                             for p in self.P["positions"]}
        for d in sorted(marks)[:-12]:
            del marks[d]

    def _holdings(self, snap):
        """[(unrealized, play)] of a day's closing positions: from the snapshot, or from the saved marks."""
        hs = snap.get("holdings") or []
        if all(h.get("u") is not None for h in hs):
            return [(h["u"], h.get("play") or "other") for h in hs], True
        mk = (self.R.get("marks") or {}).get(snap["date"])
        if mk is not None and len(mk) == len(hs):
            return [tuple(v) for v in mk.values()], True
        return [(h.get("u"), h.get("play") or "other") for h in hs if h.get("u") is not None], False

    def _snap(self, d):
        return next((s for s in self.L.get("snapshots", []) if s["date"] == d), None)

    def _prev_snap(self, d):
        prev = [s for s in self.L.get("snapshots", []) if s["date"] < d]
        return prev[-1] if prev else None

    def _keys(self):
        from trader import PLAYS
        out = dict(KEY_TR)
        for k, P_ in PLAYS.items():
            out.setdefault(k, P_["label"] + " adayı")
        return out

    def _day_review(self, d):
        s = self._snap(d)
        if not s:
            return None
        p = self._prev_snap(d)
        start_cap = self.C.get("startCapital") or 3000.0
        fresh = self.C.get("startDate") == d
        v0 = start_cap if (fresh or not p) else p["totalValue"]
        v1 = s["totalValue"]
        trades = [t for t in self.L["trades"] if t.get("date") == d and not t.get("cancelled")]
        plays = {}
        exact = True
        for t in trades:
            if t.get("action") in ("SELL", "COVER"):
                k = t.get("play") or "other"
                plays[k] = plays.get(k, 0.0) + (t.get("realizedPL") or 0.0)
        hs, ok = self._holdings(s)
        exact = exact and ok
        for u, k in hs:
            plays[k] = plays.get(k, 0.0) + (u or 0.0)
        if p and not fresh:
            hs, ok = self._holdings(p)
            exact = exact and ok
            for u, k in hs:
                plays[k] = plays.get(k, 0.0) - (u or 0.0)
        total = v1 - v0
        plays = {k: round(v, 2) for k, v in plays.items() if abs(v) >= 0.005}
        plays["fees"] = round(total - sum(plays.values()), 2)
        spy0 = None if fresh or not p else p.get("benchmarkPrice")
        spy1 = s.get("benchmarkPrice")
        q0 = None if fresh or not p else p.get("qqqPrice")
        q1 = s.get("qqqPrice")
        sh = [x for x in self._shadow() if x["d"] == d]
        KT = self._keys()
        groups = {}
        for x in sh:
            g = groups.setdefault(x["k"], {"n": 0, "taken": 0, "o0": [], "o1": [], "o2": []})
            g["n"] += 1
            g["taken"] += 1 if x["dec"] == "al" else 0
            for k in ("0", "1", "2"):
                g["o" + k].append((x.get("o") or {}).get(k))
        gl = []
        for k, g in groups.items():
            gl.append({"k": k, "label": KT.get(k, k), "n": g["n"], "taken": g["taken"],
                       "toClose": r2(mean(g["o0"])), "next": r2(mean(g["o1"])), "two": r2(mean(g["o2"]))})
        gl.sort(key=lambda g: -g["n"])
        first = {}
        for x in sh:                       # one line per stock: its first appearance that day
            first.setdefault(x["t"], x)
        cand = [x for x in first.values() if (x.get("o") or {}).get("1") is not None]
        best = sorted(cand, key=lambda x: -abs(x["o"]["1"]))[:8]
        missed = [{"t": x["t"], "n": x["n"], "s": x["s"], "mv": x["mv"], "k": x["k"], "label": KT.get(x["k"], x["k"]),
                   "dec": x["dec"], "why": x["why"], "o0": x["o"].get("0"), "o1": x["o"].get("1"), "o2": x["o"].get("2")}
                  for x in best]
        rv = {"date": d, "start": round(v0, 2), "end": round(v1, 2), "pl": round(total, 2),
              "plPct": round(total / v0 * 100, 2) if v0 else None, "exact": exact,
              "spyPct": round((spy1 / spy0 - 1) * 100, 2) if spy0 and spy1 else None,
              "qqqPct": round((q1 / q0 - 1) * 100, 2) if q0 and q1 else None,
              "leverage": s.get("leverage"), "plays": plays,
              "trades": [{"ts": t.get("ts", "")[11:16], "a": t["action"], "t": t["ticker"], "rule": t.get("rule"),
                          "v": round(t.get("value") or 0, 2), "pl": t.get("realizedPL")} for t in trades],
              "scan": {"seen": len(first), "groups": gl, "missed": missed}}
        rv["text"] = self._day_text(rv)
        return rv

    def _day_text(self, rv):
        parts = []
        mk = []
        if rv.get("spyPct") is not None:
            mk.append(f"S&P 500 {tpct(rv['spyPct'])}")
        if rv.get("qqqPct") is not None:
            mk.append(f"Nasdaq 100 {tpct(rv['qqqPct'])}")
        parts.append(f"Hesap {tusd(rv['pl'])} ({tpct(rv['plPct'] or 0)})" + (f"; {', '.join(mk)}." if mk else "."))
        pl = {k: v for k, v in rv["plays"].items() if k != "fees"}
        if pl:
            w = max(pl, key=pl.get)
            l = min(pl, key=pl.get)
            if pl[w] > 0:
                parts.append(f"En çok kazandıran: {self._play_label(w)} ({tusd(pl[w])}).")
            if pl[l] < 0:
                parts.append(f"En çok kaybettiren: {self._play_label(l)} ({tusd(pl[l])}).")
        for key, word in (("up", "yükselenler"), ("down", "düşenler")):
            g = next((g for g in rv["scan"]["groups"] if g["k"] == key), None)
            if g and g.get("next") is not None:
                parts.append(f"Taramada görülen {word} ertesi gün ortalama {tpct(g['next'])}.")
        return " ".join(parts)

    def _week_review(self, wk, days):
        if not days:
            return None
        d0, d1 = days[0], days[-1]
        v0, v1 = d0["start"], d1["end"]
        flows = sum(1 for d in days if self.C.get("startDate") == d["date"])
        plays = {}
        for d in days:
            for k, v in d["plays"].items():
                plays[k] = round(plays.get(k, 0.0) + v, 2)
        trades = [t for d in days for t in d["trades"]]
        closes = [t for t in trades if t["a"] in ("SELL", "COVER") and t.get("pl") is not None]
        spy = [d["spyPct"] for d in days if d.get("spyPct") is not None]
        qqq = [d["qqqPct"] for d in days if d.get("qqqPct") is not None]
        comp = lambda xs: round((math.prod(1 + x / 100 for x in xs) - 1) * 100, 2) if xs else None
        acct = comp([d["plPct"] for d in days if d.get("plPct") is not None])
        # what each kind of move did over the week (shadow, one line per stock and day)
        sh = [x for x in self._shadow() if week_key(x["d"]) == wk]
        first = {}
        for x in sh:
            first.setdefault((x["d"], x["t"]), x)
        groups = {}
        for x in first.values():
            g = groups.setdefault(x["k"], {"n": 0, "o1": [], "o2": []})
            g["n"] += 1
            g["o1"].append((x.get("o") or {}).get("1"))
            g["o2"].append((x.get("o") or {}).get("2"))
        KT = self._keys()
        gl = sorted([{"k": k, "label": KT.get(k, k), "n": g["n"], "next": r2(mean(g["o1"])), "two": r2(mean(g["o2"]))}
                     for k, g in groups.items()], key=lambda g: -g["n"])
        decisions = [h for h in self.M.get("healthLog", []) if week_key(h["date"]) == wk]
        best = max(days, key=lambda d: d["pl"])
        worst = min(days, key=lambda d: d["pl"])
        rv = {"week": wk, "from": d0["date"], "to": d1["date"], "days": len(days), "start": v0, "end": v1,
              "pl": round(v1 - v0, 2) if not flows else round(sum(d["pl"] for d in days), 2), "plPct": acct,
              "spyPct": comp(spy), "qqqPct": comp(qqq), "plays": plays, "trades": len(trades), "closed": len(closes),
              "wins": sum(1 for t in closes if t["pl"] > 0), "bestDay": {"date": best["date"], "pl": best["pl"]},
              "worstDay": {"date": worst["date"], "pl": worst["pl"]}, "scan": gl, "decisions": decisions}
        pl = {k: v for k, v in plays.items() if k != "fees"}
        txt = [f"Hafta: hesap {tusd(rv['pl'])} ({tpct(acct or 0)})"
               + (f", S&P 500 {tpct(rv['spyPct'])}" if rv["spyPct"] is not None else "")
               + (f", Nasdaq 100 {tpct(rv['qqqPct'])}" if rv["qqqPct"] is not None else "") + "."]
        if pl:
            w, l = max(pl, key=pl.get), min(pl, key=pl.get)
            if pl[w] > 0:
                txt.append(f"Kazandıran: {self._play_label(w)} ({tusd(pl[w])}).")
            if pl[l] < 0:
                txt.append(f"Kaybettiren: {self._play_label(l)} ({tusd(pl[l])}).")
        if decisions:
            txt.append("Kural kararları: " + "; ".join(f"{self._play_label(h['play'])} {h['to']}" for h in decisions) + ".")
        rv["text"] = " ".join(txt)
        return rv

    def build_reviews(self):
        snaps = [s["date"] for s in self.L.get("snapshots", []) if s["date"] >= (self.C.get("startDate") or "")]
        days = []
        old = {x["date"]: x for x in self.R.get("days") or [] if x.get("exact")}
        for d in snaps[-REVIEW_DAYS:]:
            try:
                rv = self._day_review(d)
            except Exception as e:                # a review must never stop the bot
                rv = {"date": d, "error": f"{type(e).__name__}: {e}"}
            if rv and not rv.get("exact") and d in old:
                keep = dict(old[d])
                keep["scan"] = rv.get("scan") or keep.get("scan")   # the shadow part keeps filling in
                rv = keep
            if rv:
                days.append(rv)
        good = [d for d in days if "error" not in d]
        weeks = {}
        for d in good:
            weeks.setdefault(week_key(d["date"]), []).append(d)
        wl = []
        for wk in sorted(weeks)[-REVIEW_WEEKS:]:
            try:
                w = self._week_review(wk, weeks[wk])
            except Exception as e:
                w = {"week": wk, "error": f"{type(e).__name__}: {e}"}
            if w:
                wl.append(w)
        self.R.update(asOf=self.now.isoformat(timespec="minutes"), days=days, weeks=wl,
                      health=self.M.get("health") or {}, healthLog=self.M.get("healthLog") or [],
                      keys=self._keys(), healthRule={"n": HEALTH_N, "min": HEALTH_MIN, "cost": COST})


def load_review(path=REVIEW):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_review(R, path=REVIEW):
    Path(path).write_text(json.dumps(R, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
