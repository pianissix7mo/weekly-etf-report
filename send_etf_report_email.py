"""
Email the latest ETF analyst report Excel file produced by:
    etf_analyst_report_complete.py

Required environment variables:
    SMTP_HOST       example: smtp.gmail.com
    SMTP_PORT       example: 465
    SMTP_USER       your sender email/login
    SMTP_PASSWORD   app password or SMTP password

Optional environment variables:
    EMAIL_FROM      sender shown in email; defaults to SMTP_USER
    EMAIL_TO        comma-separated recipients; defaults to the two addresses below
    REPORT_DIR      defaults to etf_analyst_target_outputs
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.utils import format_datetime, make_msgid
from etf_report.delivery import load_delivery
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo
from zipfile import ZipFile, BadZipFile
from xml.etree import ElementTree as ET

DEFAULT_RECIPIENTS = "mofeiwang@hotmail.com,mofeiwang@yahoo.ca"


def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def find_latest_report(report_dir: Path) -> Path:
    canonical = report_dir / "ETF_analyst_report.xlsx"
    if canonical.exists():
        return canonical

    candidates = sorted(
        list(report_dir.glob("ETF_analyst_report_*.xlsx"))
        + list(report_dir.glob("ETF_analyst_report_*.xlsm")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No ETF analyst report found in: {report_dir.resolve()}"
        )
    return candidates[0]


def validate_report_attachment(report_path: Path) -> None:
    """Do not email a missing, truncated, or structurally incomplete workbook."""
    if not report_path.is_file() or report_path.stat().st_size < 10_000:
        raise RuntimeError(f"ETF workbook is missing or unexpectedly small: {report_path}")
    try:
        with ZipFile(report_path) as archive:
            bad = archive.testzip()
            if bad:
                raise RuntimeError(f"Corrupt XLSX ZIP entry: {bad}")
            root = ET.fromstring(archive.read("xl/workbook.xml"))
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            sheets = {
                elem.attrib.get("name") for elem in root.findall(".//s:sheet", namespaces=ns)
            }
    except (BadZipFile, KeyError, ET.ParseError) as exc:
        raise RuntimeError(f"ETF workbook XLSX is invalid: {report_path}") from exc

    must_have = {
        "Summary", "Forward PE", "QQQ", "SPMO", "OEF", "MAGS",
        "CHAT", "DRAM", "TECH_TO", "CHPS_TO", "SOXX", "SMH",
        "XLK", "Price_Audit", "ETF Overlap",
    }
    missing = sorted(must_have - sheets)
    if missing:
        raise RuntimeError(f"ETF workbook is missing required sheets: {missing}")


def send_email_with_attachment(report_path: Path, *, data: bytes | None = None,
                               generated_at: datetime | None = None) -> None:
    smtp_host = env_required("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = env_required("SMTP_USER")
    smtp_password = env_required("SMTP_PASSWORD")

    email_from = os.environ.get("EMAIL_FROM", smtp_user).strip() or smtp_user
    email_to = os.environ.get("EMAIL_TO", DEFAULT_RECIPIENTS).strip() or DEFAULT_RECIPIENTS
    recipients = [x.strip() for x in email_to.split(",") if x.strip()]
    recipients = list(dict.fromkeys(recipients))
    if not recipients:
        raise RuntimeError("EMAIL_TO contains no recipients")

    toronto_now = datetime.now(ZoneInfo("America/Toronto"))
    today = toronto_now.strftime("%Y-%m-%d")

    msg = EmailMessage()
    msg["Subject"] = f"ETF Analyst Report - {today} {toronto_now:%H:%M} Toronto"
    msg["From"] = email_from
    msg["To"] = ", ".join(recipients)
    msg["Date"] = format_datetime(toronto_now)
    msg["Message-ID"] = make_msgid()
    msg.set_content(
        "Hi,\n\n"
        "Attached is the latest daily ETF analyst target report.\n\n"
        f"File: {report_path.name}\n\n"
        + (f"Report generated: {generated_at.isoformat()}\n\n" if generated_at else "")
        + "This email was sent automatically by GitHub Actions.\n"
    )

    if data is None:
        data = report_path.read_bytes()
    msg.add_attachment(
        data,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=report_path.name,
    )

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30,
                          context=ssl.create_default_context()) as smtp:
        smtp.login(smtp_user, smtp_password)
        refused = smtp.send_message(msg, from_addr=email_from, to_addrs=recipients)

    if refused:
        accepted = [address for address in recipients if address not in refused]
        print(f"SMTP accepted recipients: {', '.join(accepted)}")
        codes = {address: result[0] for address, result in refused.items()}
        raise RuntimeError(f"SMTP refused recipients: {codes}; do not resend to accepted recipients")

    print(f"SMTP accepted email for: {', '.join(recipients)}")
    print(f"Attachment: {report_path.resolve()}")


def main() -> None:
    report_dir = Path(os.environ.get("REPORT_DIR", "etf_analyst_target_outputs"))
    report_path, data, generated_at = load_delivery(report_dir)
    validate_report_attachment(report_path)

    today_code = generated_at.strftime("%y%m%d")
    dated_report_path = report_dir / f"ETF_analyst_report_{today_code}.xlsx"

    if report_path.name != dated_report_path.name:
        dated_report_path.write_bytes(data)
        report_path = dated_report_path

    send_email_with_attachment(report_path, data=data, generated_at=generated_at)


if __name__ == "__main__":
    main()

