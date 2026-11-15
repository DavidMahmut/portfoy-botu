"""Weekly agenda and market strip for the top of the dashboard (reading only, never trades).

  markets   what moves the market, with the latest price and the day's and week's change: indices, VIX, 10-year
            yield, dollar, oil (WTI and Brent), natural gas, gold, copper, Bitcoin, euro and lira,
  econ      this week's US economic releases (inflation, jobs, Fed, growth ...) with the expected, previous and - once
            published - actual values,
  earnings  the largest companies reporting this week,
  ipos      US listings expected or priced this week,
  special   market holidays, early closes and options-expiration days.

Data: Yahoo Finance through yfinance (prices: daily bars; calendars: yfinance.Calendars). Everything is best effort:
a part that fails keeps its previous content and the reason is written to `errors`. Runs from
.github/workflows/watch.yml every 30 minutes on trading days and once on Sunday evening. Output: agenda.json.
"""
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"

# (ticker, label, group, unit, decimals, why it matters)
MARKETS = [
    ("^GSPC", "S&P 500", "Endeksler", "", 0, "ABD'nin en büyük 500 şirketi; piyasanın genel yönü."),
    ("^NDX", "Nasdaq 100", "Endeksler", "", 0, "Teknoloji ağırlıklı 100 büyük şirket; botun endeks kolu bunu izler (QQQ)."),
    ("^DJI", "Dow Jones", "Endeksler", "", 0, "30 büyük sanayi ve finans şirketi."),
    ("^RUT", "Russell 2000", "Endeksler", "", 0, "Küçük şirketler; faiz ve ekonomi beklentisine en duyarlı grup."),
    ("^VIX", "VIX (korku endeksi)", "Risk", "", 2, "Beklenen oynaklık: 15 altı sakin, 20 üstü gergin, 30 üstü panik."),
    ("^TNX", "10 yıllık ABD faizi", "Faiz ve dolar", "%", 2, "Yükselince büyüme hisseleri baskılanır, ipotek ve kredi pahalanır."),
    ("DX-Y.NYB", "Dolar endeksi (DXY)", "Faiz ve dolar", "", 2, "Güçlü dolar, emtiayı ve ABD şirketlerinin yurt dışı gelirini baskılar."),
    ("CL=F", "Petrol (WTI)", "Emtia", "$", 2, "ABD ham petrolü: enflasyonu, enerji ve havayolu hisselerini etkiler."),
    ("BZ=F", "Petrol (Brent)", "Emtia", "$", 2, "Dünya petrol fiyatının ölçüsü."),
    ("NG=F", "Doğal gaz", "Emtia", "$", 3, "Enerji maliyeti; kış aylarında sert oynar."),
    ("GC=F", "Altın (ons)", "Emtia", "$", 0, "Güvenli liman: korku ve faiz indirimi beklentisiyle yükselir."),
    ("HG=F", "Bakır", "Emtia", "$", 3, "Sanayi talebinin göstergesi: yükselmesi büyüme beklentisine işaret eder."),
    ("BTC-USD", "Bitcoin", "Kripto ve kur", "$", 0, "Risk iştahının göstergesi; kripto hisselerini (COIN, MSTR) sürükler."),
    ("EURUSD=X", "Euro / dolar", "Kripto ve kur", "", 4, "Doların avroya karşı değeri."),
    ("USDTRY=X", "Dolar / TL", "Kripto ve kur", "₺", 2, "Doların Türk lirası karşılığı."),
]

