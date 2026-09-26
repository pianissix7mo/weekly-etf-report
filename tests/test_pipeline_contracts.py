import pandas as pd
import pytest

import etf_analyst_report_complete as pipeline
from etf_report.errors import (
    HoldingsWeightInvalidError,
    ReportIncompleteError,
    SourceSchemaChangedError,
)


def test_normalize_holdings_scales_fraction_weights_and_drops_cash():
    raw = pd.DataFrame(
        {
            "Ticker": ["AAPL", "MSFT", "USD"],
            "Name": ["Apple Inc.", "Microsoft Corp.", "Cash"],
            "Weight": [0.60, 0.30, 0.10],
            "Shares": [10, 20, 1],
        }
    )
    out = pipeline.normalize_holdings(raw, "TEST")
    assert set(out["Yahoo Ticker"]) == {"AAPL", "MSFT"}
    weights = dict(zip(out["Yahoo Ticker"], out["Weight"]))
    assert weights["AAPL"] == pytest.approx(60.0)
    assert weights["MSFT"] == pytest.approx(30.0)


def test_normalize_holdings_merges_duplicate_symbols():
    raw = pd.DataFrame(
        {
            "Ticker": ["AAPL US", "AAPL"],
            "Name": ["Apple Inc.", "Apple Inc."],
            "Weight": [35.0, 25.0],
        }
    )
    out = pipeline.normalize_holdings(raw, "TEST")
    assert len(out) == 1
    assert out.iloc[0]["Yahoo Ticker"] == "AAPL"
    assert out.iloc[0]["Weight"] == pytest.approx(60.0)


def test_missing_weight_column_is_typed_schema_error():
    raw = pd.DataFrame({"Ticker": ["AAPL"], "Name": ["Apple Inc."]})
    with pytest.raises(SourceSchemaChangedError) as exc:
        pipeline.normalize_holdings(raw, "TEST")
    assert exc.value.code == "SOURCE_SCHEMA_CHANGED"


def test_invalid_total_weight_is_typed_error():
    holdings = pd.DataFrame({"Weight": [20.0, 10.0]})
    with pytest.raises(HoldingsWeightInvalidError) as exc:
        pipeline.sanity_check_weight_total(holdings, "TEST", low=70, high=130)
    assert exc.value.code == "HOLDINGS_WEIGHT_INVALID"


def test_main_fails_closed_and_does_not_export_partial_report(monkeypatch):
    monkeypatch.setattr(pipeline, "ETFS", ["AAA", "BBB"])
    calls = {"history": 0, "export": 0}

    def fake_run(etf):
        if etf == "BBB":
            raise RuntimeError("source broke")
        return pd.DataFrame({"ETF": ["AAA"]}), {"ETF": "AAA"}

    def fake_history(_summaries):
        calls["history"] += 1
        return pd.DataFrame()

    def fake_export(*_args, **_kwargs):
        calls["export"] += 1

    monkeypatch.setattr(pipeline, "run_one_etf", fake_run)
    monkeypatch.setattr(pipeline, "update_pe_history", fake_history)
    monkeypatch.setattr(pipeline, "export_excel_report", fake_export)

    with pytest.raises(ReportIncompleteError) as exc:
        pipeline.main_with_excel()

    assert exc.value.code == "REPORT_INCOMPLETE"
    assert "BBB" in str(exc.value)
    assert calls == {"history": 0, "export": 0}


def test_main_exports_only_after_all_required_etfs_complete(monkeypatch):
    monkeypatch.setattr(pipeline, "ETFS", ["AAA", "BBB"])
    calls = {"history": 0, "export": 0}

    def fake_run(etf):
        return pd.DataFrame({"ETF": [etf]}), {"ETF": etf}

    def fake_history(summaries):
        calls["history"] += 1
        assert {s["ETF"] for s in summaries} == {"AAA", "BBB"}
        return pd.DataFrame({"ETF": ["AAA", "BBB"]})

    def fake_export(all_details, summaries, pe_history=None):
        calls["export"] += 1
        assert len(all_details) == 2
        assert len(summaries) == 2
        assert pe_history is not None

    monkeypatch.setattr(pipeline, "run_one_etf", fake_run)
    monkeypatch.setattr(pipeline, "update_pe_history", fake_history)
    monkeypatch.setattr(pipeline, "export_excel_report", fake_export)

    details, summaries, failures = pipeline.main_with_excel()
    assert len(details) == 2
    assert len(summaries) == 2
    assert failures == []
    assert calls == {"history": 1, "export": 1}


def test_source_helpers_survive_module_split():
    assert pipeline.INVESCO_OFFICIAL_PAGE_URLS["QQQ"].startswith("https://")
    assert callable(pipeline.map_name_to_yahoo)
    assert pipeline.map_name_to_yahoo("NVIDIA Corporation") == "NVDA"
