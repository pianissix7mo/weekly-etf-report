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
    assert "combined company exposure" in cfg["weight_basis"]


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
    with pytest.raises(ValueError, match="only 7 company exposure rows"):
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


def test_dram_rejects_double_counted_company_exposure_above_expected_range():
    raw = _dram_fixture().copy()
    scale = 115.05 / raw["Weight"].sum()
    raw["Weight"] = raw["Weight"] * scale

    with pytest.raises(ValueError, match="bad company exposure total"):
        pipeline.standardize_roundhill_candidate(raw, "DRAM", "double-counted fixture")

    assert pipeline.ETF_CONFIG["DRAM"]["max_total_weight"] == 110
    basis = pipeline.ETF_CONFIG["DRAM"]["weight_basis"].lower()
    assert "combined" in basis
    assert "stock" in basis
    assert "total return swaps" in basis


def test_dram_rejects_implausible_company_exposure():
    raw = _dram_fixture().copy()
    raw["Weight"] = raw["Weight"] * 1.40
    with pytest.raises(ValueError, match="bad company exposure total"):
        pipeline.standardize_roundhill_candidate(raw, "DRAM", "too high fixture")


def test_dram_hierarchical_table_uses_company_exposure_rows_only():
    raw = pd.DataFrame(
        {
            "Name": [
                "Micron Technology Inc", None, None, None,
                "Samsung Electronics Co", None, None, None,
                "SK hynix", None, None, None,
                "CXMT",
                "Sandisk", "Seagate Technology Holdings", "Western Digital",
                "Kioxia Holdings", "Nanya Technology", "Winbond Electronics",
            ],
            "Ticker": [
                None, "595112103 TRS 050427 NM", "595112103 TRS 052427 GS", "MU",
                None, "005930 KS", "6771720 TRS 052427 GS", "005935 KS",
                None, "000660 KS", "6450267 TRS 052427 GS", "SKHY",
                "BTMTQT8 TRS 052427 GS",
                "SNDK", "STX", "WDC", "285A JP", "2408 TT", "2344 TT",
            ],
            "Weight": [
                "26.33%", "16.26%", "9.66%", "0.41%",
                "25.16%", "19.04%", "6.01%", "0.11%",
                "22.77%", "16.80%", "5.37%", "0.60%",
                "5.08%",
                "4.72%", "4.63%", "3.57%", "3.28%", "2.39%", "1.09%",
            ],
        }
    )

    exposure_rows = pipeline.standardize_dram_exposure_candidate(raw, "fixture")
    normalized = pipeline.normalize_holdings(exposure_rows, "DRAM")

    # The issuer's company-level exposure is ~99%, while raw stock/swap legs
    # would double count the same exposures.
    assert normalized["Weight"].sum() == pytest.approx(99.01, abs=0.02)
    assert "MU" in set(normalized["Yahoo Ticker"])
    assert "005930.KS" in set(normalized["Yahoo Ticker"])
    assert "000660.KS" in set(normalized["Yahoo Ticker"])
    assert "285A.T" in set(normalized["Yahoo Ticker"])
    assert not normalized["Raw Ticker"].astype(str).str.contains("595112103 TRS").any()
