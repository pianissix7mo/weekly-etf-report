"""No-network tests for fail-closed preflight and bounded retries."""
import pytest

from etf_report import readiness
from etf_report.errors import ReportIncompleteError


def test_preflight_checks_session_dates_for_both_exchanges(monkeypatch):
    monkeypatch.setattr(readiness, "expected_completed_session_date", lambda s: "2026-11-20")
    monkeypatch.setattr(readiness.yf, "Ticker", lambda symbol, **kwargs: symbol)
    seen = []

    def quote(ticker, symbol):
        seen.append(symbol)
        return (100, "2026-11-20") if symbol != "TECH.TO" else (None, "2026-11-19")

    monkeypatch.setattr(readiness, "fetch_dated_close", quote)
    issues = readiness.check_quote_freshness(("QQQ", "NVDA", "TECH.TO"))
    assert seen == ["QQQ", "NVDA", "TECH.TO"]
    assert len(issues) == 1
    assert "TECH.TO: expected=2026-11-20, observed=2026-11-19" in issues[0]


def test_preflight_waits_for_provider_to_catch_up(monkeypatch):
    cycles = iter([["QQQ stale"], ["QQQ stale"], []])
    monkeypatch.setattr(readiness, "check_quote_freshness", lambda symbols: next(cycles))
    monkeypatch.setattr(readiness.YAHOO_SESSION, "open_reason", None)
    slept = []
    monkeypatch.setattr(readiness, "sleep", lambda seconds: slept.append(seconds))
    readiness.wait_for_market_data(attempts=3, delay_seconds=11, symbols=("QQQ",))
    assert slept == [11, 11]


def test_preflight_fails_without_full_report_or_email_when_closes_stale(monkeypatch):
    monkeypatch.setattr(
        readiness, "check_quote_freshness",
        lambda symbols: ["QQQ: expected=2026-11-20, observed=2026-11-19"],
    )
    monkeypatch.setattr(readiness.YAHOO_SESSION, "open_reason", None)
    slept = []
    monkeypatch.setattr(readiness, "sleep", lambda seconds: slept.append(seconds))
    with pytest.raises(ReportIncompleteError, match="MARKET_DATA_NOT_READY"):
        readiness.wait_for_market_data(attempts=3, delay_seconds=0, symbols=("QQQ",))
    assert slept == [0, 0]


def test_preflight_does_not_retry_after_yahoo_denial(monkeypatch):
    monkeypatch.setattr(readiness, "check_quote_freshness", lambda symbols: ["QQQ unavailable"])
    monkeypatch.setattr(readiness.YAHOO_SESSION, "open_reason", "Yahoo rate-limit circuit open")
    monkeypatch.setattr(
        readiness, "sleep", lambda *_: pytest.fail("do not retry a provider denial"),
    )
    try:
        with pytest.raises(ReportIncompleteError, match="MARKET_DATA_NOT_READY"):
            readiness.wait_for_market_data(attempts=6, symbols=("QQQ",))
    finally:
        monkeypatch.setattr(readiness.YAHOO_SESSION, "open_reason", None)


def test_preflight_rejects_invalid_retry_parameters():
    with pytest.raises(ValueError):
        readiness.wait_for_market_data(attempts=0)
    with pytest.raises(ValueError):
        readiness.wait_for_market_data(delay_seconds=-1)
