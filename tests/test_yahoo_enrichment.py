import pandas as pd
import pytest

import etf_analyst_report_complete as pipeline
from etf_report.errors import ReportIncompleteError


def _fake_row(symbol):
    row = pipeline._empty_yahoo_row(symbol)
    row.update({
        "Current Price": 100.0,
        "Target Low": 90.0,
        "Target Mean": 110.0,
        "Target High": 120.0,
        "Target Median": 105.0,
        "Trailing PE": 20.0,
        "Forward PE": 18.0,
        "Growth Last Year": 0.10,
    })
    return row


def test_add_yahoo_targets_skips_implausible_symbols_before_network(monkeypatch):
    pipeline.YAHOO_TARGET_CACHE.clear()
    pipeline.YAHOO_FETCH_TIMINGS.clear()
    called = []

    def fake_pull(symbol):
        called.append(symbol)
        return _fake_row(symbol)

    monkeypatch.setattr(pipeline, "pull_yahoo_targets", fake_pull)
    monkeypatch.setattr(pipeline, "YAHOO_MAX_WORKERS", 2)
    monkeypatch.setattr(pipeline, "YAHOO_RETRY_MISSING_GROWTH", False)

    holdings = pd.DataFrame(
        {
            "Yahoo Ticker": ["AAPL", "MSFT", "ARM$CBRS", "IXTZ6", "AGPXX"],
            "Weight": [20.0, 20.0, 20.0, 20.0, 20.0],
        }
    )

    out = pipeline.add_yahoo_targets(holdings)

    assert set(called) == {"AAPL", "MSFT", "IXTZ6"}
    assert len(called) == 3
    skipped = out.set_index("Yahoo Ticker").loc[["ARM$CBRS", "AGPXX"], "Yahoo Error"]
    assert skipped.str.contains("preflight skipped").all()
    assert out.set_index("Yahoo Ticker").loc["AAPL", "Current Price"] == 100.0


def test_add_yahoo_targets_reuses_cache(monkeypatch):
    pipeline.YAHOO_TARGET_CACHE.clear()
    pipeline.YAHOO_FETCH_TIMINGS.clear()
    pipeline.YAHOO_TARGET_CACHE["AAPL"] = _fake_row("AAPL")

    def should_not_run(_symbol):
        raise AssertionError("cached ticker should not hit Yahoo")

    monkeypatch.setattr(pipeline, "pull_yahoo_targets", should_not_run)
    monkeypatch.setattr(pipeline, "YAHOO_RETRY_MISSING_GROWTH", False)

    holdings = pd.DataFrame({"Yahoo Ticker": ["AAPL"], "Weight": [100.0]})
    out = pipeline.add_yahoo_targets(holdings)
    assert out.iloc[0]["Current Price"] == 100.0


def test_missing_growth_does_not_trigger_second_history_pass_by_default(monkeypatch):
    pipeline.YAHOO_TARGET_CACHE.clear()
    pipeline.YAHOO_FETCH_TIMINGS.clear()

    row = _fake_row("AAPL")
    row["Growth Last Year"] = None

    monkeypatch.setattr(pipeline, "pull_yahoo_targets", lambda _symbol: row.copy())
    monkeypatch.setattr(pipeline, "YAHOO_MAX_WORKERS", 1)
    monkeypatch.setattr(pipeline, "YAHOO_RETRY_MISSING_GROWTH", False)

    def unexpected_retry(_symbol):
        raise AssertionError("duplicate Growth Last Year retry should be disabled")

    monkeypatch.setattr(pipeline, "get_last_calendar_year_stock_return_from_yahoo", unexpected_retry)

    holdings = pd.DataFrame({"Yahoo Ticker": ["AAPL"], "Weight": [100.0]})
    out = pipeline.add_yahoo_targets(holdings)
    assert pd.isna(out.iloc[0]["Growth Last Year"])



def test_yahoo_quality_gate_rejects_zero_target_coverage():
    details = pd.DataFrame(
        {
            "Weight Decimal": [0.6, 0.4],
            "Current Price": [100.0, 200.0],
            "Yahoo Error": ["target error: outage", "target error: outage"],
        }
    )
    summary = {
        "Covered Weight": 0.0,
        "PE Coverage Weight": 0.7,
        "Forward PE Coverage Weight": 0.6,
    }

    with pytest.raises(ReportIncompleteError) as exc:
        pipeline.validate_yahoo_data_quality(details, summary, "TEST")

    assert "analyst-target coverage" in str(exc.value)


def test_yahoo_quality_gate_rejects_empty_pe_history_input():
    details = pd.DataFrame(
        {
            "Weight Decimal": [0.6, 0.4],
            "Current Price": [100.0, 200.0],
            "Yahoo Error": [None, None],
        }
    )
    summary = {
        "Covered Weight": 0.8,
        "PE Coverage Weight": 0.0,
        "Forward PE Coverage Weight": 0.0,
    }

    with pytest.raises(ReportIncompleteError) as exc:
        pipeline.validate_yahoo_data_quality(details, summary, "TEST")

    assert "PE coverage collapsed" in str(exc.value)


