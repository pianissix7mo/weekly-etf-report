"""Fail-fast, no-cost market-data readiness check before the expensive ETF build.

A complete daily report requires dated closes from the latest *completed*
exchange session. Do not silently relabel yesterday's quotes as today's.
"""
from __future__ import annotations

import os
from time import sleep

import yfinance as yf

from etf_report.errors import ReportIncompleteError
from etf_report.prices import expected_completed_session_date, fetch_dated_close
from etf_report.yahoo import YAHOO_SESSION

# Highly liquid US symbols plus a Toronto-listed ETF check both exchanges.
PROBE_SYMBOLS = ("QQQ", "NVDA", "AAPL", "TECH.TO")


def check_quote_freshness(symbols=PROBE_SYMBOLS):
    """Return human-readable issues for quotes missing a verified dated close."""
    issues = []
    for symbol in symbols:
        expected = expected_completed_session_date(symbol)
        if not expected:
            issues.append(f"{symbol}: no known completed exchange session")
            continue
        try:
            ticker = yf.Ticker(symbol, session=YAHOO_SESSION)
            close, observed = fetch_dated_close(ticker, symbol)
        except Exception as error:
            issues.append(f"{symbol}: quote request {type(error).__name__}")
            continue
        if close is None or close <= 0 or observed != expected:
            issues.append(f"{symbol}: expected={expected}, observed={observed}, verified_close={'yes' if close else 'no'}")
        else:
            print(f"PREFLIGHT OK {symbol}: verified session={observed}", flush=True)
    return issues


def wait_for_market_data(attempts=6, delay_seconds=180, symbols=PROBE_SYMBOLS):
    """Retry only small dated-close probes; never retry the entire ETF build."""
    if attempts < 1 or delay_seconds < 0:
        raise ValueError("attempts must be >=1 and delay_seconds >=0")
    for attempt in range(1, attempts + 1):
        print(f"Market-data freshness preflight {attempt}/{attempts}", flush=True)
        issues = check_quote_freshness(symbols)
        if not issues:
            print("PREFLIGHT READY: latest completed sessions verified; proceeding with full report.", flush=True)
            return
        print("PREFLIGHT NOT READY:\n - " + "\n - ".join(issues), flush=True)
        if YAHOO_SESSION.open_reason:
            print(f"Yahoo access circuit open: {YAHOO_SESSION.open_reason}; not retrying a provider denial.", flush=True)
            break
        if attempt < attempts:
            print(f"Checking dated closes again in {delay_seconds} seconds.", flush=True)
            sleep(delay_seconds)
    raise ReportIncompleteError(
        "MARKET_DATA_NOT_READY: Yahoo or the configured licensed backup has not "
        "published verified closes for the required completed sessions. "
        "Skipping ETF enrichment, Excel export and email; stale prices are never substituted."
    )


def main():
    attempts = int(os.getenv("ETF_PREFLIGHT_ATTEMPTS", "6"))
    delay_seconds = int(os.getenv("ETF_PREFLIGHT_DELAY_SECONDS", "180"))
    wait_for_market_data(attempts=attempts, delay_seconds=delay_seconds)


if __name__ == "__main__":
    main()
