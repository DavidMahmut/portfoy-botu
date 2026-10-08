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