def test_yahoo_quality_gate_allows_partial_missing_data_when_coverage_is_healthy():
    details = pd.DataFrame(
        {
            "Weight Decimal": [0.6, 0.3, 0.1],
            "Current Price": [100.0, 200.0, None],
            "Yahoo Error": [None, None, "price unavailable"],
        }
    )
    summary = {
        "Covered Weight": 0.65,
        "PE Coverage Weight": 0.55,
        "Forward PE Coverage Weight": 0.50,
    }

    pipeline.validate_yahoo_data_quality(details, summary, "TEST")


def test_prepare_detail_sheet_does_not_refetch_missing_growth(monkeypatch):
    details = pd.DataFrame(
        {
            "Yahoo Ticker": ["AAPL"],
            "Weight Decimal": [1.0],
            "YTD Return": [0.1],
            "Low Return": [-0.1],
            "Mean Return": [0.1],
            "Median Return": [0.08],
            "High Return": [0.2],
            "Current Price": [100.0],
            "Trailing PE": [20.0],
            "Forward PE": [18.0],
            "Growth Last Year": [None],
            "Growth This Year Est": [0.1],
            "Growth Next Year Est": [0.12],
            "Target Low": [90.0],
            "Target Mean": [110.0],
            "Target Median": [108.0],
            "Target High": [120.0],
            "EPS Last Year": [5.0],
            "EPS This Year Est Avg": [5.5],
            "EPS Next Year Est Avg": [6.0],
        }
    )

    def unexpected_refetch(_symbol):
        raise AssertionError("Excel export must not perform Yahoo network refetches")

    monkeypatch.setattr(
        pipeline,
        "get_last_calendar_year_stock_return_from_yahoo",
        unexpected_refetch,
    )

    out = pipeline.prepare_detail_sheet(details)
    assert pd.isna(out.iloc[0]["Growth Last Year"])


def test_normalize_growth_rate_preserves_yfinance_decimal_ratios():
    assert pipeline.normalize_growth_rate(2.6) == pytest.approx(2.6)
    assert pipeline.normalize_growth_rate(1.6) == pytest.approx(1.6)
    assert pipeline.normalize_growth_rate(0.25) == pytest.approx(0.25)
    assert pipeline.normalize_growth_rate("260%") == pytest.approx(2.6)


def test_ytd_uses_prior_year_end_close(monkeypatch):
    today = pd.Timestamp.now(tz="America/Toronto").tz_localize(None)
    year_start = pd.Timestamp(year=today.year, month=1, day=1)
    idx = pd.to_datetime([
        year_start - pd.Timedelta(days=1),
        year_start + pd.Timedelta(days=2),
        max(year_start + pd.Timedelta(days=3), today.normalize()),
    ])
    series = pd.Series([100.0, 110.0, 120.0], index=idx)
    monkeypatch.setattr(pipeline, "_download_adjusted_close", lambda *_args, **_kwargs: series)
    assert pipeline.get_ytd_return_from_yahoo("TEST") == pytest.approx(0.20)


def test_last_calendar_year_uses_prior_year_end_close(monkeypatch):
    today = pd.Timestamp.now(tz="America/Toronto").tz_localize(None)
    last_year = today.year - 1
    start = pd.Timestamp(year=last_year, month=1, day=1)
    end = pd.Timestamp(year=today.year, month=1, day=1)
    series = pd.Series(
        [100.0, 110.0, 130.0],
        index=pd.to_datetime([
            start - pd.Timedelta(days=1),
            start + pd.Timedelta(days=2),
            end - pd.Timedelta(days=1),
        ]),
    )
    monkeypatch.setattr(pipeline, "_download_adjusted_close", lambda *_args, **_kwargs: series)
    assert pipeline.get_last_calendar_year_stock_return_from_yahoo("TEST") == pytest.approx(0.30)


def test_transient_yahoo_failure_is_not_reused_from_cache(monkeypatch):
    pipeline.YAHOO_TARGET_CACHE.clear()
    pipeline.YAHOO_FETCH_TIMINGS.clear()
    calls = []

    def fake_pull(symbol):
        calls.append(symbol)
        if len(calls) == 1:
            return pipeline._empty_yahoo_row(symbol, "temporary outage")
        return _fake_row(symbol)

    monkeypatch.setattr(pipeline, "pull_yahoo_targets", fake_pull)
    monkeypatch.setattr(pipeline, "YAHOO_MAX_WORKERS", 1)
    monkeypatch.setattr(pipeline, "YAHOO_RETRY_MISSING_GROWTH", False)

    holdings = pd.DataFrame({"Yahoo Ticker": ["AAPL"], "Weight": [100.0]})
    first = pipeline.add_yahoo_targets(holdings)
    assert pd.isna(first.iloc[0]["Current Price"])
    assert "AAPL" not in pipeline.YAHOO_TARGET_CACHE

    second = pipeline.add_yahoo_targets(holdings)
    assert second.iloc[0]["Current Price"] == 100.0
    assert calls == ["AAPL", "AAPL"]
    assert "AAPL" in pipeline.YAHOO_TARGET_CACHE
