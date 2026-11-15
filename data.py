"""Market data from Yahoo Finance through the free `yfinance` library.

Every function returns plain pandas objects indexed by timezone-aware timestamps
(America/New_York). The engine never calls yfinance directly, so tests can swap
this module for a fake one.
"""
import time

import pandas as pd

NY = "America/New_York"


def _download(tickers, **kw):
    import yfinance as yf

    last_err = None
    for attempt in range(4):
        try:
            df = yf.download(tickers=list(tickers), group_by="ticker", auto_adjust=False,
                             progress=False, threads=True, **kw)
            if df is not None and not df.empty:
                return df
        except Exception as e:  # rate limits, network hiccups
            last_err = e
        time.sleep(5 * (attempt + 1))
    if last_err:
        print("download failed:", last_err)
    return pd.DataFrame()


def _split(df, tickers, is_daily):
    out = {}
    if df.empty:
        return out
    multi = isinstance(df.columns, pd.MultiIndex)
    for t in tickers:
        try:
            sub = df[t] if multi else df
        except KeyError:
            continue
        sub = sub.dropna(subset=["Close"])
        if sub.empty:
            continue
        idx = pd.DatetimeIndex(sub.index)
        if idx.tz is None:
            # daily bars come as exchange-local dates; intraday bars as UTC
            idx = idx.tz_localize(NY if is_daily else "UTC")
        sub = sub.copy()
        sub.index = idx.tz_convert(NY)
        out[t] = sub[["Open", "High", "Low", "Close"]].astype(float)
    return out


def daily(tickers, period="1y"):
    """Daily OHLC bars. During the session the last row is today's partial bar."""
    tickers = sorted(set(tickers))
    return _split(_download(tickers, period=period, interval="1d"), tickers, True)


def intraday(tickers):
    """Today's 5-minute bars (regular session only)."""
    tickers = sorted(set(tickers))
    return _split(_download(tickers, period="1d", interval="5m", prepost=False), tickers, False)


def dividends(tickers):
    """Dividend history per ticker (ex-date indexed Series); empty dict on failure."""
    import yfinance as yf

    out = {}
    for t in sorted(set(tickers)):
        try:
            s = yf.Ticker(t).dividends
            if s is not None and len(s):
                idx = pd.DatetimeIndex(s.index)
                if idx.tz is None:
                    idx = idx.tz_localize(NY)
                out[t] = pd.Series(s.values, index=idx.tz_convert(NY))
        except Exception as e:  # dividends are best effort
            print("dividends failed:", t, e)
    return out


def extended(tickers):
    """5-minute bars of the last few days including pre-market (04:00-09:30) and after-hours (16:00-20:00)."""
    tickers = sorted(set(tickers))
    return _split(_download(tickers, period="5d", interval="5m", prepost=True), tickers, False)


def screen_movers(min_pct=4.0, min_price=5.0, size=120):
    """Today's biggest movers among all US-listed stocks (Nasdaq, NYSE, NYSE American) from Yahoo's screener:
    one request for the risers and one for the fallers. Returns a list of dicts
    {ticker, name, price, pct, dvM (3-month average volume x price, M$), capB, ex, volume}."""
    import yfinance as yf
    from yfinance import EquityQuery as Q

    ex_code = {"NMS": "Q", "NGM": "Q", "NCM": "Q", "NYQ": "N", "ASE": "A"}
    base = [Q("eq", ["region", "us"]), Q("is-in", ["exchange"] + list(ex_code)),
            Q("gte", ["intradayprice", min_price]), Q("gt", ["avgdailyvol3m", 200000])]
    out = {}
    for up in (True, False):
        q = Q("and", base + [Q("gt", ["percentchange", min_pct]) if up else Q("lt", ["percentchange", -min_pct])])
        res = None
        for attempt in range(3):
            try:
                res = yf.screen(q, size=size, sortField="percentchange", sortAsc=not up)
                break
            except Exception as e:  # rate limits, network hiccups
                print("screen failed:", e)
                time.sleep(4 * (attempt + 1))
        for x in (res or {}).get("quotes") or []:
            t = x.get("symbol")
            price, pct = x.get("regularMarketPrice"), x.get("regularMarketChangePercent")
            if not t or price is None or pct is None or x.get("quoteType") not in (None, "EQUITY"):
                continue
            vol3m, cap = x.get("averageDailyVolume3Month"), x.get("marketCap")
            out[t] = {"ticker": t, "name": x.get("shortName") or x.get("longName") or t, "price": float(price),
                      "pct": float(pct), "dvM": round(vol3m * price / 1e6, 1) if vol3m else None,
                      "capB": round(cap / 1e9, 2) if cap else None, "ex": ex_code.get(x.get("exchange")),
                      "volume": x.get("regularMarketVolume")}
    return list(out.values())
