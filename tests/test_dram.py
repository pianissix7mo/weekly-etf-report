import pandas as pd
import pytest

import etf_analyst_report_complete as pipeline


def _dram_fixture():
    return pd.DataFrame(
        {
            "Ticker": [
                "005930 KS", "000660 KS", "MU", "SNDK", "WDC",
                "STX", "285A JT", "2408 TT", "2344 TT",
            ],
            "Name": [
                "Samsung Electronics Co Ltd",
                "SK Hynix Inc",
                "Micron Technology Inc",
                "SanDisk Corp",
                "Western Digital Corp",
                "Seagate Technology Holdings PLC",
                "Kioxia Holdings Corp",
                "Nanya Technology Corp",
                "Winbond Electronics Corp",
            ],
            "Weight": [25.0, 24.0, 23.0, 5.0, 5.0, 5.0, 4.0, 4.0, 5.0],
        }
    )


def test_dram_is_required_and_uses_roundhill_config():
    assert "DRAM" in pipeline.ETFS
    cfg = pipeline.ETF_CONFIG["DRAM"]
    assert cfg["issuer"] == "Roundhill live holdings"
    assert cfg["url"].endswith("/etf/dram/")
    assert cfg["min_rows"] == 8


def test_dram_roundhill_candidate_accepts_focused_memory_basket():
    raw = _dram_fixture()
    validated = pipeline.standardize_roundhill_candidate(
        raw, "DRAM", "fixture"
    )
    normalized = pipeline.normalize_holdings(validated, "DRAM")

    assert len(normalized) == 9
    assert normalized["Weight"].sum() == pytest.approx(100.0)
    assert {
        "005930.KS", "000660.KS", "MU", "SNDK", "WDC",
        "STX", "285A.T", "2408.TW", "2344.TW",
    }.issubset(set(normalized["Yahoo Ticker"]))


def test_dram_roundhill_candidate_rejects_incomplete_basket():
    raw = _dram_fixture().iloc[:7].copy()
    with pytest.raises(ValueError, match="only 7 rows"):
        pipeline.standardize_roundhill_candidate(raw, "DRAM", "fixture")


def test_dram_dispatch_uses_roundhill_adapter(monkeypatch):
    raw = _dram_fixture()
    called = {"dram": 0}

    def fake_pull():
        called["dram"] += 1
        return raw.copy()

    monkeypatch.setattr(pipeline, "pull_dram_roundhill_issuer_page", fake_pull)
    holdings = pipeline.pull_issuer_holdings("DRAM")

    assert called["dram"] == 1
    assert len(holdings) == 9
    assert holdings["Weight"].sum() == pytest.approx(100.0)


def test_dram_preserves_gross_exposure_above_100_percent():
    raw = _dram_fixture().copy()
    scale = 115.05 / raw["Weight"].sum()
    raw["Weight"] = raw["Weight"] * scale

    validated = pipeline.standardize_roundhill_candidate(raw, "DRAM", "gross exposure fixture")
    normalized = pipeline.normalize_holdings(validated, "DRAM")

    assert normalized["Weight"].sum() == pytest.approx(115.05, abs=0.01)
    assert pipeline.ETF_CONFIG["DRAM"]["max_total_weight"] == 130
    assert "gross exposure" in pipeline.ETF_CONFIG["DRAM"]["weight_basis"]


def test_dram_rejects_implausible_gross_exposure():
    raw = _dram_fixture().copy()
    raw["Weight"] = raw["Weight"] * 1.40
    with pytest.raises(ValueError, match="bad total"):
        pipeline.standardize_roundhill_candidate(raw, "DRAM", "too leveraged fixture")
