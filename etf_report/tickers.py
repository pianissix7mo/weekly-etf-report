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
    "analog devices": "ADI", "nxp": "NXPI", "on semiconductor": "ON", "texas instruments": "TXN", "intel": "INTC", "synopsys": "SNPS", "cadence": "CDNS",
    "sk hynix": "000660.KS", "samsung electronics": "005930.KS", "disco corp": "6146.T", "advantest": "6857.T",
}


def map_name_to_yahoo(name):
    s = "" if pd.isna(name) else str(name).lower()
    for key, ticker in NAME_TO_YAHOO.items():
        if key in s:
            return ticker
    return None


def looks_like_bad_row(text):
    text = str(text).lower()
    bad_terms = [
        "cash", "cash equivalent", "treasury", "t-bill", "t bill", "money market",
        "collateral", "repo", "repurchase", "total", "disclaimer", "receivable", "payable",
    ]
    return any(x in text for x in bad_terms)


def map_to_yahoo_symbol(raw_ticker, name="", identifier=""):
    raw = "" if pd.isna(raw_ticker) else str(raw_ticker).strip()
    name = "" if pd.isna(name) else str(name).strip()
    identifier = "" if pd.isna(identifier) else str(identifier).strip()
    combined = f"{raw} {name} {identifier}".lower()
    for cusip, yahoo_sym in CUSIP_TO_YAHOO.items():
        if cusip.lower() in combined:
            return yahoo_sym
    name_guess = map_name_to_yahoo(name)
    if name_guess:
        return name_guess
    s = raw.strip()
    if not s or s.lower() in ["nan", "none", "-", "--"]:
        return None
    s = s.replace(" Equity", "").replace(" Common Stock", "").replace("Class A", "").replace("Class C", "").strip()
    suffix_map = {"US": "", "CN": ".TO", "CT": ".TO", "TT": ".TW", "JT": ".T", "NA": ".AS", "GY": ".DE", "SW": ".SW", "LN": ".L", "HK": ".HK"}
    for suffix, yahoo_suffix in suffix_map.items():
        m = re.match(rf"^([A-Z0-9.\-]+)\s+{suffix}$", s, flags=re.I)
        if m:
            return m.group(1).replace(".", "-").upper() + yahoo_suffix
    m = re.match(r"^([0-9]+)\s+(KS|KP)$", s, flags=re.I)
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
    s = re.sub(r"\.(O|N|A)$", "", s)
    s = s.replace(".", "-").split()[0].strip().upper()
    if len(s) > 15 or looks_like_bad_row(f"{s} {name}"):
        return None
    return s
