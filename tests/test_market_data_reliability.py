"""Transport and provider regressions without live credentials or email."""
from collections import Counter
from types import SimpleNamespace

import pandas as pd
import pytest
from curl_cffi.requests import Session
from curl_cffi.requests.exceptions import Timeout

import etf_analyst_report_complete as pipeline
from etf_report import prices, yahoo
from etf_report.backup import FMPBackup
from etf_report.delivery import MANIFEST_NAME, load_delivery, write_delivery_manifest
from etf_report.errors import ReportIncompleteError


QUOTE = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/TSLA"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/TSLA"


def transport(monkeypatch, outcomes):
    clock = [10.0]
    sent = []

    def sleep(seconds):
        clock[0] += seconds

    def request(self, method, url, **kwargs):
        sent.append((url, clock[0], kwargs["timeout"]))
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        status, headers, *body = outcome
        return SimpleNamespace(status_code=status, headers=headers, text=body[0] if body else "")

    monkeypatch.setattr(Session, "request", request)
    monkeypatch.setattr(yahoo, "monotonic", lambda: clock[0])
    monkeypatch.setattr(yahoo, "sleep", sleep)
    monkeypatch.setattr(yahoo, "uniform", lambda *_: 0.25)
    return yahoo.YahooSession(), sent


def test_401_is_not_retried_and_circuit_is_scoped_to_denied_family(monkeypatch):
    client, sent = transport(monkeypatch, iter([(401, {}), (401, {}), (200, {})]))
    for _ in range(2):
        with pytest.raises(yahoo.YahooRequestError, match="401"):
            client.get(QUOTE)
    with pytest.raises(yahoo.YahooRequestError, match="authentication circuit open"):
        client.get(QUOTE)
    assert client.get(CHART).status_code == 200
    assert [url for url, _, _ in sent] == [QUOTE, QUOTE, CHART]
    assert [moment for _, moment, _ in sent] == [10, 10.5, 11]


def test_429_honors_retry_after_then_opens_circuit_globally(monkeypatch):
    client, sent = transport(monkeypatch, iter([(429, {"Retry-After": "3"}), (429, {})]))
    with pytest.raises(yahoo.YahooRequestError, match="429"):
        client.get(QUOTE, timeout=30)
    with pytest.raises(yahoo.YahooRequestError, match="rate-limit circuit open"):
        client.get(CHART)
    assert sent == [(QUOTE, 10, 12), (QUOTE, 13, 12)]
    assert client.metrics["quotesummary retry"] == 1


def test_rate_limit_text_in_crumb_response_is_not_treated_as_a_credential(monkeypatch):
    client, sent = transport(monkeypatch, iter([(200, {}, "Too Many Requests")] * 2))
    crumb = "https://query1.finance.yahoo.com/v1/test/getcrumb"
    with pytest.raises(yahoo.YahooRequestError, match="429"):
        client.get(crumb)
    with pytest.raises(yahoo.YahooRequestError, match="rate-limit circuit open"):
        client.get(CHART)
    assert len(sent) == 2
    assert client.metrics["getcrumb rate-limit payload"] == 2


def test_timeout_retries_with_jitter_and_does_not_authenticate_again(monkeypatch):
    client, sent = transport(monkeypatch, iter([Timeout("timed out"), (200, {})]))
    assert client.get(CHART).status_code == 200
    assert [moment for _, moment, _ in sent] == [10, 11.25]
    assert client.metrics["chart timeout"] == 1


def test_long_retry_after_stops_run_without_ignoring_limit(monkeypatch):
    client, sent = transport(monkeypatch, iter([(429, {"Retry-After": "180"})]))
    with pytest.raises(yahoo.YahooRequestError, match="429"):
        client.get(QUOTE)
    with pytest.raises(yahoo.YahooRequestError, match="rate-limit circuit open"):
        client.get(CHART)
    assert len(sent) == 1


@pytest.mark.parametrize(("result", "message"), [
    ([], "empty response"),
    ([{"quoteType": {"symbol": "GOOG"}}], "listing identity"),
])
def test_quote_summary_rejects_empty_or_wrong_share_class(monkeypatch, result, message):
    from yfinance.data import YfData

    monkeypatch.setattr(YfData, "get_raw_json",
                        lambda *a, **kw: {"quoteSummary": {"result": result, "error": None}})
    with pytest.raises(yahoo.YahooRequestError, match=message):
        yahoo.fetch_quote_summary("GOOGL")