# US economic releases: (pattern on Yahoo's release name, Turkish label, importance 3 = market-moving, 2 = watched)
ECON = [
    (r"fed funds|fomc.*(rate|decision|statement)|interest rate decision|fed interest", "Fed faiz kararı", 3),
    (r"\bcore cpi\b|cpi.*ex.*food", "Çekirdek enflasyon (TÜFE)", 3),
    (r"\bcpi\b|consumer price", "Enflasyon (TÜFE)", 3),
    (r"\bcore pce\b", "Çekirdek PCE enflasyonu (Fed'in izlediği ölçü)", 3),
    (r"\bpce\b", "PCE enflasyonu", 3),
    (r"non[- ]?farm payroll|nonfarm payroll", "Tarım dışı istihdam", 3),
    (r"unemployment rate", "İşsizlik oranı", 3),
    (r"\bgdp\b|gross domestic", "Büyüme (GSYH)", 3),
    (r"fomc minutes|fed minutes|meeting minutes", "Fed toplantı tutanakları", 3),
    (r"\bppi\b|producer price", "Üretici fiyatları (ÜFE)", 2),
    (r"retail sales", "Perakende satışlar", 3),
    (r"ism.*(manuf|mfg)", "ISM imalat endeksi", 2),
    (r"ism.*(serv|non-?m)", "ISM hizmet endeksi", 2),
    (r"jobless cl|initial cl|continuing cl|cont jobless", "Haftalık işsizlik başvuruları", 2),
    (r"jolts|job openings", "Açık iş ilanları (JOLTS)", 2),
    (r"\badp\b", "ADP özel sektör istihdamı", 2),
    (r"u mich|michigan|consumer (confidence|sentiment)|cons(umer)? conf", "Tüketici güveni", 2),
    (r"average (hourly )?earnings|avg (hourly )?earnings", "Ortalama saatlik kazanç", 2),
    (r"durable", "Dayanıklı mal siparişleri", 2),
    (r"housing starts|building permits|build permits", "Konut başlangıçları / inşaat izinleri", 1),
    (r"(existing|new|pending) home", "Konut satışları", 1),
    (r"s&p global.*pmi|flash.*pmi|markit.*pmi|\bpmi\b", "PMI (satın alma yöneticileri endeksi)", 2),
    (r"industrial production|capacity util", "Sanayi üretimi", 1),
    (r"philly fed|philadelphia fed|ny fed|empire state", "Bölgesel Fed imalat endeksi", 1),
    (r"trade balance|goods trade", "Dış ticaret dengesi", 1),
    (r"import prices|export prices", "İthalat / ihracat fiyatları", 1),
    (r"crude oil inventor|eia.*(crude|wkly)|api.*crude", "Ham petrol stokları", 1),
    (r"factory orders", "Fabrika siparişleri", 1),
    (r"personal (income|spending|consump)|consumption, adjusted", "Kişisel gelir ve harcama", 2),
    (r"employment cost|unit labor|productivity", "İşgücü maliyeti / verimlilik", 1),
    (r"treasury budget|federal budget", "Federal bütçe dengesi", 1),
    (r"case[- ]shiller|house price|hpi", "Konut fiyatları", 1),
    (r"business inventories|wholesale inv", "Stoklar", 1),
]
ECON_RX = [(re.compile(rx, re.I), tr, imp) for rx, tr, imp in ECON]
WHY = {
    "Fed faiz kararı": "Faizin yönü hisse değerlemelerini doğrudan etkiler; sürpriz karar sert hareket getirir.",
    "Enflasyon (TÜFE)": "Beklentinin üstünde gelirse faiz indirimi umudu azalır, hisseler (özellikle Nasdaq) düşebilir.",
    "Çekirdek enflasyon (TÜFE)": "Gıda ve enerji hariç enflasyon; Fed'in asıl baktığı eğilim.",
    "Çekirdek PCE enflasyonu (Fed'in izlediği ölçü)": "Fed'in %2 hedefini ölçtüğü gösterge.",
    "Tarım dışı istihdam": "Ayın en önemli verisi: çok güçlü ya da çok zayıf gelmesi faiz beklentisini değiştirir.",
    "İşsizlik oranı": "Yükselmesi ekonominin soğuduğunu gösterir.",
    "Büyüme (GSYH)": "Ekonominin çeyreklik büyüme hızı.",
    "Fed toplantı tutanakları": "Fed üyelerinin faiz konusundaki görüş ayrılıkları buradan okunur.",
    "Perakende satışlar": "Tüketici harcamasının gücü; ABD ekonomisinin üçte ikisi tüketimdir.",
    "Haftalık işsizlik başvuruları": "İş piyasasının en güncel göstergesi; her perşembe 08:30.",
    "Üretici fiyatları (ÜFE)": "Şirketlerin maliyet enflasyonu; TÜFE'nin öncü göstergesi sayılır.",
}
EARN_MIN_CAP, EARN_TOP = 15e9, 30
# Fed (FOMC) decision days: the statement comes at 14:00 New York time, the press conference at 14:30; the minutes
# follow three weeks later at 14:00. Source: federalreserve.gov/monetarypolicy/fomccalendars.htm (2026 schedule).
# Yahoo's calendar does not always carry these, so they are added from this table; extend it when a new year is published.
FOMC_DECISIONS = ("2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09")
MONTHS_TR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def week_of(today):
    """Monday-Friday of the current week; on Saturday and Sunday the coming week."""
    mon = today - timedelta(days=today.weekday())
    if today.weekday() >= 5:
        mon += timedelta(days=7)
    return mon, mon + timedelta(days=4)


