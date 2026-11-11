import io
from zipfile import ZipFile, ZIP_STORED

import pytest

from send_etf_report_email import validate_report_attachment


REQUIRED_SHEETS = [
    "Summary", "Forward PE", "QQQ", "SPMO", "OEF", "MAGS",
    "CHAT", "DRAM", "TECH_TO", "CHPS_TO", "SOXX", "SMH",
    "XLK", "Price_Audit", "ETF Overlap",
]


def make_workbook(path, names):
    # A minimal XLSX-shaped ZIP is sufficient for testing the safety gate.
    sheet_xml = "".join(f'<sheet name="{name}" sheetId="{i+1}"/>' for i, name in enumerate(names))
    workbook_xml = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheets>{sheet_xml}</sheets></workbook>"
    )
    with ZipFile(path, "w", ZIP_STORED) as archive:
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("padding.bin", bytes(range(256)) * 50)


def test_complete_analyst_report_allowed_for_email(tmp_path):
    path = tmp_path / "report.xlsx"
    make_workbook(path, REQUIRED_SHEETS)
    validate_report_attachment(path)


def test_missing_etf_tab_prevents_email(tmp_path):
    path = tmp_path / "report.xlsx"
    make_workbook(path, [x for x in REQUIRED_SHEETS if x != "QQQ"])
    with pytest.raises(RuntimeError, match="missing required sheets"):
        validate_report_attachment(path)


def test_invalid_xlsx_prevents_email(tmp_path):
    path = tmp_path / "report.xlsx"
    path.write_bytes(b"x" * 15000)
    with pytest.raises(RuntimeError, match="invalid"):
        validate_report_attachment(path)
