import pandas as pd
import pytest

import etf_analyst_report_complete as pipeline
from etf_report import prices
from etf_report.errors import ReportIncompleteError


RUN_DATE = pd.Timestamp("2026-11-10T20:20:00", tz="America/New_York")


def bar(date, close):
    return pd.DataFrame(
        {"Close": [float(close)]},
        index=pd.DatetimeIndex([pd.Timestamp(date, tz="America/New_York")]),
    )


def test_required_us_close_date_respects_completed_session():
    assert prices.expected_us_close_date(RUN_DATE) == "2026-11-10"
    before_close = pd.Timestamp("2026-11-10T15:30:00", tz="America/New_York")
    assert prices.expected_us_close_date(before_close) == "2026-11-09"
    sunday = pd.Timestamp("2026-11-15T22:00:00", tz="America/New_York")
    assert prices.expected_us_close_date(sunday) == "2026-11-13"


def test_dated_daily_candle_beats_stale_fast_info(monkeypatch):
    class Ticker:
        fast_info = {"last_price": 178.67}  # intentionally stale; must never be used
        def history(self, **kwargs):
            assert kwargs["period"] == "5d"
            assert kwargs["auto_adjust"] is False
            return pd.concat([bar("2026-11-09", 178.67), bar("2026-11-10", 168.89)])

    def no_fallback(*args, **kwargs):
        pytest.fail("fresh primary candle must not require download fallback")

    monkeypatch.setattr(prices.yf, "download", no_fallback)
    price, date = prices.fetch_dated_close(Ticker(), "NVDA", now=RUN_DATE)
    assert price == pytest.approx(168.89)
    assert date == "2026-11-10"


def test_stale_primary_candle_retries_second_dated_source(monkeypatch):
    class Ticker:
        def history(self, **kwargs):
            return bar("2026-11-09", 178.67)

    monkeypatch.setattr(prices.yf, "download", lambda *args, **kwargs: bar("2026-11-10", 168.89))
    price, date = prices.fetch_dated_close(Ticker(), "NVDA", now=RUN_DATE)
    assert price == pytest.approx(168.89)
    assert date == "2026-11-10"


def test_stale_nvda_cannot_pass_high_coverage_quality_gate(monkeypatch):
    monkeypatch.setattr(pipeline, "expected_us_close_date", lambda: "2026-11-10")
    df = pd.DataFrame({
        "Yahoo Ticker": ["NVDA", "AAPL"],
        "Weight Decimal": [0.08, 0.92],
        "Current Price": [178.67, 350.0],
        "Price Date": ["2026-11-09", "2026-11-10"],
        "Yahoo Error": [None, None],
    })
    summary = {
        "Covered Weight": 0.98,
        "PE Coverage Weight": 0.98,
        "Forward PE Coverage Weight": 0.98,
    }
    with pytest.raises(ReportIncompleteError, match="stale"):
        pipeline.validate_yahoo_data_quality(df, summary, "QQQ")

    df.loc[0, "Current Price"] = 168.89
    df.loc[0, "Price Date"] = "2026-11-10"
    pipeline.validate_yahoo_data_quality(df, summary, "QQQ")


def test_core_holding_missing_daily_quote_cannot_silently_pass(monkeypatch):
    monkeypatch.setattr(pipeline, "expected_us_close_date", lambda: "2026-11-10")
    df = pd.DataFrame({
        "Yahoo Ticker": ["NVDA", "AAPL"],
        "Weight Decimal": [0.07, 0.93],
        "Current Price": [None, 350.0],
        "Price Date": [None, "2026-11-10"],
    })
    summary = {"Covered Weight": 0.93, "PE Coverage Weight": 0.93, "Forward PE Coverage Weight": 0.93}
    with pytest.raises(ReportIncompleteError, match="major holding"):
        pipeline.validate_yahoo_data_quality(df, summary, "QQQ")