def num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def markets():
    import yfinance as yf
    ts = [m[0] for m in MARKETS]
    df = yf.download(ts, period="1mo", interval="1d", group_by="ticker", auto_adjust=False, progress=False, threads=True)
    out = []
    for t, label, group, unit, dec, why in MARKETS:
        try:
            c = df[t]["Close"].dropna()
        except Exception:
            continue
        if len(c) < 2:
            continue
        last, prev = float(c.iloc[-1]), float(c.iloc[-2])
        wk = float(c.iloc[-6]) if len(c) >= 6 else float(c.iloc[0])
        is_yield = unit == "%"
        out.append({"t": t, "label": label, "group": group, "unit": unit, "dec": dec, "why": why, "price": round(last, 4),
                    "prev": round(prev, 4), "date": str(c.index[-1].date()),
                    # a yield moves in points, everything else in per cent
                    "d1": round(last - prev, 3) if is_yield else round((last / prev - 1) * 100, 2),
                    "w1": round(last - wk, 3) if is_yield else round((last / wk - 1) * 100, 2),
                    "chgUnit": "puan" if is_yield else "%",
                    "spark": [round(float(v), 4) for v in c.tail(15)]})
    return out


def _ny(ts):
    try:
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert(NY)
    except Exception:
        return None


def econ_label(name):
    for rx, tr, imp in ECON_RX:
        if rx.search(name):
            return tr, imp
    return None, 0


# the line that stands for a group of sub-series published together (first match wins)
PRIMARY = [r"^retail sales mm", r"initial jobless", r"business indx", r"ny fed manuf|empire", r"sentiment", r"crude stk|crude inventor",
           r"housing starts number", r"building permits: number", r"\bcpi yy", r"\bcpi mm", r"core pce.*yy", r"pce.*yy",
           r"non-?farm payrolls", r"gdp", r"ppi.*yy", r"ppi.*mm", r"industrial production", r"pending homes index",
           r"existing home sales$|new home sales", r"import prices"]
PRIMARY_RX = [re.compile(x, re.I) for x in PRIMARY]


def _unit(name):
    return "%" if re.search(r"\b(MM|YY|QQ)\b|\brate\b|change", name) else ""


