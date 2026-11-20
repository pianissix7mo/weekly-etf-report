import json
import smtplib
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import send_etf_report_email as emailer
from etf_report.delivery import MANIFEST_NAME, TORONTO, load_delivery, write_delivery_manifest


@pytest.fixture
def report(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    path = tmp_path / "ETF_analyst_report.xlsx"
    path.write_bytes(b"report bytes")
    write_delivery_manifest(path)
    return path


def test_generated_report_round_trip(report):
    path, data, generated = load_delivery(report.parent)
    assert path == report
    assert data == b"report bytes"
    assert generated.date() == datetime.now(TORONTO).date()


def test_main_uses_verified_report_and_generation_date(report, monkeypatch):
    monkeypatch.setenv("REPORT_DIR", str(report.parent))
    monkeypatch.setattr(emailer, "validate_report_attachment", lambda path: None)
    send = MagicMock()
    monkeypatch.setattr(emailer, "send_email_with_attachment", send)
    emailer.main()
    path = send.call_args.args[0]
    assert path.name == f"ETF_analyst_report_{datetime.now(TORONTO):%y%m%d}.xlsx"
    assert path.read_bytes() == send.call_args.kwargs["data"] == b"report bytes"


def test_pipeline_writes_manifest_only_after_export(tmp_path, monkeypatch):
    import pandas as pd
    import etf_analyst_report_complete as pipeline

    path = tmp_path / "ETF_analyst_report.xlsx"
    monkeypatch.setattr(pipeline, "REPORT_PATH", path)
    monkeypatch.setattr(pipeline, "ETFS", ["AAA"])
    monkeypatch.setattr(pipeline, "run_one_etf", lambda etf: (pd.DataFrame(), {"ETF": etf}))
    monkeypatch.setattr(pipeline, "update_pe_history", lambda summaries: pd.DataFrame())
    monkeypatch.setattr(pipeline, "export_excel_report", lambda *args, **kwargs: path.write_bytes(b"exported"))
    pipeline.main_with_excel()
    assert load_delivery(tmp_path)[1] == b"exported"

    (tmp_path / MANIFEST_NAME).unlink()
    def failed_export(*args, **kwargs):
        raise RuntimeError("export failed")
    monkeypatch.setattr(pipeline, "export_excel_report", failed_export)
    with pytest.raises(RuntimeError, match="export failed"):
        pipeline.main_with_excel()
    assert not (tmp_path / MANIFEST_NAME).exists()


@pytest.mark.parametrize("case", ["old", "future", "naive", "hash", "run", "attempt", "missing"])
def test_invalid_delivery_never_sends(report, monkeypatch, case):
    manifest = report.parent / MANIFEST_NAME
    payload = json.loads(manifest.read_text())
    if case == "old":
        payload["generated_at"] = (datetime.now(TORONTO) - timedelta(days=1)).isoformat()
    elif case == "future":
        payload["generated_at"] = (datetime.now(TORONTO) + timedelta(minutes=5)).isoformat()
    elif case == "naive":
        payload["generated_at"] = datetime.now().isoformat()
    elif case == "hash":
        report.write_bytes(b"changed after generation")
    elif case == "run":
        monkeypatch.setenv("GITHUB_RUN_ID", "456")
    elif case == "attempt":
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    manifest.write_text(json.dumps(payload))
    if case == "missing":
        manifest.unlink()
    monkeypatch.setenv("REPORT_DIR", str(report.parent))
    send = MagicMock()
    monkeypatch.setattr(emailer, "send_email_with_attachment", send)
    with pytest.raises(RuntimeError, match="delivery validation failed"):
        emailer.main()
    send.assert_not_called()


@pytest.fixture
def smtp(monkeypatch):
    for key, value in {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "sender@example.com",
                       "SMTP_PASSWORD": "test-only", "EMAIL_TO": "a@example.com,b@example.com"}.items():
        monkeypatch.setenv(key, value)
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.send_message.return_value = {}
    constructor = MagicMock(return_value=connection)
    monkeypatch.setattr(emailer.smtplib, "SMTP_SSL", constructor)
    return constructor, connection


def test_success_has_timeout_tls_and_exact_attachment(report, smtp):
    constructor, connection = smtp
    emailer.send_email_with_attachment(report, data=b"verified snapshot")
    assert constructor.call_args.kwargs["timeout"] == 30
    assert constructor.call_args.kwargs["context"].check_hostname
    message = connection.send_message.call_args.args[0]
    assert message["Date"] and message["Message-ID"]
    assert list(message.iter_attachments())[0].get_payload(decode=True) == b"verified snapshot"
    assert connection.send_message.call_args.kwargs["to_addrs"] == ["a@example.com", "b@example.com"]


def test_partial_refusal_fails_without_resending(report, smtp, capsys):
    _, connection = smtp
    connection.send_message.return_value = {"b@example.com": (450, b"temporary refusal")}
    with pytest.raises(RuntimeError, match="b@example.com"):
        emailer.send_email_with_attachment(report)
    connection.send_message.assert_called_once()
    assert "SMTP accepted recipients: a@example.com" in capsys.readouterr().out


@pytest.mark.parametrize("error", [TimeoutError("timeout"), smtplib.SMTPAuthenticationError(535, b"auth"),
                                  smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"refused")})])
