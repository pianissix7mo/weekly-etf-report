import re

import pandas as pd

CUSIP_TO_YAHOO = {
    "037833100": "AAPL", "594918104": "MSFT", "02079K305": "GOOGL", "02079K107": "GOOG",
    "023135106": "AMZN", "30303M102": "META", "67066G104": "NVDA", "88160R101": "TSLA", "64110L106": "NFLX",
}

NAME_TO_YAHOO = {
    "apple": "AAPL", "microsoft": "MSFT", "alphabet": "GOOGL", "google": "GOOGL", "amazon": "AMZN", "meta platforms": "META", "facebook": "META",
    "netflix": "NFLX", "nvidia": "NVDA", "tesla": "TSLA", "broadcom": "AVGO", "taiwan semiconductor": "TSM", "tsmc": "TSM", "asml": "ASML",
    "advanced micro devices": "AMD", "amd": "AMD", "lam research": "LRCX", "applied materials": "AMAT", "kla": "KLAC", "arm holdings": "ARM",
    "qualcomm": "QCOM", "micron": "MU", "marvell": "MRVL", "monolithic power": "MPWR", "teradyne": "TER", "microchip technology": "MCHP",
    "analog devices": "ADI", "nxp": "NXPI", "on semiconductor": "ON", "texas instruments": "TXN", "intel": "INTC", "synopsys": "SNPS", "cadence design systems": "CDNS",
    "berkshire hathaway": "BRK-B", "sk hynix": "000660.KS", "samsung electronics": "005930.KS", "disco corp": "6146.T", "advantest": "6857.T",
}


def _normalize_company_text(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value).lower())).strip()


def map_name_to_yahoo(name):
    s = _normalize_company_text(name)
    if not s:
        return None
    for key, ticker in sorted(NAME_TO_YAHOO.items(), key=lambda item: len(item[0]), reverse=True):
        alias = _normalize_company_text(key)
        if re.search(rf"(?:^|\s){re.escape(alias)}(?:$|\s)", s):
            return ticker
    return None


def looks_like_bad_row(text):
    s = _normalize_company_text(text)
    if not s:
        return False
    bad_patterns = [
        r"\bcash\b", r"\bcash equivalents?\b", r"\btreasury\b",
        r"\bt bill\b", r"\bmoney market\b", r"\bcollateral\b",
        r"\brepo\b", r"\brepurchase\b", r"\btotal\b",
        r"\bdisclaimer\b", r"\breceivable\b", r"\bpayable\b",
    ]
    return any(re.search(pattern, s) for pattern in bad_patterns)


def _raw_to_yahoo_symbol(raw):
    s = "" if pd.isna(raw) else str(raw).strip()
    if not s or s.lower() in ["nan", "none", "-", "--"]:
        return None

    s = re.sub(r"\s+(?:Equity|Common Stock)$", "", s, flags=re.I).strip()
    suffix_map = {"US": "", "CN": ".TO", "CT": ".TO", "TT": ".TW", "JT": ".T", "JP": ".T", "NA": ".AS", "GY": ".DE", "SW": ".SW", "LN": ".L", "HK": ".HK"}
    for suffix, yahoo_suffix in suffix_map.items():
        m = re.fullmatch(rf"([A-Z0-9.\-]+)\s+{suffix}", s, flags=re.I)
        if m:
            return m.group(1).replace(".", "-").upper() + yahoo_suffix

    m = re.fullmatch(r"([0-9]+)\s+(KS|KP)", s, flags=re.I)
    if m:
        return m.group(1).zfill(6) + ".KS"

    exchange_prefix_map = {"KRX": ".KS", "TPE": ".TW", "TYO": ".T", "AMS": ".AS", "ETR": ".DE", "EPA": ".PA", "SWX": ".SW", "LON": ".L", "HKG": ".HK", "TSX": ".TO"}
    if ":" in s:
        prefix, sym = [x.strip() for x in s.split(":", 1)]
        prefix = prefix.upper()
        if prefix in exchange_prefix_map:
            if prefix == "KRX" and sym.isdigit():
                sym = sym.zfill(6)
            return sym.replace(".", "-").upper() + exchange_prefix_map[prefix]

    # Strip Reuters-style exchange suffixes before interpreting a dot as a
    # share-class separator (AAPL.O -> AAPL, while BRK.B -> BRK-B).
    s = re.sub(r"\.(O|N|A)$", "", s, flags=re.I)

    class_match = re.fullmatch(r"([A-Z]{1,6})[ .\-/]([A-Z])", s, flags=re.I)
    if class_match:
        return f"{class_match.group(1).upper()}-{class_match.group(2).upper()}"

    candidate = s.replace(".", "-").strip().upper()
    if candidate in {"UNKNOWN", "NAN", "NONE", "NULL", "N/A", "NA"}:
        return None
    # Preserve issuer-specific opaque security codes (for example DRAM's
    # private-company exposure identifiers) for portfolio-weight accounting.
    # The later Yahoo preflight decides whether a code is queryable.
    return candidate if re.fullmatch(r"[A-Z0-9-]{1,15}", candidate) else None


def map_to_yahoo_symbol(raw_ticker, name="", identifier=""):
    raw = "" if pd.isna(raw_ticker) else str(raw_ticker).strip()
    name = "" if pd.isna(name) else str(name).strip()
    identifier = "" if pd.isna(identifier) else str(identifier).strip()
    combined = f"{raw} {name} {identifier}".lower()

    for cusip, yahoo_sym in CUSIP_TO_YAHOO.items():
        if cusip.lower() in combined:
            return yahoo_sym

    if looks_like_bad_row(f"{raw} {name}"):
        return None

    raw_guess = _raw_to_yahoo_symbol(raw)
    if raw_guess:
        return raw_guess

    return map_name_to_yahoo(name)


YAHOO_SUFFIXES = {".TO", ".TW", ".T", ".AS", ".DE", ".SW", ".L", ".HK", ".KS", ".PA"}


def is_plausible_yahoo_symbol(symbol):
    """Cheap preflight filter for symbols that would otherwise waste Yahoo timeouts.

    This is intentionally conservative. It accepts normal US tickers/classes and
    exchange-suffixed international tickers, while rejecting obvious cash/fund/
    malformed artifacts seen in issuer holdings feeds.
    """
    if symbol is None:
        return False
    s = str(symbol).strip().upper()
    if not s or len(s) > 20 or "$" in s or " " in s:
        return False

    # Exchange-qualified symbols can legitimately use numeric bases (e.g. 005930.KS).
    for suffix in sorted(YAHOO_SUFFIXES, key=len, reverse=True):
        if s.endswith(suffix):
            base = s[:-len(suffix)]
            return bool(base) and bool(re.fullmatch(r"[A-Z0-9-]+", base))

    # Money-market / mutual-fund cash sweep tickers commonly end in XX/XXX and do
    # not provide the stock analyst data this report consumes.
    if len(s) >= 5 and s.endswith("XX"):
        return False

    # Keep bare numeric/alphanumeric symbols eligible. Some international issuer
    # feeds omit exchange suffixes; rejecting them here could reduce coverage.
    # Concurrency makes an occasional Yahoo miss cheap enough to preserve behavior.
    return bool(re.fullmatch(r"[A-Z0-9]{1,6}(?:-[A-Z0-9])?", s))
