"""Regression coverage for Yahoo target gaps and Alphabet Class A/Class C."""
from collections import Counter

import pandas as pd
import pytest

import etf_analyst_report_complete as pipeline
from etf_report.errors import ReportIncompleteError
from etf_report.target_quality import (
    clean_target_fields,
    complete_target_trio,
    fill_alphabet_share_class_proxy,
    fill_from_quote_summary,
)


def test_same_ticker_quote_summary_recovers_empty_endpoint():
    row = pipeline._empty_yahoo_row("GOOGL")
    fill_from_quote_summary(row, {
        "targetLowPrice": 290,
        "targetMeanPrice": 395,
        "targetHighPrice": 460,
        "numberOfAnalystOpinions": 48,
    })
    clean_target_fields(row)
    assert complete_target_trio(row)
    assert row["Target Mean"] == 395
    assert row["Analyst Count"] == 48
    assert "quote summary" in row["Target Source"]


def test_bad_or_nonfinite_targets_are_not_valid_consensus():
    row = {"Target Low": 500, "Target Mean": 400, "Target High": 300}
    clean_target_fields(row)
    assert not complete_target_trio(row)
    assert "inconsistent" in row["Yahoo Error"]
    for bad in (0, -3, float("nan"), float("inf"), "n/a"):
        assert not complete_target_trio({"Target Low": 1, "Target Mean": bad, "Target High": 3})


def _alphabet_rows():
    own = pipeline._empty_yahoo_row("GOOGL")
    own.update({"Current Price": 298, "Price Date": "2026-11-12"})
    peer = pipeline._empty_yahoo_row("GOOG")
    peer.update({
        "Current Price": 295, "Price Date": "2026-11-12",
        "Target Low": 300, "Target Mean": 400,
        "Target High": 500, "Target Median": 410,
        "Target Source": "Yahoo analyst endpoint",
    })
    return own, peer


def test_alphabet_proxy_is_scaled_and_clearly_marked():
    own, peer = _alphabet_rows()
    assert fill_alphabet_share_class_proxy(own, peer, "GOOG")
    assert own["Target Mean"] == pytest.approx(400 * 298 / 295)
    assert own["Target Median"] == pytest.approx(410 * 298 / 295)
    assert own["Analyst Count"] is None
    assert "NOT direct GOOGL consensus" in own["Target Source"]


@pytest.mark.parametrize("badness", ["different_date", "missing_date", "divergent_price", "chained_proxy", "no_donor_targets"])
def test_alphabet_proxy_rejects_unsafe_sources(badness):
    own, peer = _alphabet_rows()
    if badness == "different_date":
        peer["Price Date"] = "2026-11-11"
    elif badness == "missing_date":
        own["Price Date"] = None
    elif badness == "divergent_price":
        own["Current Price"] = 100
    elif badness == "chained_proxy":
        peer["Target Source"] = "ALPHABET PROXY: from GOOGL"
    else:
        peer["Target Mean"] = None
    assert not fill_alphabet_share_class_proxy(own, peer, "GOOG")
    assert own["Target Mean"] is None


def test_empty_core_ticker_targets_are_not_cached(monkeypatch):
    monkeypatch.setattr(pipeline, "expected_completed_session_date", lambda symbol: "2026-11-12")
    row = pipeline._empty_yahoo_row("GOOGL")
    row.update({"Current Price": 298, "Price Date": "2026-11-12"})
    assert not pipeline._yahoo_row_cacheable(row)
    row.update({"Target Low": 300, "Target Mean": 400, "Target High": 500})
    assert pipeline._yahoo_row_cacheable(row)


def test_missing_googl_target_tries_direct_then_peer(monkeypatch):
    monkeypatch.setattr(pipeline, "YAHOO_TARGET_CACHE", {})
    monkeypatch.setattr(pipeline, "YAHOO_CRITICAL_RETRIED", set())
    monkeypatch.setattr(pipeline, "YAHOO_ALPHABET_DONOR_CACHE", {})
    monkeypatch.setattr(pipeline, "expected_completed_session_date", lambda symbol: "2026-11-12")
    calls = Counter()

    def fake_fetch(symbol):
        calls[symbol] += 1
        own, peer = _alphabet_rows()
        return symbol, own if symbol == "GOOGL" else peer, 0.01

    monkeypatch.setattr(pipeline, "_pull_yahoo_targets_timed", fake_fetch)
    holdings = pd.DataFrame({"Yahoo Ticker": ["GOOGL"], "Weight": [15.0]})
    result = pipeline.add_yahoo_targets(holdings)
    assert calls == {"GOOGL": 2, "GOOG": 1}
    assert result.iloc[0]["Target Mean"] == pytest.approx(400 * 298 / 295)
    assert "ALPHABET PROXY" in result.iloc[0]["Target Source"]


def test_major_googl_target_gap_fails_quality_gate(monkeypatch):
    monkeypatch.setattr(pipeline, "expected_completed_session_date", lambda symbol: "2026-11-12")
    row = pd.DataFrame([{
        "Yahoo Ticker": "GOOGL", "Weight Decimal": 0.03,
        "Current Price": 298.0, "Price Date": "2026-11-12",
        "Target Low": None, "Target Mean": None, "Target High": None,
        "Forward PE": 20.0,
    }, {
        "Yahoo Ticker": "AAPL", "Weight Decimal": 0.97,
        "Current Price": 250.0, "Price Date": "2026-11-12",
        "Target Low": 250.0, "Target Mean": 300.0, "Target High": 350.0,
        "Forward PE": 28.0,
    }])
    summary = {"Covered Weight": 0.97, "PE Coverage Weight": 0.97,
               "Forward PE Coverage Weight": 1.0}
    with pytest.raises(ReportIncompleteError, match="major holding missing analyst targets.*GOOGL"):
        pipeline.validate_yahoo_data_quality(row, summary, "QQQ")