def economic(cal, start, end, debug):
    rows, off = [], 0
    while off < 800:
        df = cal.get_economic_events_calendar(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                              limit=100, offset=off, force=True)
        if df is None or df.empty:
            break
        if not debug.get("econCols"):
            debug["econCols"] = [str(c) for c in df.columns]
            debug["econSample"] = json.loads(df.head(4).reset_index().to_json(orient="records", date_format="iso"))
        for name, r in df.iterrows():
            rows.append((str(name), r))
        if len(df) < 100:
            break
        off += 100
    flat, seen = [], set()
    for name, r in rows:
        if str(r.get("Region") or "").upper() not in ("US", "USA", "UNITED STATES"):
            continue
        at = _ny(r.get("Event Time"))
        if at is None or not (start <= at.date() <= end):
            continue
        name = name.replace("*", "").strip()
        key = (name, at.isoformat())
        if key in seen:
            continue
        seen.add(key)
        tr, imp = econ_label(name)
        period = r.get("For")
        flat.append({"at": at.isoformat(timespec="minutes"), "day": at.date().isoformat(), "name": name, "tr": tr, "imp": imp,
                     "period": None if period is None or (isinstance(period, float) and math.isnan(period)) else str(period),
                     "actual": num(r.get("Actual")), "expected": num(r.get("Expected")), "last": num(r.get("Last")),
                     "unit": _unit(name)})
    groups, out = {}, []
    for x in flat:
        if not x["tr"]:
            out.append({**x, "more": []})
            continue
        groups.setdefault((x["at"], x["tr"]), []).append(x)
    for (at, tr), items in groups.items():
        def rank(x):
            for i, rx in enumerate(PRIMARY_RX):
                if rx.search(x["name"]):
                    return i
            return len(PRIMARY_RX)
        items.sort(key=lambda x: (rank(x), x["name"]))
        head = items[0]
        out.append({**head, "why": WHY.get(tr),
                    "more": [{k: y[k] for k in ("name", "actual", "expected", "last", "unit")} for y in items[1:8]]})
    out.sort(key=lambda x: (x["at"], -x["imp"], x["name"]))
    return out


def _us_listed():
    """Tickers listed on Nasdaq, NYSE and NYSE American (from the daily fundamentals file), to drop foreign OTC lines."""
    try:
        return set((json.loads((ROOT / "fundamentals.json").read_text(encoding="utf-8")).get("rows") or {}).keys())
    except Exception:
        return set()


def earnings(cal, start, end, debug):
    df = cal.get_earnings_calendar(market_cap=EARN_MIN_CAP, filter_most_active=False, start=start.isoformat(),
                                   end=(end + timedelta(days=1)).isoformat(), limit=100, force=True)
    if df is None or df.empty:
        return []
    debug["earnCols"] = [str(c) for c in df.columns]
    listed = _us_listed()
    out, seen = [], set()
    for sym, r in df.iterrows():
        sym = str(sym)
        at = _ny(r.get("Event Start Date"))
        if at is None or not (start <= at.date() <= end) or sym in seen:
            continue
        # foreign shares quoted over the counter (five letters ending in F or Y) repeat the US listing
        if (listed and sym not in listed) or (not listed and len(sym) == 5 and sym[-1] in "FY"):
            continue
        seen.add(sym)
        timing = str(r.get("Timing") or "").upper()
        cap = num(r.get("Marketcap"))
        out.append({"t": sym, "name": r.get("Company"), "capB": round(cap / 1e9, 1) if cap else None,
                    "day": at.date().isoformat(),
                    "when": "açılıştan önce" if timing == "BMO" else "kapanıştan sonra" if timing == "AMC" else None,
                    "event": r.get("Event Name"), "epsEst": num(r.get("EPS Estimate")), "epsActual": num(r.get("Reported EPS")),
                    "surprisePct": num(r.get("Surprise(%)"))})
    out.sort(key=lambda x: -(x["capB"] or 0))
    out = out[:EARN_TOP]
    out.sort(key=lambda x: (x["day"], -(x["capB"] or 0)))
    return out


