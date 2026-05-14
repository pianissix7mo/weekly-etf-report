# ETF Analyst Report Automation

A Python-based ETF analytics workflow that collects ETF holdings, analyst estimates, valuation metrics, and price data, then generates a structured Excel report for investment research. The project is designed to be automated with GitHub Actions so a fresh report can be produced and emailed on a schedule.

> **Disclaimer:** This project is for research and educational purposes only. It is not financial advice or a recommendation to buy or sell any security.

---

## Project Overview

ETF investors often need to compare valuation, analyst expectations, and holdings exposure across multiple funds. However, this information is usually scattered across ETF provider websites, Yahoo Finance, downloaded holding files, and manual spreadsheets.

This project automates that workflow by:

* Pulling ETF holdings from provider pages or downloadable files
* Collecting stock-level metrics such as price, P/E ratio, forward P/E, analyst target price, and EPS estimates
* Aggregating stock-level data into ETF-level summary metrics
* Calculating weighted and median valuation/return measures
* Tracking historical P/E data over time
* Creating a multi-sheet Excel report for non-technical users
* Supporting scheduled execution through GitHub Actions
* Sending the generated Excel report by email

The goal is to turn a repetitive manual investment research process into a reproducible, automated reporting pipeline.

---

## Key Features

### 1. ETF Holdings Collection

The workflow collects ETF holdings from multiple sources, including ETF provider websites and downloadable holdings files. Each ETF receives its own detailed sheet in the final Excel workbook.

Supported examples include:

* QQQ
* SPMO
* SMH
* Other ETFs that provide accessible holdings data

The code is designed to handle common issues in ETF holdings data, such as different ticker formats, duplicate sheet names, provider-specific layouts, and incomplete rows.

---

### 2. Stock-Level Analyst and Valuation Metrics

For each holding, the project attempts to collect or calculate key metrics, including:

* Current stock price
* Trailing P/E ratio
* Forward P/E ratio
* Analyst target price
* Estimated EPS
* Analyst target return
* Holding weight inside the ETF

These stock-level metrics are then used to build ETF-level summary statistics.

---

### 3. ETF-Level Summary Report

The first sheet of the Excel workbook provides a high-level ETF comparison table. It includes metrics such as:

* Weighted average analyst target return
* Median analyst target return
* Weighted P/E ratio
* Median P/E ratio
* Forward P/E ratio
* Estimated EPS coverage
* Raw covered weight / reliability score
* Number of holdings analyzed

This summary page is designed for quick comparison across ETFs.

---

### 4. Detailed ETF Sheets

Each ETF receives a separate worksheet with stock-level details. These sheets make it easy to inspect which holdings are driving the ETF-level results.

Each ETF sheet includes information such as:

* Ticker
* Company name
* Holding weight
* Price
* Analyst target price
* Analyst target return
* P/E ratio
* Forward P/E ratio
* EPS estimate
* Data coverage indicators

The Excel formatting highlights important metric blocks so the report is easier to read for users without programming experience.

---

### 5. ETF Overlap Analysis

The report includes an ETF overlap worksheet that compares two selected ETFs and calculates their shared exposure.

The overlap calculation uses the minimum shared weight for each common holding.

For example:

| Holding           | ETF A Weight | ETF B Weight | Overlap Contribution |
| ----------------- | -----------: | -----------: | -------------------: |
| NVDA              |          60% |          40% |                  40% |
| META              |          40% |          60% |                  40% |
| **Total Overlap** |              |              |              **80%** |

This allows users to understand whether two ETFs are truly diversified or mostly holding the same stocks.

---

### 6. Historical P/E Tracking

The project saves historical ETF P/E data so valuation changes can be monitored over time.

Instead of only creating a one-time report, the workflow can preserve a growing history file that supports longer-term analysis, such as:

* Whether an ETF is becoming more expensive or cheaper
* How current valuation compares with recent history
* How P/E trends differ across ETFs

---

### 7. GitHub Actions Automation

The project can be scheduled to run automatically using GitHub Actions.

The automation can:

* Run the ETF report script on a schedule
* Generate the latest Excel report
* Save selected historical files back to the repository
* Email the Excel report to selected recipients

This makes the workflow useful for recurring weekly or daily monitoring.

---

## Example Output

The final output is an Excel workbook with multiple sheets:

```text
ETF_Analyst_Report.xlsx
│
├── Summary
├── ETF Overlap
├── QQQ
├── SPMO
├── SMH
├── ...
```

The workbook is designed for both technical and non-technical users. A user can open the file directly in Excel and review the ETF summaries, detailed holdings, and overlap calculations without running Python code.

---

## Tech Stack

* **Python**: Core data pipeline and report generation
* **pandas / NumPy**: Data cleaning, transformation, and aggregation
* **yfinance**: Market and analyst estimate data
* **requests / BeautifulSoup**: Web data collection
* **Playwright**: Browser automation for dynamic ETF holdings pages
* **openpyxl / XlsxWriter**: Excel workbook creation and formatting
* **GitHub Actions**: Scheduled automation
* **SMTP**: Automated email delivery

---

## Repository Structure

