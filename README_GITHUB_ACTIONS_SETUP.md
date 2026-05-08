# Weekly ETF Analyst Report on GitHub Actions

This repo setup runs the ETF analyst report every Friday night and emails the Excel file to:

- mofeiwang@hotmail.com
- mofeiwang@yahoo.ca

## Files to commit

Put these files in your GitHub repository:

```text
etf_analyst_report_LAST_WORKING_PLUS_INVESCO_BROWSER.py
send_etf_report_email.py
requirements.txt
.github/workflows/weekly_etf_report.yml
```

## GitHub Secrets

In GitHub, go to:

```text
Repo → Settings → Secrets and variables → Actions → New repository secret
```

Add these secrets:

```text
SMTP_HOST       smtp.gmail.com
SMTP_PORT       465
SMTP_USER       your sender email, for example your Gmail address
SMTP_PASSWORD   your email app password, not your normal email password
EMAIL_FROM      same as SMTP_USER, or any sender address your SMTP account allows
```

For Gmail, create an App Password in your Google Account security settings, then use that app password as `SMTP_PASSWORD`.

## Schedule

The workflow is set to:

```yaml
schedule:
  - cron: "0 22 * * 5"
    timezone: "America/Toronto"
```

That means Friday 10:00 PM Toronto time.

You can also run it manually from:

```text
GitHub repo → Actions → Weekly ETF Analyst Report → Run workflow
```

## Local test before pushing

From the repo folder:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
python etf_analyst_report_LAST_WORKING_PLUS_INVESCO_BROWSER.py
```

To test email locally, set environment variables first, then run:

```bash
python send_etf_report_email.py
```