def ipos(cal, start, end, debug):
    df = cal.get_ipo_info_calendar(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), limit=100, force=True)
    if df is None or df.empty:
        return []
    debug["ipoCols"] = [str(c) for c in df.columns]
    out = []
    for sym, r in df.iterrows():
        at = _ny(r.get("Date"))
        ex = str(r.get("Exchange") or "")
        name = str(r.get("Company") or "")
        if at is None or not (start <= at.date() <= end):
            continue
        if not re.search(r"nasdaq|nyse|amex|cboe", ex, re.I):
            continue                       # funds and foreign listings come without a US exchange
        if re.search(r"\bETF\b|\bFunds?\b|\bTrust\b|Warrants?|\bUnits?\b|\bRights?\b|Notes due|Preferred|Depositary", name, re.I):
            continue
        action = str(r.get("Action") or "")
        if re.search(r"withdraw|postpon", action, re.I):
            continue
        lo, hi, px, sh = num(r.get("Price From")), num(r.get("Price To")), num(r.get("Price")), num(r.get("Shares"))
        mid = px or ((lo + hi) / 2 if lo and hi else lo or hi)
        out.append({"t": str(sym) if sym and str(sym) != "nan" else None,
                    "name": re.sub(r"\s+(Class [A-Z] )?(Ordinary Shares|Common Stock|Common Shares)$", "", name).strip(),
                    "ex": ex, "day": at.date().isoformat(),
                    "status": {"expected": "bekleniyor", "priced": "fiyatlandı", "filed": "başvuru yapıldı"}.get(action.lower(), action or None),
                    "spac": bool(re.search(r"acquisition corp|acquisition co\b|blank check", name, re.I)),
                    "priceFrom": lo, "priceTo": hi, "price": px, "shares": sh,
                    "sizeM": round(mid * sh / 1e6, 1) if mid and sh else None})
    out.sort(key=lambda x: (x["day"], x["spac"], -(x["sizeM"] or 0)))
    seen, uniq = set(), []
    for x in out:                          # the same listing can come twice (share classes, repeated rows)
        key = (x["t"] or x["name"], x["day"])
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq[:25]


def special(start, end):
    """Market holidays, early closes and options expirations inside the week."""
    try:
        from trader import HOLIDAYS
    except Exception:
        HOLIDAYS = set()
    early = {"2026-11-27": "Şükran Günü'nün ertesi: borsa 13:00'te kapanır", "2026-12-24": "Noel arifesi: borsa 13:00'te kapanır",
             "2027-11-26": "Şükran Günü'nün ertesi: borsa 13:00'te kapanır"}
    names = {"11-26": "Şükran Günü (Thanksgiving)", "11-25": "Şükran Günü (Thanksgiving)", "12-25": "Noel", "12-24": "Noel (tatil)",
             "01-01": "Yılbaşı", "01-18": "Martin Luther King Günü", "01-19": "Martin Luther King Günü",
             "02-15": "Başkanlar Günü", "02-16": "Başkanlar Günü", "03-26": "Kutsal Cuma", "04-03": "Kutsal Cuma",
             "05-25": "Anma Günü (Memorial Day)", "05-31": "Anma Günü (Memorial Day)", "06-19": "Juneteenth", "06-18": "Juneteenth",
             "07-03": "Bağımsızlık Günü (tatil)", "07-05": "Bağımsızlık Günü (tatil)", "09-07": "İşçi Bayramı (Labor Day)",
             "09-06": "İşçi Bayramı (Labor Day)"}
    out = []
    d = start
    while d <= end:
        s = d.isoformat()
        if s in HOLIDAYS:
            out.append({"day": s, "kind": "holiday", "text": f"Borsa kapalı: {names.get(s[5:], 'resmî tatil')}"})
        elif s in early:
            out.append({"day": s, "kind": "early", "text": early[s]})
        if s in FOMC_DECISIONS:
            out.append({"day": s, "kind": "fed", "time": "14:00", "imp": 3,
                        "text": "Fed faiz kararı (14:00) ve başkanın basın toplantısı (14:30)",
                        "why": WHY["Fed faiz kararı"]})
        if (d - timedelta(days=21)).isoformat() in FOMC_DECISIONS:
            out.append({"day": s, "kind": "fed", "time": "14:00", "imp": 3, "text": "Fed toplantı tutanakları (14:00)",
                        "why": WHY["Fed toplantı tutanakları"]})
        if d.weekday() == 4 and 15 <= d.day <= 21:
            out.append({"day": s, "kind": "opex", "text": ("Üçlü vade günü: endeks vadeli işlemleri ve opsiyonların vadesi birlikte dolar; "
                                                             "kapanışa doğru hacim ve oynaklık artar" if d.month in (3, 6, 9, 12) else
                                                             "Aylık opsiyon vadesi: kapanışa doğru hacim artabilir")})
        d += timedelta(days=1)
    return out