```text
weekly-etf-report/
│
├── etf_analyst_report_complete.py       # Main report-generation script
├── send_etf_report_email.py             # Sends latest Excel report by email
├── pe_history/                          # Historical ETF valuation data
├── etf_analyst_target_outputs/          # Generated Excel reports
├── .github/
│   └── workflows/
│       └── etf_report.yml               # GitHub Actions workflow
├── requirements.txt                     # Python dependencies
└── README.md
```

File names may vary depending on the version of the project.

---

## Installation

Clone the repository:

```bash
git clone https://github.com/your-username/weekly-etf-report.git
cd weekly-etf-report
```

Create and activate a virtual environment:

```bash
python -m venv .venv
```

On Windows:

```bash
.venv\Scripts\activate
```

On macOS or Linux:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

If using Playwright for dynamic ETF pages, install the browser dependency:

```bash
python -m playwright install chromium
```

---

## Environment Variables

The email script uses SMTP credentials stored as environment variables.

Required variables:

```text
SMTP_HOST       Example: smtp.gmail.com
SMTP_PORT       Example: 465
SMTP_USER       Sender email address
SMTP_PASSWORD   App password or SMTP password
```

Optional variables:

```text
EMAIL_FROM      Sender shown in the email
EMAIL_TO        Comma-separated recipient emails
REPORT_DIR      Folder containing generated Excel reports
```

When running through GitHub Actions, these should be stored as GitHub repository secrets rather than hardcoded in the source code.

---

## How to Run Locally

Generate the ETF analyst report:

```bash
python etf_analyst_report_complete.py
```

Send the latest generated report by email:

```bash
python send_etf_report_email.py
```

After running, the generated Excel report will be saved in the output folder.

---

## GitHub Actions Workflow

The project can be automated with a workflow similar to this:

```yaml
name: ETF Analyst Report

on:
  workflow_dispatch:
  schedule:
    - cron: "0 0 * * 2-6"

permissions:
  contents: write

jobs:
  run-report:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          python -m playwright install --with-deps chromium

      - name: Run ETF analyst report
        run: python etf_analyst_report_complete.py

      - name: Send report email
        env:
          SMTP_HOST: ${{ secrets.SMTP_HOST }}
          SMTP_PORT: ${{ secrets.SMTP_PORT }}
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
          EMAIL_TO: ${{ secrets.EMAIL_TO }}
        run: python send_etf_report_email.py
```

---

## Methodology

### Weighted ETF Metrics

For ETF-level statistics, the project aggregates stock-level metrics using ETF holding weights.

A simplified example:

```text
Weighted ETF Metric = Sum(Stock Metric × Holding Weight) / Sum(Covered Holding Weight)
```

This helps avoid treating small holdings and large holdings as equally important.

---

### Median Metrics

The report also includes median values to reduce the impact of outliers. This is useful because a few companies with extremely high or low P/E ratios can distort weighted averages.

Using both weighted averages and medians provides a more balanced view of ETF valuation.

---

### Reliability / Raw Covered Weight

Not every holding always has complete analyst or valuation data. The report includes a coverage indicator to show how much of the ETF was actually covered by available data.

A higher covered weight means the ETF-level estimate is based on more complete information.

---

### ETF Overlap Formula

ETF overlap is calculated by comparing the common holdings between two ETFs and summing the smaller weight for each shared ticker.

```text
Overlap = Sum(min(Weight in ETF A, Weight in ETF B)) for all shared holdings
```

This gives a practical estimate of how much exposure the two ETFs have in common.

---

## Challenges Solved

This project addresses several real-world data engineering and analytics challenges:

* ETF providers publish holdings in inconsistent formats
* Some ETF websites require browser automation to access full holdings
* Ticker symbols may need cleaning before being used with financial APIs
* Analyst and valuation data can be missing or inconsistent
* Excel sheet names must be cleaned to avoid duplicate or invalid names
* Weighted metrics require careful handling of missing data
* Automated emails require secure credential management
* GitHub Actions uses UTC time, so scheduled runs must account for local time zones

---

## Skills Demonstrated

This project demonstrates practical experience in:

* Financial data analysis
* Data cleaning and transformation
* Web scraping and browser automation
* API-based data collection
* Excel reporting automation
* GitHub Actions CI/CD scheduling
* Secure environment variable management
* Reproducible data pipelines
* Designing reports for non-technical stakeholders

---

## Future Improvements

Potential improvements include:

* Add a Streamlit or desktop application interface
* Add more ETF providers and asset classes
* Store historical data in SQLite or PostgreSQL
* Add charts for valuation and target return trends
* Improve error logging and retry handling
* Add unit tests for overlap and weighted metric calculations
* Add a dashboard version of the Excel summary
* Add configurable ETF lists through a simple YAML or JSON file

---

## Resume Summary

This project can be summarized on a resume as:

> Built an automated ETF analytics pipeline in Python that collects ETF holdings, analyst estimates, valuation metrics, and price data, then generates a multi-sheet Excel report with weighted valuation, target return, coverage, historical P/E tracking, and ETF overlap analysis. Automated scheduled execution and email delivery using GitHub Actions and SMTP.

---

## Author

**Mofei Wang**
Data Science and Analytics
GitHub: [https://github.com/pianissix7mo](https://github.com/pianissix7mo)
