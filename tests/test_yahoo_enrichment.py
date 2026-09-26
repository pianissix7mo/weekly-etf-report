import pandas as pd

import etf_analyst_report_complete as pipeline


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

    assert set(called) == {"AAPL", "MSFT"}
    assert len(called) == 2
    skipped = out.set_index("Yahoo Ticker").loc[["ARM$CBRS", "IXTZ6", "AGPXX"], "Yahoo Error"]
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