def main():
    now = datetime.now(NY)
    probe = os.environ.get("AGENDA_PROBE") == "1"
    try:
        old = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        old = {}
    start, end = week_of(now.date())
    same_week = (old.get("week") or {}).get("start") == start.isoformat()
    errors, debug = [], {}
    out = {"asOf": now.isoformat(timespec="minutes"), "week": {"start": start.isoformat(), "end": end.isoformat(),
                                                               "label": f"{start.day} {MONTHS_TR[start.month - 1]} – {end.day} {MONTHS_TR[end.month - 1]} {end.year}"},
           "today": now.date().isoformat()}
    try:
        out["markets"] = markets()
        if not out["markets"]:
            raise RuntimeError("fiyat alınamadı")
        out["marketsAt"] = out["asOf"]
    except Exception as e:
        errors.append(f"piyasa şeridi: {type(e).__name__}: {str(e)[:120]}")
        out["markets"], out["marketsAt"] = old.get("markets", []), old.get("marketsAt")
    cal = None
    try:
        import yfinance as yf
        cal = yf.Calendars(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat())
    except Exception as e:
        errors.append(f"takvim: {type(e).__name__}: {str(e)[:120]}")
    for key, fn in (("econ", economic), ("earnings", earnings), ("ipos", ipos)):
        try:
            if cal is None:
                raise RuntimeError("takvim servisi yok")
            out[key] = fn(cal, start, end, debug)
            out[key + "At"] = out["asOf"]
        except Exception as e:
            errors.append(f"{key}: {type(e).__name__}: {str(e)[:160]}")
            out[key] = old.get(key, []) if same_week else []
            out[key + "At"] = old.get(key + "At") if same_week else None
    out["special"] = special(start, end)
    out["errors"] = errors
    out["source"] = {"title": "Yahoo Finance (yfinance): fiyatlar ve takvimler", "url": "https://finance.yahoo.com/calendar/"}
    out["note"] = ("Saatler New York saatidir. Beklenti ve önceki değerler Yahoo Finance takviminden; açıklanan değer veri "
                   "yayımlandıktan sonraki ilk güncellemede görünür. Fiyatlar 30 dakikada bir yenilenir. Yatırım tavsiyesi değildir.")
    # the releases Yahoo already lists are not repeated from the Fed table
    have = {(e["day"], e["tr"]) for e in out.get("econ", [])}
    out["special"] = [x for x in out["special"] if not (x["kind"] == "fed" and (
        (x["day"], "Fed faiz kararı") in have and "kararı" in x["text"] or (x["day"], "Fed toplantı tutanakları") in have and "tutanak" in x["text"]))]
    if probe:
        try:
            import data
            mv = data.screen_movers()
            debug["screener"] = {"n": len(mv), "sample": sorted(mv, key=lambda r: -abs(r["pct"]))[:8]}
        except Exception as e:
            debug["screener"] = f"{type(e).__name__}: {str(e)[:200]}"
        out["debug"] = debug
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"agenda {out['week']['label']}: {len(out['markets'])} markets, {len(out['econ'])} econ, {len(out['earnings'])} earnings, "
          f"{len(out['ipos'])} ipos, {len(out['special'])} special; errors: {errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