def test_transport_failures_propagate_without_retry(report, smtp, error):
    _, connection = smtp
    connection.send_message.side_effect = error
    with pytest.raises(type(error)):
        emailer.send_email_with_attachment(report)
    connection.send_message.assert_called_once()


def test_complete_pipeline_exports_pe_history_and_emails_generated_workbook(tmp_path, monkeypatch, smtp):
    from collections import Counter

    import openpyxl
    import pandas as pd
    import etf_analyst_report_complete as pipeline

    report = tmp_path / "ETF_analyst_report.xlsx"
    history = tmp_path / "ETF_PE_history.xlsx"
    monkeypatch.setattr(pipeline, "REPORT_PATH", report)
    monkeypatch.setattr(pipeline, "PE_HISTORY_PATH", history)
    monkeypatch.setenv("REPORT_DIR", str(tmp_path))
    calls = Counter()

    def holdings(etf):
        return pd.DataFrame({"ETF": [etf], "Yahoo Ticker": ["TSLA"], "Weight": [100.0]})

    def ticker(symbol):
        calls[symbol] += 1
        row = pipeline._empty_yahoo_row(symbol)
        row.update({
            "Current Price": 100, "Price Date": pipeline.expected_completed_session_date(symbol),
            "Target Low": 90, "Target Mean": 110, "Target High": 125, "Target Median": 108,
            "Trailing PE": 20, "Forward PE": 17, "Price Source": "Yahoo daily chart",
            "Target Source": "FMP price target consensus",
        })
        return row

    def fund_data(symbol, _ticker, required):
        calls[symbol] += 1
        return 50, required, "Yahoo daily chart", pd.Series(dtype=float), None, ""

    def no_refetch(*args):
        pytest.fail("Excel formatting cannot refetch a verified ETF close")

    monkeypatch.setattr(pipeline, "pull_issuer_holdings", holdings)
    monkeypatch.setattr(pipeline, "pull_yahoo_targets", ticker)
    monkeypatch.setattr(pipeline, "fetch_daily_prices", fund_data)
    monkeypatch.setattr(pipeline.yf, "Ticker", lambda *args, **kwargs: object())
    monkeypatch.setattr(pipeline, "get_etf_current_price", no_refetch)
    monkeypatch.setattr(pipeline, "build_vix_summary", lambda: (pd.DataFrame(), "fixture"))
    monkeypatch.setattr(pipeline, "build_fear_greed_summary", lambda: (pd.DataFrame(), "fixture"))

    pipeline.main_with_excel()
    workbook = openpyxl.load_workbook(report)
    assert {"Summary", "Forward PE", "Price_Audit", "Target_Audit", "ETF Overlap"} <= set(workbook.sheetnames)
    assert any(cell.data_type == "f" for row in workbook["QQQ"] for cell in row)
    assert workbook["Price_Audit"]["F2"].value == "ETF CLOSE"
    assert set(pd.read_excel(history)["ETF"]) == set(pipeline.ETFS)
    assert calls == Counter({symbol: 1 for symbol in (*pipeline.ETFS, "TSLA")})
    emailer.main()
    _, connection = smtp
    connection.send_message.assert_called_once()
    attachment = next(connection.send_message.call_args.args[0].iter_attachments())
    assert attachment.get_payload(decode=True) == report.read_bytes()
