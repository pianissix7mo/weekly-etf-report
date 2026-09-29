# ETF report architecture

This branch is a conservative refactor of the production report pipeline. The goal is maintainability without changing financial formulas or report semantics.

## Boundaries

- `etf_report/config.py`: ETF list, issuer metadata, runtime constants, and source URLs.
- `etf_report/tickers.py`: deterministic ticker normalization only; no network calls.
- `etf_report/analytics.py`: deterministic return and weighted P/E calculations only; no network calls.
- `etf_report/errors.py`: typed failure categories for CI diagnostics.
- `etf_analyst_report_complete.py`: orchestration, issuer adapters, Yahoo collection, sentiment data, history, and Excel rendering. These high-risk sections remain in place until covered by source fixtures.
- `tests/`: offline regression and contract tests.

## Refactor rule

A refactor is acceptable only if it preserves the existing financial calculation contract and keeps the pipeline fail-closed: if any required ETF fails, no partial report is exported or emailed.

## Next safe extraction candidates

1. Add saved issuer HTML/CSV/XLSX fixtures and parser tests.
2. Move issuer-specific source adapters into `etf_report/sources/` one issuer at a time.
3. Move Yahoo collection into `etf_report/market_data/yahoo.py` after mocked-response tests exist.
4. Move Excel rendering last, using workbook-level regression checks.

The production `main` branch remains unchanged while this work is validated in `refactor-safe`.
