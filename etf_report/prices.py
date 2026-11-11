"""Exchange-dated, unadjusted daily closing prices for ETF valuation.

Never accept yfinance fast_info.last_price without a dated trading session.
"""
from __future__ import annotations

import re

import pandas as pd
import yfinance as yf


def is_us_quote(symbol):
    return bool(re.fullmatch(r"[A-Z]{1,6}(?:-[A-Z])?", str(symbol).upper()))


def expected_us_close_date(now=None):
    """Last completed US weekday session (fail closed on market holidays).

    NYSE holidays during a weekday intentionally produce no fresh quote and
    therefore no publication, rather than accepting yesterday's data.
    """
    moment = pd.Timestamp.now(tz="America/New_York") if now is None else pd.Timestamp(now)
    if moment.tzinfo is None:
        moment = moment.tz_localize("America/New_York")
    else:
        moment = moment.tz_convert("America/New_York")
    cutoff_reached = (moment.hour, moment.minute) >= (16, 5)
    if moment.weekday() < 5 and cutoff_reached:
        return moment.strftime("%Y-%m-%d")
    previous = moment.normalize() - pd.offsets.BDay(1)
    return previous.strftime("%Y-%m-%d")


def parse_dated_close(history):
    """Read the last non-null unadjusted Close and its exchange-local date."""
    if history is None or history.empty:
        return None, None
    if isinstance(history.columns, pd.MultiIndex):
        columns = [c for c in history.columns if str(c[0]).lower() == "close"]
        if not columns:
            return None, None
        close = history[columns[0]]
    else:
        if "Close" not in history.columns:
            return None, None
        close = history["Close"]
    close = pd.to_numeric(close, errors="coerce").dropna()
    close = close[close > 0]
    if close.empty:
        return None, None
    session = pd.Timestamp(close.index[-1]).strftime("%Y-%m-%d")
    return float(close.iloc[-1]), session


def fetch_dated_close(ticker, symbol, now=None):
    """Use dated daily candles; retry via download when Yahoo returns a stale bar.

    Returns (price, session_date). A stale result is returned with its actual date,
    so downstream quality gates can refuse the workbook.
    """
    required = expected_us_close_date(now) if is_us_quote(symbol) else None
    choices = []
    try:
        hist = ticker.history(period="5d", interval="1d", auto_adjust=False, actions=False)
        price, session = parse_dated_close(hist)
        if price is not None:
            choices.append((session, price))
            if required is None or session >= required:
                return price, session
    except Exception:
        pass

    try:
        hist = yf.download(
            symbol, period="5d", interval="1d", auto_adjust=False,
            progress=False, threads=False,
        )
        price, session = parse_dated_close(hist)
        if price is not None:
            choices.append((session, price))
    except Exception:
        pass

    if not choices:
        return None, None
    session, price = max(choices, key=lambda x: x[0])
    return price, session
