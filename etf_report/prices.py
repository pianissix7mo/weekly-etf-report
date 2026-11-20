"""Exchange-dated, unadjusted daily closing prices for ETF valuation.

Never accept yfinance fast_info.last_price without a dated trading session.
"""
from __future__ import annotations

import re
from functools import lru_cache

import exchange_calendars as calendars
import numpy as np

import pandas as pd

from etf_report.backup import FMP


def is_us_quote(symbol):
    return bool(re.fullmatch(r"[A-Z]{1,6}(?:-[A-Z])?", str(symbol).upper()))


# Each exchange calendar includes holidays and special/early closes.
_MARKETS = {
    ".TO": ("XTSE", 5),
    ".TW": ("XTAI", 10),
    ".KS": ("XKRX", 10),
    ".T": ("XTKS", 10),
    ".HK": ("XHKG", 10),
    ".AS": ("XAMS", 10),
    ".DE": ("XETR", 10),
    ".SW": ("XSWX", 10),
    ".PA": ("XPAR", 10),
    ".L": ("XLON", 10),
}


@lru_cache(maxsize=32)
def _calendar(name, year):
    return calendars.get_calendar(name, start=f"{year - 1}-01-01", end=f"{year + 1}-12-31")


@lru_cache(maxsize=128)
def _recent_sessions(name, day):
    end = pd.Timestamp(day)
    return _calendar(name, end.year).schedule.loc[end - pd.Timedelta(days=21):end, "close"]


def expected_completed_session_date(symbol, now=None):
    """Date of the most recent completed session, including publication buffer."""
    name = str(symbol).upper().strip()
    market = next((spec for suffix, spec in _MARKETS.items() if name.endswith(suffix)), None)
    if market is None:
        if not is_us_quote(name) and name != "^VIX":
            return None
        market = ("XNYS", 5)
    calendar_name, buffer_minutes = market
    moment = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    calendar = _calendar(calendar_name, moment.year)
    local = moment.tz_localize(calendar.tz) if moment.tzinfo is None else moment.tz_convert(calendar.tz)
    closes = _recent_sessions(calendar_name, local.date())
    completed = closes[closes <= local - pd.Timedelta(minutes=buffer_minutes)]
    return completed.index[-1].strftime("%Y-%m-%d") if not completed.empty else None


def expected_us_close_date(now=None):
    """Backwards-compatible US session helper."""
    return expected_completed_session_date("NVDA", now=now)


def parse_dated_close(history, required=None):
    """Select the completed session's unadjusted Close, never a partial candle."""
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
    close = close[np.isfinite(close) & (close > 0)].sort_index()
    if required is not None:
        completed_rows = [
            idx for idx in close.index
            if pd.Timestamp(idx).strftime("%Y-%m-%d") == required
        ]
        if not completed_rows:
            return None, None
        if len(completed_rows) != 1:
            return None, None
        return float(close.loc[completed_rows[0]]), required
    if close.empty:
        return None, None
    session = pd.Timestamp(close.index[-1]).strftime("%Y-%m-%d")
    return float(close.iloc[-1]), session



def resolve_dated_close(symbol, history, required):
    """Return a verified close, the observed session and provenance."""
    price, day = parse_dated_close(history, required=required)
    if price is not None and required is not None:
        return price, day, "Yahoo daily chart"
    _, observed = parse_dated_close(history)
    if required:
        price = FMP.close(symbol, required)
        if price is not None:
            return price, required, "FMP unadjusted end-of-day"
    # Preserve the observed date for the stale-quote gate without using the price.
    return None, observed, None


def fetch_dated_close(ticker, symbol, now=None):
    required = expected_completed_session_date(symbol, now=now)
    try:
        history = ticker.history(
            period="5d", interval="1d", auto_adjust=False, actions=False,
            raise_errors=True,
        )
    except Exception as error:
        print(f"Yahoo daily close {symbol}: {type(error).__name__}")
        history = None
    price, date, source = resolve_dated_close(symbol, history, required)
    if source:
        print(f"QUOTE {symbol}: session={date}, source={source}, retrieved={pd.Timestamp.now(tz='UTC').isoformat()}")
    return price, date