def test_undated_etf_quote_is_not_exported(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_dated_close", lambda *args: (679.35, "2026-11-09"))
    monkeypatch.setattr(pipeline, "expected_us_close_date", lambda: "2026-11-10")
    monkeypatch.setattr(pipeline.yf, "Ticker", lambda *_: object())
    with pytest.raises(ReportIncompleteError, match="ETF quote stale"):
        pipeline.get_etf_current_price("QQQ")


def test_public_html_close_requires_matching_session():
    html = (
        "<table><thead><tr><th>Date</th><th>Open</th><th>High</th>"
        "<th>Low</th><th>Close</th></tr></thead><tbody>"
        "<tr><td>Nov 10, 2026</td><td>170.00</td><td>175.00</td>"
        "<td>165.00</td><td>168.89</td></tr></tbody></table>"
    )
    assert prices._parse_history_html_close(html, "2026-11-10") == (168.89, "2026-11-10")
    assert prices._parse_history_html_close(html, "2026-11-09") == (None, None)


def test_backup_dated_close_used_only_when_yahoo_daily_bar_is_stale(monkeypatch):
    class Ticker:
        def history(self, **kwargs):
            return bar("2026-11-09", 178.67)

    monkeypatch.setattr(prices.yf, "download", lambda *args, **kwargs: bar("2026-11-09", 178.67))
    backup_calls = []

    def fresh_backup(symbol, required):
        backup_calls.append((symbol, required))
        return 168.89, required

    monkeypatch.setattr(prices, "fetch_public_dated_close", fresh_backup)
    value, quoted = prices.fetch_dated_close(Ticker(), "NVDA", now=RUN_DATE)
    assert value == pytest.approx(168.89)
    assert quoted == "2026-11-10"
    assert backup_calls == [("NVDA", "2026-11-10")]


def test_ytd_uses_verified_closing_price_when_adjusted_history_lags(monkeypatch):
    dates = pd.to_datetime(["2025-12-31", "2026-11-09"])
    stale_history = pd.Series([100.0, 105.0], index=dates)
    monkeypatch.setattr(
        pipeline, "_download_adjusted_close",
        lambda *args, **kwargs: stale_history,
    )
    ytd = pipeline.get_ytd_return_from_yahoo(
        "NVDA", verified_price=168.89, verified_quote_date="2026-11-10"
    )
    assert ytd == pytest.approx(0.6889)


def test_ytd_does_not_overwrite_newer_history_with_older_quote(monkeypatch):
    dates = pd.to_datetime(["2025-12-31", "2026-11-10"])
    fresh_history = pd.Series([100.0, 110.0], index=dates)
    monkeypatch.setattr(
        pipeline, "_download_adjusted_close",
        lambda *args, **kwargs: fresh_history,
    )
    ytd = pipeline.get_ytd_return_from_yahoo(
        "NVDA", verified_price=105.0, verified_quote_date="2026-11-09"
    )
    assert ytd == pytest.approx(0.10)


def test_stale_valid_quote_is_not_cached_between_etfs(monkeypatch):
    monkeypatch.setattr(pipeline, "expected_us_close_date", lambda: "2026-11-10")
    stale = pipeline._empty_yahoo_row("NVDA")
    stale.update({
        "Current Price": 178.67,
        "Price Date": "2026-11-09",
        "Target Low": 100,
        "Target Mean": 200,
        "Target High": 300,
    })
    assert not pipeline._yahoo_row_cacheable(stale)
    stale["Price Date"] = "2026-11-10"
    assert pipeline._yahoo_row_cacheable(stale)


def test_real_etf_rejects_missing_price_date_metadata():
    df = pd.DataFrame({
        "Yahoo Ticker": ["NVDA"],
        "Weight Decimal": [1.0],
        "Current Price": [168.89],
    })
    summary = {
        "Covered Weight": 1.0,
        "PE Coverage Weight": 1.0,
        "Forward PE Coverage Weight": 1.0,
    }
    with pytest.raises(ReportIncompleteError, match="mandatory price date"):
        pipeline.validate_yahoo_data_quality(df, summary, "QQQ")


def test_real_etf_rejects_collapsed_analyst_coverage(monkeypatch):
    monkeypatch.setattr(pipeline, "expected_us_close_date", lambda: "2026-11-10")
    df = pd.DataFrame({
        "Yahoo Ticker": ["NVDA", "AAPL"],
        "Weight Decimal": [0.50, 0.50],
        "Current Price": [168.89, 348.51],
        "Price Date": ["2026-11-10", "2026-11-10"],
    })
    summary = {
        "Covered Weight": 0.10,
        "PE Coverage Weight": 1.0,
        "Forward PE Coverage Weight": 1.0,
    }
    with pytest.raises(ReportIncompleteError, match="analyst-target coverage"):
        pipeline.validate_yahoo_data_quality(df, summary, "QQQ")
