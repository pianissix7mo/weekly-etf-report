"""
Email the latest ETF analyst report Excel file produced by:
    etf_analyst_report_LAST_WORKING_PLUS_INVESCO_BROWSER.py

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
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_RECIPIENTS = "mofeiwang@hotmail.com,mofeiwang@yahoo.ca"


def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def find_latest_report(report_dir: Path) -> Path:
    candidates = sorted(
        list(report_dir.glob("*.xlsx")) + list(report_dir.glob("*.xlsm")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No Excel report found in: {report_dir.resolve()}")
    return candidates[0]


def send_email_with_attachment(report_path: Path) -> None:
    smtp_host = env_required("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = env_required("SMTP_USER")
    smtp_password = env_required("SMTP_PASSWORD")

    email_from = os.environ.get("EMAIL_FROM", smtp_user).strip() or smtp_user
    email_to = os.environ.get("EMAIL_TO", DEFAULT_RECIPIENTS).strip() or DEFAULT_RECIPIENTS
    recipients = [x.strip() for x in email_to.split(",") if x.strip()]

    today = datetime.now().strftime("%Y-%m-%d")

    msg = EmailMessage()
    msg["Subject"] = f"Weekly ETF Analyst Target Report - {today}"
    msg["From"] = email_from
    msg["To"] = ", ".join(recipients)
    msg.set_content(
        "Hi,\n\n"
        "Attached is the latest weekly ETF analyst target report.\n\n"
        f"File: {report_path.name}\n\n"
        "This email was sent automatically by GitHub Actions.\n"
    )

    data = report_path.read_bytes()
    msg.add_attachment(
        data,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=report_path.name,
    )

    with smtplib.SMTP_SSL(smtp_host, smtp_port) as smtp:
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)

    print(f"Email sent to: {', '.join(recipients)}")
    print(f"Attachment: {report_path.resolve()}")


def main() -> None:
    report_dir = Path(os.environ.get("REPORT_DIR", "etf_analyst_target_outputs"))
    report_path = find_latest_report(report_dir)

    today_code = datetime.now(ZoneInfo("America/Toronto")).strftime("%y%m%d")
    dated_report_path = report_dir / f"ETF_analyst_report_{today_code}.xlsx"

    if report_path.name != dated_report_path.name:
        dated_report_path.write_bytes(report_path.read_bytes())
        report_path = dated_report_path

    send_email_with_attachment(report_path)


if __name__ == "__main__":
    main()