def test_fmp_uses_named_fields_and_verified_quarters(monkeypatch):
    backup = FMPBackup()
    monkeypatch.setenv("FMP_API_KEY", "fixture")
    payload = {
        "profile": [{"symbol": "TSLA", "country": "US", "currency": "USD", "isAdr": False}],
        "price-target-consensus": [
            {"symbol": "GOOGL", "targetLow": 20, "targetHigh": 40, "targetConsensus": 30},
            {"symbol": "TSLA", "targetLow": 200, "targetHigh": 400, "targetMedian": 280, "targetConsensus": 300},
        ],
        "ratios-ttm": [{"symbol": "TSLA", "priceToEarningsRatioTTM": 27, "forwardPriceToEarningsGrowthRatioTTM": 450}],
        "historical-price-eod/non-split-adjusted": [
            {"symbol": "TSLA", "date": "2026-11-18", "adjClose": 275},
            {"symbol": "TSLA", "date": "2026-11-19", "adjClose": 280, "close": 5},
        ],
        "historical-price-eod/dividend-adjusted": [
            {"symbol": "TSLA", "date": "2026-11-19", "adjClose": 280, "close": 5},
            {"symbol": "TSLA", "date": "2026-11-20", "adjClose": 500},
        ],
        "analyst-estimates": [
            {"symbol": "TSLA", "date": day, "epsAvg": 2, "numAnalystsEps": 9}
            for day in ("2026-12-31", "2027-03-31", "2027-06-30", "2027-09-30")
        ],
    }

    def get(url, **kwargs):
        assert kwargs["params"]["apikey"] == "fixture"
        data = payload[url.split("/stable/")[1]]
        return SimpleNamespace(status_code=200, json=lambda: data)

    monkeypatch.setattr("etf_report.backup.requests.get", get)
    assert backup.targets("TSLA")["Target Mean"] == 300
    assert backup.targets("TSLA")["Target Median"] == 280
    assert backup.targets("GOOG") == {}
    assert backup.trailing_pe("TSLA") == 27
    assert backup.close("TSLA", "2026-11-19") == 280
    assert backup.close("TSLA", "2026-11-20") is None
    assert backup.adjusted_history("TSLA", "2026-11-18", "2026-11-19").tolist() == [280]
    assert backup.forward_pe("TSLA", 280, "2026-11-19") == 35
    payload["analyst-estimates"][1]["date"] = "2026-12-31"
    assert backup.forward_pe("TSLA", 280, "2026-11-19") is None
    payload["profile"][0]["isAdr"] = True
    assert backup.forward_pe("TSLA", 280, "2026-11-19") is None


@pytest.mark.parametrize(("returned_symbol", "country", "currency", "price"), [
    ("BESI.AS", "NL", "EUR", 150.8),
    ("BESI", "US", "USD", None),
    ("BESI.AS", "NL", "USD", None),
])
def test_foreign_fmp_backup_requires_exact_listing_and_currency(
        monkeypatch, returned_symbol, country, currency, price):
    monkeypatch.setenv("FMP_API_KEY", "fixture")
    sent = []

    def get(url, **kwargs):
        assert kwargs["params"]["symbol"] == "BESI.AS"
        endpoint = url.split("/stable/")[1]
        sent.append(endpoint)
        rows = (
            [{"symbol": returned_symbol, "country": country, "currency": currency, "isAdr": False}]
            if endpoint == "profile" else [{"symbol": "BESI.AS", "date": "2026-11-19", "adjClose": 150.8}]
        )
        return SimpleNamespace(status_code=200, json=lambda: rows)

    monkeypatch.setattr("etf_report.backup.requests.get", get)
    assert FMPBackup().close("BESI.AS", "2026-11-19") == price
    assert sent == (["profile", "historical-price-eod/non-split-adjusted"] if price else ["profile"])


