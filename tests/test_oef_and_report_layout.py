import io

import openpyxl
import pandas as pd
import pytest

import etf_analyst_report_complete as pipeline


class _FakeResponse:
    def __init__(self, content: bytes, status_code=200):
        self.content = content
        self.status_code = status_code
        self.text = content.decode("utf-8", errors="ignore")
        self.headers = {"content-type": "text/csv"}


def _make_oef_csv():
    rows = ["Ticker,Name,Weight (%)"]
    for i in range(100):
        ticker = f"T{i:03d}"
        rows.append(f"{ticker},Company {i},{1.0:.2f}")
    return ("\n".join(rows) + "\n").encode("utf-8")


def test_oef_is_in_required_etf_config():
    assert "OEF" in pipeline.ETFS
    assert pipeline.ETF_CONFIG["OEF"]["product_id"] == "239723"
    assert pipeline.ETF_CONFIG["OEF"]["file_name"] == "OEF_holdings"
    assert pipeline.ETF_CONFIG["OEF"]["min_rows"] >= 90


def test_oef_blackrock_csv_parser_uses_official_product_id(monkeypatch):
    seen_urls = []

    def fake_get(url, headers=None, timeout=None):
        seen_urls.append(url)
        return _FakeResponse(_make_oef_csv())

    monkeypatch.setattr(pipeline.requests, "get", fake_get)

    raw = pipeline.pull_blackrock_oef()
    normalized = pipeline.normalize_holdings(raw, "OEF")

    assert len(normalized) == 100
    assert normalized["Weight"].sum() == pytest.approx(100.0)
    assert seen_urls
    assert "239723" in seen_urls[0]
    assert "OEF_holdings" in seen_urls[0]


def test_forward_pe_charts_live_on_dedicated_tab(tmp_path):
    output = tmp_path / "layout.xlsx"
    hist = pd.DataFrame(
        [
            {"Date": "2026-08-01", "ETF": "QQQ", "PE Ratio": 30.0, "Forward PE": 25.0},
            {"Date": "2026-09-01", "ETF": "QQQ", "PE Ratio": 31.0, "Forward PE": 26.0},
            {"Date": "2026-08-01", "ETF": "OEF", "PE Ratio": 28.0, "Forward PE": 23.0},
            {"Date": "2026-09-01", "ETF": "OEF", "PE Ratio": 29.0, "Forward PE": 24.0},
        ]
    )

    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        workbook = writer.book
        summary = workbook.add_worksheet("Summary")
        writer.sheets["Summary"] = summary
        pipeline._write_summary_pe_history_data_and_charts(
            workbook,
            summary,
            writer,
            hist,
            chart_start_row=2,
            start_col=1,
        )

    wb = openpyxl.load_workbook(output, read_only=False, data_only=False)
    assert "Summary" in wb.sheetnames
    assert "Forward PE" in wb.sheetnames
    assert "PE_History_Data" in wb.sheetnames
    assert wb["PE_History_Data"].sheet_state == "hidden"
    assert len(wb["Summary"]._charts) == 2
    assert len(wb["Forward PE"]._charts) == 2
