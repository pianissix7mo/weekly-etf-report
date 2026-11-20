"""Optional, licensed Financial Modeling Prep fallback.

The Stable API returns listing symbols in each record. Foreign listings
also require a matching currency, country and non-ADR company profile.
"""
import os
import re
from datetime import date
from threading import Lock

import pandas as pd
import requests

from etf_report.target_quality import complete_target_trio, positive_float


# Exact Yahoo suffixes only. London is excluded because GBP and GBp quote
# units are not interchangeable and the profile does not prove the target unit.
FOREIGN_MARKETS = {
    ".TO": ("CA", "CAD"),
    ".TW": ("TW", "TWD"),
    ".KS": ("KR", "KRW"),
    ".T": ("JP", "JPY"),
    ".HK": ("HK", "HKD"),
    ".AS": ("NL", "EUR"),
    ".DE": ("DE", "EUR"),
    ".SW": ("CH", "CHF"),
    ".PA": ("FR", "EUR"),
}


class FMPBackup:
    def __init__(self):
        self.lock = Lock()
        self.reset()

    def reset(self):
        self.blocked = set()
        self.profiles = {}
        self.profile_failures = set()

    def _records(self, endpoint, symbol, **params):
        key = os.getenv("FMP_API_KEY")
        if not key or not re.fullmatch(r"[A-Z0-9]{1,8}(?:-[A-Z])?(?:\.[A-Z]{1,2})?", symbol):
            return []
        if endpoint != "profile" and not self._verified_listing(symbol):
            return []
        with self.lock:
            if "*" in self.blocked or endpoint in self.blocked:
                return []
            try:
                response = requests.get(
                    f"https://financialmodelingprep.com/stable/{endpoint}",
                    params={"symbol": symbol, "apikey": key, **params},
                    timeout=12,
                )
                status = response.status_code
                if status in (401, 429):
                    self.blocked.add("*")
                elif status in (402, 403):
                    self.blocked.add(endpoint)
                if status != 200:
                    print(f"FMP {endpoint} {symbol}: HTTP {status}")
                    return []
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                # Request exceptions can include the API key in the URL.
                print(f"FMP {endpoint} {symbol}: {type(error).__name__}")
                return []
        rows = [row for row in payload if isinstance(row, dict) and row.get("symbol") == symbol] if isinstance(payload, list) else []
        if not rows:
            print(f"FMP {endpoint} {symbol}: empty or listing mismatch")
        return rows

    def _profile(self, symbol):
        if symbol in self.profiles:
            return self.profiles[symbol]
        if symbol in self.profile_failures:
            return {}
        rows = self._records("profile", symbol)
        if len(rows) == 1 and isinstance(rows[0].get("country"), str) and isinstance(rows[0].get("currency"), str):
            self.profiles[symbol] = rows[0]
            return rows[0]
        self.profile_failures.add(symbol)
        return {}

    def _verified_listing(self, symbol):
        if "." not in symbol:
            return True
        _, suffix = symbol.rsplit(".", 1)
        expected = FOREIGN_MARKETS.get("." + suffix)
        if expected is None:
            return False
        profile = self._profile(symbol)
        return profile.get("isAdr") is False and (profile.get("country"), profile.get("currency")) == expected

    def close(self, symbol, session):
        rows = self._records("historical-price-eod/non-split-adjusted", symbol, **{"from": session, "to": session})
        matches = [r for r in rows if r.get("date") == session]
        # The non-split-adjusted endpoint names its raw session close adjClose.
        # The same spelling is dividend-adjusted on the separate history route.
        return positive_float(matches[0].get("adjClose")) if len(matches) == 1 else None

    def adjusted_history(self, symbol, start, end):
        rows = self._records("historical-price-eod/dividend-adjusted", symbol, **{"from": start, "to": end})
        values = {}
        for row in rows:
            day = record_date(row)
            value = positive_float(row.get("adjClose"))
            if day and value is not None and start <= day.isoformat() <= end:
                if day in values:
                    return pd.Series(dtype=float)
                values[day] = value
        return pd.Series(values, dtype=float).rename(index=pd.Timestamp).sort_index()

    def targets(self, symbol):
        rows = self._records("price-target-consensus", symbol)
        if len(rows) != 1:
            return {}
        target = rows[0]
        fields = {
            "Target Low": positive_float(target.get("targetLow")),
            "Target Mean": positive_float(target.get("targetConsensus")),
            "Target High": positive_float(target.get("targetHigh")),
            "Target Median": positive_float(target.get("targetMedian")),
        }
        if not complete_target_trio(fields):
            return {}
        return {**fields, "Analyst Count": None, "Target Source": "FMP price target consensus (provider timestamp unavailable)"}

    def trailing_pe(self, symbol):
        rows = self._records("ratios-ttm", symbol)
        return positive_float(rows[0].get("priceToEarningsRatioTTM")) if len(rows) == 1 else None

    def forward_pe(self, symbol, close, session):
        if positive_float(close) is None or session is None:
            return None
        # US-only because the quarterly estimates payload has no reporting/quote
        # currency metadata with which to validate a foreign EPS denominator.
        if "." in symbol or not os.getenv("FMP_API_KEY"):
            return None
        profile = self._profile(symbol)
        if profile.get("currency") != "USD" or profile.get("country") != "US" or profile.get("isAdr") is not False:
            return None
        session_date = date.fromisoformat(session)
        rows = self._records("analyst-estimates", symbol, period="quarter", page=0, limit=20)
        quarters = sorted(
            [(day, positive_float(row.get("numAnalystsEps")), row)
             for row in rows if (day := record_date(row)) and day > session_date],
            key=lambda entry: entry[0],
        )[:4]
        if len(quarters) != 4 or (quarters[0][0] - session_date).days > 100:
            return None
        if any(not 75 <= (b[0] - a[0]).days <= 105 for a, b in zip(quarters, quarters[1:])):
            return None
        estimates = []
        for _, count, row in quarters:
            eps = row.get("epsAvg")
            if not count or isinstance(eps, bool) or not isinstance(eps, (float, int)):
                return None
            estimates.append(eps)
        earnings = positive_float(sum(estimates))
        return close / earnings if earnings else None


def record_date(row):
    try:
        return date.fromisoformat(row["date"])
    except (KeyError, TypeError, ValueError):
        return None


FMP = FMPBackup()
