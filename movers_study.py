"""Research only (never trades): after a Nasdaq stock moves 20 % or more in a day, does it tend to reverse?

Runs on GitHub Actions (.github/workflows/movers_study.yml) with Yahoo Finance daily bars since mid-2018 for today's
Nasdaq-listed common stocks (same filters as movers.py: price >= 3 $, 20-day average dollar volume >= 2 million $).
Writes movers_study.json. Survivorship bias: companies that were delisted or went bankrupt are missing, which makes
buying after big falls look better than it really was.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

import movers as MV

ROOT = Path(__file__).resolve().parent
HORIZONS = (1, 2, 5, 10, 20)
COST = 0.003            # round trip for smaller, more volatile stocks (spread + commission)


def events(data):
    out = []
    for t, s in data.items():
        c = s["Close"].astype(float).values
        o = s["Open"].astype(float).values
        v = s["Volume"].astype(float).values
        idx = s.index
        n = len(c)
        dv = np.convolve(np.nan_to_num(c * v), np.ones(20) / 20, mode="full")[:n]   # mean of the 20 days ending at i
        for i in range(21, n - 1):
            prev = c[i - 1]
            if prev < MV.MIN_PRICE or not np.isfinite(prev) or dv[i - 1] < MV.MIN_DOLLAR_VOL:
                continue
            mv = c[i] / prev - 1
            if not np.isfinite(mv) or abs(mv) < MV.ALERT:
                continue
            e = {"t": t, "d": str(idx[i].date()), "move": float(mv), "dvM": float(dv[i - 1] / 1e6), "price": float(c[i])}
            for h in HORIZONS:
                if i + h < n:
                    e[f"r{h}"] = float(c[i + h] / c[i] - 1)
                    e[f"o{h}"] = float(c[i + h] / o[i + 1] - 1) if o[i + 1] > 0 else None
            e["gapNext"] = float(o[i + 1] / c[i] - 1) if o[i + 1] > 0 else None
            out.append(e)
    return out


def stats(ev, h, side_fn, key="r"):
    vals = [side_fn(e) * e[f"{key}{h}"] - COST for e in ev if e.get(f"{key}{h}") is not None]
    raw = [e[f"r{h}"] for e in ev if e.get(f"r{h}") is not None]
    if not vals:
        return {"n": 0}
    a, r = np.array(vals), np.array(raw)
    return {"n": int(len(a)), "rawMeanPct": round(float(r.mean() * 100), 2), "rawMedianPct": round(float(np.median(r) * 100), 2),
            "tradeMeanPct": round(float(a.mean() * 100), 2), "tradeMedianPct": round(float(np.median(a) * 100), 2),
            "winRatePct": round(float((a > 0).mean() * 100), 1)}


def main():
    names = MV.nasdaq_symbols()
    data = MV.download(sorted(names), start="2018-06-01", interval="1d")
    ev = events(data)
    ev = [e for e in ev if e["d"] >= "2019-01-01"]
    mid = "2023-01-01"
    res = {}
    for label, sel in (("up", [e for e in ev if e["move"] > 0]), ("down", [e for e in ev if e["move"] < 0])):
        fade = (lambda e: -1) if label == "up" else (lambda e: 1)
        res[label] = {"count": len(sel), "medianMovePct": round(float(np.median([e["move"] for e in sel]) * 100), 1) if sel else None,
                      "nextGapMedianPct": round(float(np.median([e["gapNext"] for e in sel if e["gapNext"] is not None]) * 100), 2) if sel else None,
                      "fadeFromClose": {f"{h}d": stats(sel, h, fade) for h in HORIZONS},
                      "fadeFromNextOpen": {f"{h}d": stats(sel, h, fade, "o") for h in HORIZONS},
                      "fade5dFirstHalf": stats([e for e in sel if e["d"] < mid], 5, fade),
                      "fade5dSecondHalf": stats([e for e in sel if e["d"] >= mid], 5, fade),
                      "fade5dLiquid": stats([e for e in sel if e["dvM"] >= 20], 5, fade),
                      "fade5dThin": stats([e for e in sel if e["dvM"] < 20], 5, fade),
                      "byYear": {y: stats([e for e in sel if e["d"][:4] == y], 5, fade) for y in sorted({e["d"][:4] for e in sel})}}
    up5, dn5 = res["up"]["fadeFromClose"]["5d"], res["down"]["fadeFromClose"]["5d"]
    text = (f"Günde %20+ yükselen {res['up']['count']} durumda hisse sonraki 5 günde ortalama %{up5.get('rawMeanPct')} "
            f"(medyan %{up5.get('rawMedianPct')}) hareket etti; %20+ düşen {res['down']['count']} durumda ortalama "
            f"%{dn5.get('rawMeanPct')} (medyan %{dn5.get('rawMedianPct')}). Ters yöne 5 günlük işlem (maliyet sonrası): "
            f"yükselene short %{up5.get('tradeMeanPct')} ortalama, kazanma %{up5.get('winRatePct')}; düşene long "
            f"%{dn5.get('tradeMeanPct')} ortalama, kazanma %{dn5.get('winRatePct')}. Batan şirketler veride olmadığı için "
            "düşüş sonrası toparlanma olduğundan iyi görünür.")
    out = {"asOf": datetime.utcnow().isoformat(timespec="seconds") + "Z", "tickers": len(data), "events": len(ev),
           "from": min((e["d"] for e in ev), default=None), "to": max((e["d"] for e in ev), default=None),
           "results": res, "summary": {"text": text, "up5d": up5, "down5d": dn5},
           "notes": "Günlük kapanışlar; bugün Nasdaq'ta işlem gören hisseler (hayatta kalma yanılgısı var). Filtre: önceki gün "
                    "fiyatı ≥ 3 $, 20 günlük ortalama işlem hacmi ≥ 2 milyon $. Ters işlem = yükselene short, düşene long; "
                    "gidiş-dönüş maliyet %0,3; short ödünç ücreti dahil değil (küçük hisselerde yüksek olabilir)."}
    (ROOT / "movers_study.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