def test_successful_fallback_combines_with_yahoo_and_reuses_one_chart(monkeypatch):
    monkeypatch.setattr(pipeline, "YAHOO_TARGET_CACHE", {})
    monkeypatch.setattr(pipeline, "YAHOO_FAILURES", {})
    monkeypatch.setattr(pipeline, "YAHOO_FETCH_TIMINGS", {})
    monkeypatch.setattr(pipeline, "YAHOO_MAX_WORKERS", 1)
    histories = Counter()
    fundamentals = Counter()
    session = prices.expected_completed_session_date("TSLA")
    year = pd.Timestamp(session).year
    current_end = prices.expected_completed_session_date("TSLA", now=f"{year-1}-12-31 23:00")
    prior_end = prices.expected_completed_session_date("TSLA", now=f"{year-2}-12-31 23:00")
    rows = pd.DataFrame({"Close": [80, 90, 100], "Adj Close": [80, 90, 100]},
                        index=pd.to_datetime([prior_end, current_end, session]))

    class Ticker:
        def __init__(self, symbol, **kwargs):
            assert kwargs["session"] is pipeline.YAHOO_SESSION
            self.symbol = symbol

        def history(self, **kwargs):
            histories[self.symbol] += 1
            return pd.DataFrame() if self.symbol == "TSLA" else rows.copy()

        def get_income_stmt(self, **kwargs):
            return pd.DataFrame()

    def get_fundamentals(symbol):
        fundamentals[symbol] += 1
        if symbol == "TSLA":
            raise yahoo.YahooRequestError("quote summary: empty response")
        return {
            "financialData": {"targetLowPrice": 85, "targetMeanPrice": 100, "targetHighPrice": 115},
            "summaryDetail": {"trailingPE": 21, "forwardPE": 20},
        }

    monkeypatch.setattr(pipeline.yf, "Ticker", Ticker)
    monkeypatch.setattr(pipeline, "fetch_quote_summary", get_fundamentals)
    monkeypatch.setattr(pipeline.FMP, "close", lambda *a: 100)
    monkeypatch.setattr(pipeline.FMP, "adjusted_history", lambda *a: rows["Adj Close"])
    monkeypatch.setattr(pipeline.FMP, "trailing_pe", lambda *a: 27)
    monkeypatch.setattr(pipeline.FMP, "forward_pe", lambda *a: 23)
    monkeypatch.setattr(pipeline.FMP, "targets", lambda *a: {
        "Target Low": 90, "Target Mean": 105, "Target High": 120, "Target Source": "FMP fixture",
    })

    holdings = pd.DataFrame({"Yahoo Ticker": ["AAPL", "TSLA"], "Weight": [80, 20], "Weight Decimal": [.8, .2]})
    detail = pipeline.add_yahoo_targets(holdings).set_index("Yahoo Ticker")
    assert detail.loc["AAPL", "Target Mean"] == 100
    assert detail.loc["TSLA", "Target Mean"] == 105
    assert detail.loc["TSLA", "Price Source"] == "FMP unadjusted end-of-day"
    assert detail.loc["TSLA", "History Source"] == "FMP dividend/split-adjusted end-of-day"
    assert detail.loc["TSLA", "YTD Return"] == pytest.approx(100 / 90 - 1)
    assert detail.loc["TSLA", "PE Retrieved UTC"]
    assert "empty response" in detail.loc["TSLA", "Data Diagnostic"]
    assert not detail["Yahoo Error"].notna().any()
    assert set(pipeline.YAHOO_TARGET_CACHE) == {"AAPL", "TSLA"}
    pipeline.add_yahoo_targets(holdings)
    assert histories == fundamentals == {"AAPL": 1, "TSLA": 1}


def test_failure_revokes_prior_manifest_and_cannot_deliver(tmp_path, monkeypatch):
    report = tmp_path / "ETF_analyst_report.xlsx"
    report.write_bytes(b"previous report")
    write_delivery_manifest(report)
    monkeypatch.setattr(pipeline, "REPORT_PATH", report)
    monkeypatch.setattr(pipeline, "ETFS", ["QQQ"])

    def fail(*args):
        raise ReportIncompleteError("major holding missing analyst targets: TSLA")

    def unexpected(*args, **kwargs):
        pytest.fail("failed report cannot write workbook or PE history")

    monkeypatch.setattr(pipeline, "run_one_etf", fail)
    monkeypatch.setattr(pipeline, "export_excel_report", unexpected)
    monkeypatch.setattr(pipeline, "update_pe_history", unexpected)
    with pytest.raises(ReportIncompleteError, match="QQQ"):
        pipeline.main_with_excel()
    assert report.read_bytes() == b"previous report"
    assert not (tmp_path / MANIFEST_NAME).exists()
    with pytest.raises(RuntimeError, match="delivery validation failed"):
        load_delivery(tmp_path)
