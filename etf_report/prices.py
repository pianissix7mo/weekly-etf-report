"""Exchange-dated, unadjusted daily closing prices for ETF valuation.

Never accept yfinance fast_info.last_price without a dated trading session.
"""
from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlsplit

import exchange_calendars as calendars
import numpy as np

import requests
from bs4 import BeautifulSoup
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



def _parse_history_html_close(html, required):
    """Read a dated Close column from a published historical-price HTML table."""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.select("table"):
        header_row = table.find("thead")
        if header_row is None:
            header_row = table.find("tr")
        if header_row is None:
            continue
        headings = [h.get_text(" ", strip=True).lower() for h in header_row.find_all(["th", "td"])]
        date_col = next((i for i, h in enumerate(headings) if h == "date"), None)
        close_col = next(
            (i for i, h in enumerate(headings) if h == "close" or h.startswith("close ")),
            None,
        )
        if date_col is None or close_col is None:
            continue
        for row in table.select("tbody tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) <= max(date_col, close_col):
                continue
            raw_date = cells[date_col].get_text(" ", strip=True)
            parsed = pd.to_datetime(raw_date, errors="coerce")
            if pd.isna(parsed) or pd.Timestamp(parsed).strftime("%Y-%m-%d") != required:
                continue
            close_text = cells[close_col].get_text(" ", strip=True)
            match = re.search(r"[-+]?(?:\d{1,3}(?:,\d{3})*|\d+)\.\d+", close_text)
            if match:
                value = float(match.group().replace(",", ""))
                if 0 < value < 10_000_000:
                    return value, required
    return None, None


def fetch_public_dated_close(symbol, required):
    """Backup for Yahoo chart lag: published dated tables, never undated quotes.

    Do not use a stockanalysis page still labeled 'market open' as closing data.
    Sources are informational websites and might be temporarily inaccessible.
    """
    symbol = str(symbol).upper()
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/128.0 Safari/537.36",
               "Accept-Language": "en-US,en;q=0.9"}
    # Cross-listed non-US symbols need the actual local-exchange history URL,
    # not a US stock page. StockAnalysis uses AMS:BESI for BESI.AS.
    foreign_paths = {
        ".AS": "ams",
        ".DE": "etr",
        ".PA": "epa",
        ".SW": "swx",
        ".L": "lon",
        ".TO": "tsx",
        ".HK": "hkg",
        ".T": "tyo",
        ".TW": "tpe",
        ".KS": "krx",
    }
    foreign = next(
        ((suffix, exchange) for suffix, exchange in foreign_paths.items()
         if symbol.endswith(suffix)), None
    )
    if foreign:
        suffix, exchange = foreign
        base = symbol[:-len(suffix)]
        analysis_url = f"https://stockanalysis.com/quote/{exchange}/{base}/history/"
    elif symbol in {"QQQ", "SPMO", "OEF", "MAGS", "CHAT", "DRAM", "SOXX", "SMH", "XLK"}:
        analysis_url = f"https://stockanalysis.com/etf/{symbol.lower()}/history/"
    else:
        analysis_url = f"https://stockanalysis.com/stocks/{symbol.lower()}/history/"
    try:
        response = requests.get(analysis_url, headers=headers, timeout=12)
        response.raise_for_status()
        actual = urlsplit(response.url)
        expected = urlsplit(analysis_url)
        if actual.netloc != expected.netloc or actual.path.rstrip("/").lower() != expected.path.rstrip("/").lower():
            return None, None
        if "market open" in response.text.lower() and "at close" not in response.text.lower():
            return None, None
        price, date = _parse_history_html_close(response.text, required)
        if price is not None and date == required:
            print(f"VERIFIED BACKUP CLOSE {symbol} {date} {price} via StockAnalysis historical table")
            return price, date
    except requests.RequestException as error:
        status = error.response.status_code if error.response is not None else type(error).__name__
        print(f"StockAnalysis close {symbol}: {status}")
    return None, None


def resolve_dated_close(symbol, history, required):
    """Return a verified close, the observed session and provenance."""
    price, day = parse_dated_close(history, required=required)
    if price is not None and required is not None:
        return price, day, "Yahoo daily chart"
    _, observed = parse_dated_close(history)
    if required:
        price, day = fetch_public_dated_close(symbol, required)
        if price is not None and day == required:
            return price, day, "StockAnalysis historical table"
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
