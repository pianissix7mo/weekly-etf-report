"""Run-wide Yahoo transport controls. yfinance uses this session for cookie,
crumb, chart and fundamentals requests.
"""
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from random import uniform
from threading import Lock, local
from time import monotonic, sleep
from urllib.parse import urlsplit

from curl_cffi.requests import Session
from curl_cffi.requests.exceptions import Timeout


class YahooRequestError(RuntimeError):
    pass


def endpoint_family(url):
    path = urlsplit(url).path.lower()
    for name in ("quotesummary", "fundamentals-timeseries", "getcrumb", "/chart/", "/quote"):
        if name in path:
            return name.strip("/")
    return "session"


def retry_after(headers):
    value = headers.get("Retry-After")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        if value:
            try:
                return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        return 0.0


class YahooSession(Session):
    """Serialize wire requests and share pacing and circuit state across workers."""

    def __init__(self, interval=0.5):
        super().__init__(timeout=12)
        self.interval = interval
        self.lock = Lock()
        self.context = local()
        self.reset()

    def reset(self):
        with self.lock:
            self.next_request = 0.0
            self.open_reason = None
            self.blocked = set()
            self.auth_failures = Counter()
            self.rate_failures = 0
            self.metrics = Counter()

    def begin_symbol(self):
        self.context.errors = set()

    def symbol_errors(self):
        return sorted(getattr(self.context, "errors", ()))

    def _error(self, family, reason):
        errors = getattr(self.context, "errors", None)
        if errors is not None:
            errors.add(f"{family}: {reason}")
        return YahooRequestError(f"{family}: {reason}")

    def request(self, method, url, **kwargs):
        family = endpoint_family(url)
        kwargs["timeout"] = min(kwargs.get("timeout") or 12, 12)
        # Hold the lock through the short backoff so other workers also honor it.
        with self.lock:
            reason = self.open_reason or ("authentication circuit open" if family in self.blocked else None)
            if reason:
                self.metrics[f"{family} circuit skip"] += 1
                raise self._error(family, reason)
            for attempt in range(2):
                delay = self.next_request - monotonic()
                if delay > 0:
                    sleep(delay)
                self.next_request = monotonic() + self.interval
                self.metrics[f"{family} request"] += 1
                response = None
                try:
                    response = super().request(method, url, **kwargs)
                except Timeout:
                    kind = "timeout"
                else:
                    status = response.status_code
                    self.metrics[f"{family} HTTP {status}"] += 1
                    # The cookie bootstrap normally receives a 404 carrying a cookie.
                    if status < 400 or (status == 404 and urlsplit(url).hostname == "fc.yahoo.com"):
                        if family == "getcrumb" and not response.text.strip():
                            raise self._error(family, "empty crumb")
                        self.auth_failures[family] = 0
                        if family not in {"getcrumb", "session"}:
                            self.rate_failures = 0
                        return response
                    kind = f"HTTP {status}"
                    if status in (401, 403):
                        self.auth_failures[family] += 1
                        if self.auth_failures[family] >= 2:
                            if family in {"getcrumb", "session"}:
                                self.open_reason = "Yahoo authentication circuit open"
                            else:
                                self.blocked.add(family)
                        # yfinance normally switches cookie strategies on HTTP failures.
                        # Surface a denial before it can launch another request.
                        raise self._error(family, kind)
                    if status == 429:
                        self.rate_failures += 1
                    elif status not in (500, 502, 503, 504):
                        raise self._error(family, kind)
                if kind == "timeout":
                    self.metrics[f"{family} timeout"] += 1
                wait = max(2 ** attempt + uniform(0.1, 0.5),
                           retry_after(response.headers) if response is not None else 0.0)
                if kind == "HTTP 429" and (self.rate_failures >= 2 or wait > 30):
                    self.open_reason = "Yahoo rate-limit circuit open"
                    raise self._error(family, kind)
                self.next_request = max(self.next_request, monotonic() + wait)
                if attempt == 0 and wait <= 30:
                    self.metrics[f"{family} retry"] += 1
                    continue
                raise self._error(family, kind)


YAHOO_SESSION = YahooSession()


def fetch_quote_summary(symbol):
    """Fetch the same financialData module used for yfinance analyst targets,
    alongside valuation and earnings trends, in a single quoteSummary request.
    """
    from yfinance.data import YfData

    payload = YfData(session=YAHOO_SESSION).get_raw_json(
        "https://query2.finance.yahoo.com/v10/finance/quoteSummary/" + symbol,
        params={"modules": "financialData,defaultKeyStatistics,summaryDetail,quoteType,earningsTrend",
                "symbol": symbol, "formatted": "false", "corsDomain": "finance.yahoo.com"},
    )
    envelope = payload.get("quoteSummary") if isinstance(payload, dict) else None
    if not isinstance(envelope, dict):
        raise YahooRequestError("quote summary: missing response envelope")
    if envelope.get("error"):
        raise YahooRequestError("quote summary: provider error payload")
    results = envelope.get("result")
    if not isinstance(results, list) or not results:
        raise YahooRequestError("quote summary: empty response")
    if len(results) != 1 or not isinstance(results[0], dict):
        raise YahooRequestError("quote summary: invalid response")
    result = results[0]
    quote_type = result.get("quoteType")
    returned_symbol = quote_type.get("symbol") if isinstance(quote_type, dict) else None
    if not isinstance(returned_symbol, str) or returned_symbol.upper() != symbol:
        raise YahooRequestError("quote summary: listing identity missing or mismatched")
    return result


def yahoo_number(value):
    return value.get("raw") if isinstance(value, dict) else value
