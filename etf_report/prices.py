"""Exchange-dated, unadjusted daily closing prices for ETF valuation.

Never accept yfinance fast_info.last_price without a dated trading session.
"""
from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup
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
            match = re.search(r"[-+]?(?:\\d{1,3}(?:,\\d{3})*|\\d+)\\.\\d+", close_text)
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
    links = [
        ("Yahoo Finance HTML", f"https://finance.yahoo.com/quote/{symbol}/history/"),
        ("StockAnalysis historical HTML",
         f"https://stockanalysis.com/etf/{symbol.lower()}/history/" if symbol in
         {"QQQ", "SPMO", "OEF", "MAGS", "CHAT", "DRAM", "SOXX", "SMH", "XLK"}
         else f"https://stockanalysis.com/stocks/{symbol.lower()}/history/"),
    ]
    for source, url in links:
        try:
            response = requests.get(url, headers=headers, timeout=12)
            response.raise_for_status()
            if "StockAnalysis" in source and (
                "Market open" in response.text or "market open" in response.text.lower()
            ) and "at close" not in response.text.lower():
                continue
            price, date = _parse_history_html_close(response.text, required)
            if price is not None and date == required:
                print(f"VERIFIED BACKUP CLOSE {symbol} {date} {price} via {source}")
                return price, date
        except Exception:
            pass
    return None, None

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

    if required is not None and not any(date >= required for date, _ in choices):
        backup_price, backup_date = fetch_public_dated_close(symbol, required)
        if backup_price is not None and backup_date == required:
            return backup_price, backup_date

    if not choices:
        return None, None
    session, price = max(choices, key=lambda x: x[0])
    return price, session
