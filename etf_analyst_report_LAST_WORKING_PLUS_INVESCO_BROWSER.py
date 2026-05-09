# ============================================================
# ETF HOLDINGS + YAHOO ANALYST TARGET XLSX REPORT
# Full replacement version
#
# Fixes included:
# 1) SMH: forces VanEck's real "% of Net Assets" column.
# 2) MAGS: no longer hard-codes 100/7 unless no real data source works.
#          It tries Roundhill downloads/tables, yfinance fund holdings,
#          and ETFDB before falling back to equal-weight approximation.
# 3) Safer weight-column detection: avoids using plain Net Assets,
#    Market Value, NAV, Notional Value, or Shares as portfolio weight.
# 4) Adds weighted trailing/fallback PE and weighted forward PE to Summary.
# 5) Jupyter-compatible. CSV outputs removed. Only saves final Excel workbook.
# ============================================================

import re
import time
import warnings
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")


# ============================================================
# SETTINGS
# ============================================================

ETFS = ["SPMO", "MAGS", "TECH.TO", "CHPS.TO", "SOXX", "SMH", "XLK", "QQQ"]

OUTPUT_DIR = Path("etf_analyst_target_outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

YAHOO_SLEEP_SECONDS = 0.25
INCLUDE_CASH_FUTURES_SWAPS = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

ETF_CONFIG = {
    "SPMO": {
        "issuer": "CompaniesMarketCap",
        "url": "https://companiesmarketcap.com/invesco-sp-500-momentum-etf/holdings/",
    },
    "QQQ": {
        "issuer": "CompaniesMarketCap",
        "url": "https://companiesmarketcap.com/invesco-qqq-trust/holdings/",
    },
    "MAGS": {
        "issuer": "Roundhill",
        "url": "https://www.roundhillinvestments.com/etf/mags/",
        "fallback_url": "https://etfdb.com/etf/MAGS/",
    },
    "TECH.TO": {
        "issuer": "Evolve ETFs",
        "csv_urls": [
            "https://evolveetfs.com/wp-content/uploads/holdings/TECH.csv",
            "https://evolveetfs.com/wp-content/uploads/holdings/TECH.CSV",
        ],
    },
    "CHPS.TO": {
        "issuer": "Global X Canada",
        "url": "https://www.globalx.ca/product/chps",
    },
    "SOXX": {
        "issuer": "iShares / BlackRock",
        "product_id": "239705",
        "file_name": "SOXX_holdings",
    },
    "SMH": {
        "issuer": "VanEck",
        "url": "https://www.vaneck.com/us/en/etf/equity/smh/holdings/download/xlsx/",
    },
    "XLK": {
        "issuer": "State Street / SSGA",
        "url": "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xlk.xlsx",
    },
}


# ============================================================
# COLUMN CANDIDATES
# ============================================================

TICKER_COLS = [
    "Ticker", "Ticker Symbol", "Symbol", "Trading Symbol",
    "Holding Ticker", "Bloomberg Ticker", "Exchange Ticker"
]

NAME_COLS = [
    "Name", "Holding", "Holdings", "Holding Name",
    "Security Name", "Security", "Description",
    "Company", "Company Name", "Issuer"
]

# IMPORTANT:
# Do NOT include plain "Net Assets" here.
# It can be a dollar asset column, not a portfolio percentage.
WEIGHT_COLS = [
    "Weight", "Weight (%)", "Weight %", "% Weight", "% Assets",
    "% of Assets", "% of Net Assets", "% of Net Asset",
    "ETF Weight", "Holding Percent", "Holding %", "Market Value Weight",
    "Fund Weight", "Portfolio Weight", "Holding Weight", "% of Fund"
]

SHARES_COLS = [
    "Shares", "Shares Held", "Quantity",
    "Shares/Par Value", "Par Value"
]

ID_COLS = ["Identifier", "FIGI", "CUSIP", "ISIN", "SEDOL"]


# ============================================================
# BASIC HELPERS
# ============================================================

def clean_col(c):
    return re.sub(r"\s+", " ", str(c).strip())


def make_unique_columns(cols):
    seen = {}
    out = []

    for c in cols:
        if isinstance(c, tuple):
            c = " ".join(str(x) for x in c if str(x) != "nan")

        c = clean_col(c)

        if c in seen:
            seen[c] += 1
            out.append(f"{c}.{seen[c]}")
        else:
            seen[c] = 0
            out.append(c)

    return out


def first_series(df, col):
    x = df[col]

    if isinstance(x, pd.DataFrame):
        x = x.iloc[:, 0]

    return x


def find_col(df, candidates, contains=None):
    lower_map = {clean_col(c).lower(): c for c in df.columns}

    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]

    if contains:
        for c in df.columns:
            lc = clean_col(c).lower()
            if any(x.lower() in lc for x in contains):
                return c

    return None


def find_weight_col_strict(df):
    """
    Safer weight-column picker.
    Avoids accidentally using Market Value / Net Assets / NAV / Shares columns.
    """
    cols = list(df.columns)

    bad_terms = [
        "market value",
        "notional value",
        "net asset value",
        "total net assets",
        "net assets value",
        "shares",
        "quantity",
        "price",
        "nav",
        "assets under",
        "identifier",
        "figi",
        "cusip",
        "isin",
        "sedol",
    ]

    preferred_exact = [
        "% of Net Assets",
        "% of Net Asset",
        "ETF Weight",
        "Holding Percent",
        "Holding %",
        "Weight",
        "Weight (%)",
        "Weight %",
        "% Weight",
        "% of Fund",
        "% of Assets",
        "% of Net Assets (%)",
        "Portfolio Weight",
        "Fund Weight",
        "Holding Weight",
    ]

    lower_map = {clean_col(c).lower(): c for c in cols}

    for name in preferred_exact:
        key = name.lower()
        if key in lower_map:
            return lower_map[key]

    for c in cols:
        lc = clean_col(c).lower()

        if any(bad in lc for bad in bad_terms):
            continue

        if "%" in lc and ("asset" in lc or "weight" in lc or "fund" in lc or "portfolio" in lc):
            return c

    for c in cols:
        lc = clean_col(c).lower()

        if any(bad in lc for bad in bad_terms):
            continue

        if "weight" in lc or "percent" in lc:
            return c

    return None


def parse_num(x):
    if pd.isna(x):
        return None

    s = str(x).strip()

    if not s:
        return None

    neg = s.startswith("(") and s.endswith(")")

    s = (
        s.replace(",", "")
        .replace("%", "")
        .replace("$", "")
        .replace("−", "-")
        .replace("—", "")
        .replace("(", "")
        .replace(")", "")
        .strip()
    )

    if s.lower() in ["", "-", "--", "nan", "none", "n/a"]:
        return None

    try:
        val = float(s)
        return -val if neg else val
    except Exception:
        return None


def requests_get(url):
    r = requests.get(url, headers=HEADERS, timeout=45)
    r.raise_for_status()
    return r


# ============================================================
# FILE / TABLE PARSING HELPERS
# ============================================================

def find_table_in_excel_raw(raw):
    header_row = None

    for i in range(min(160, len(raw))):
        row_text = " ".join(
            raw.iloc[i].dropna().astype(str).str.lower().tolist()
        )

        score = 0

        if "ticker" in row_text or "symbol" in row_text:
            score += 1
        if "name" in row_text or "security" in row_text or "holding" in row_text:
            score += 1
        if (
            "weight" in row_text
            or "% of" in row_text
            or "net assets" in row_text
            or "holding percent" in row_text
        ):
            score += 1

        if score >= 2:
            header_row = i
            break

    if header_row is None:
        return None

    df = raw.iloc[header_row + 1:].copy()
    df.columns = [clean_col(c) for c in raw.iloc[header_row].tolist()]

    return df.dropna(how="all")


def read_any_file_to_tables(content_bytes):
    dfs = []

    # Excel / XLSX / XLS
    try:
        xls = pd.ExcelFile(BytesIO(content_bytes))

        for sheet in xls.sheet_names:
            raw = pd.read_excel(BytesIO(content_bytes), sheet_name=sheet, header=None)
            df = find_table_in_excel_raw(raw)

            if df is not None and not df.empty:
                dfs.append(df)

    except Exception:
        pass

    # CSV / TSV / pipe-separated text
    try:
        text = content_bytes.decode("utf-8-sig", errors="ignore")

        for sep in [",", "\t", "|"]:
            for skip in range(0, 35):
                try:
                    df = pd.read_csv(StringIO(text), sep=sep, skiprows=skip)

                    if df.shape[1] >= 3 and len(df) > 0:
                        dfs.append(df)

                except Exception:
                    pass

    except Exception:
        pass

    # HTML tables
    try:
        text = content_bytes.decode("utf-8", errors="ignore")

        for df in pd.read_html(StringIO(text)):
            if df.shape[1] >= 3:
                dfs.append(df)

    except Exception:
        pass

    return dfs


def choose_best_holdings_table(dfs):
    best = None
    best_score = -1

    for df in dfs:
        if df is None:
            continue

        df = df.copy()
        df = df.dropna(how="all")

        if df.empty or df.shape[1] < 3:
            continue

        df.columns = make_unique_columns(df.columns)

        ticker_col = find_col(df, TICKER_COLS, contains=["ticker", "symbol"])
        name_col = find_col(
            df,
            NAME_COLS,
            contains=["name", "security", "holding", "description", "company"],
        )
        weight_col = find_weight_col_strict(df)

        score = 0

        if ticker_col:
            score += 4
        if name_col:
            score += 3
        if weight_col:
            score += 5

        # Prefer tables whose weight column name looks very clear.
        if weight_col:
            lc = clean_col(weight_col).lower()
            if "% of net assets" in lc or "etf weight" in lc or "holding percent" in lc:
                score += 5

        score += min(len(df), 500) / 100

        if score > best_score:
            best = df
            best_score = score

    if best is None:
        raise ValueError("Could not identify holdings table.")

    return best


# ============================================================
# TICKER MAPPING HELPERS
# ============================================================

CUSIP_TO_YAHOO = {
    "037833100": "AAPL",
    "594918104": "MSFT",
    "02079K305": "GOOGL",
    "02079K107": "GOOG",
    "023135106": "AMZN",
    "30303M102": "META",
    "67066G104": "NVDA",
    "88160R101": "TSLA",
    "64110L106": "NFLX",
}

NAME_TO_YAHOO = {
    "apple": "AAPL",
    "microsoft": "MSFT",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "amazon": "AMZN",
    "meta platforms": "META",
    "facebook": "META",
    "netflix": "NFLX",
    "nvidia": "NVDA",
    "tesla": "TSLA",
    "broadcom": "AVGO",
    "taiwan semiconductor": "TSM",
    "tsmc": "TSM",
    "asml": "ASML",
    "advanced micro devices": "AMD",
    "amd": "AMD",
    "lam research": "LRCX",
    "applied materials": "AMAT",
    "kla": "KLAC",
    "arm holdings": "ARM",
    "qualcomm": "QCOM",
    "micron": "MU",
    "marvell": "MRVL",
    "monolithic power": "MPWR",
    "teradyne": "TER",
    "microchip technology": "MCHP",
    "analog devices": "ADI",
    "nxp": "NXPI",
    "on semiconductor": "ON",
    "texas instruments": "TXN",
    "intel": "INTC",
    "synopsys": "SNPS",
    "cadence": "CDNS",
    "sk hynix": "000660.KS",
    "samsung electronics": "005930.KS",
    "disco corp": "6146.T",
    "advantest": "6857.T",
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
        "cash",
        "cash equivalent",
        "treasury",
        "t-bill",
        "t bill",
        "money market",
        "collateral",
        "repo",
        "repurchase",
        "total",
        "disclaimer",
        "receivable",
        "payable",
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

    s = (
        s.replace(" Equity", "")
        .replace(" Common Stock", "")
        .replace("Class A", "")
        .replace("Class C", "")
        .strip()
    )

    suffix_map = {
        "US": "",
        "CN": ".TO",
        "CT": ".TO",
        "TT": ".TW",
        "JT": ".T",
        "NA": ".AS",
        "GY": ".DE",
        "SW": ".SW",
        "LN": ".L",
        "HK": ".HK",
    }

    for suffix, yahoo_suffix in suffix_map.items():
        m = re.match(rf"^([A-Z0-9.\-]+)\s+{suffix}$", s, flags=re.I)

        if m:
            return m.group(1).replace(".", "-").upper() + yahoo_suffix

    m = re.match(r"^([0-9]+)\s+(KS|KP)$", s, flags=re.I)

    if m:
        return m.group(1).zfill(6) + ".KS"

    exchange_prefix_map = {
        "KRX": ".KS",
        "TPE": ".TW",
        "TYO": ".T",
        "AMS": ".AS",
        "ETR": ".DE",
        "EPA": ".PA",
        "SWX": ".SW",
        "LON": ".L",
        "HKG": ".HK",
        "TSX": ".TO",
    }

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


# ============================================================
# NORMALIZE HOLDINGS
# ============================================================

def normalize_holdings(raw_df, etf):
    df = raw_df.copy()
    df = df.dropna(how="all")
    df.columns = make_unique_columns(df.columns)

    if df.empty:
        raise ValueError(f"{etf}: issuer holdings table is empty.")

    ticker_col = find_col(df, TICKER_COLS, contains=["ticker", "symbol"])
    name_col = find_col(
        df,
        NAME_COLS,
        contains=["name", "security", "holding", "description", "company"],
    )
    weight_col = find_weight_col_strict(df)
    shares_col = find_col(df, SHARES_COLS, contains=["shares", "quantity"])
    id_col = find_col(df, ID_COLS, contains=["cusip", "isin", "sedol", "identifier", "figi"])

    if weight_col is None:
        raise ValueError(f"{etf}: could not find real weight column. Columns={list(df.columns)}")

    out = pd.DataFrame()

    out["Raw Ticker"] = (
        first_series(df, ticker_col).astype(str).str.strip()
        if ticker_col
        else ""
    )
    out["Name"] = (
        first_series(df, name_col).astype(str).str.strip()
        if name_col
        else ""
    )
    out["Weight"] = first_series(df, weight_col).map(parse_num)
    out["Shares Held"] = (
        first_series(df, shares_col).map(parse_num)
        if shares_col
        else None
    )
    out["Identifier"] = (
        first_series(df, id_col).astype(str).str.strip()
        if id_col
        else ""
    )

    out["ETF"] = etf
    out = out.dropna(subset=["Weight"])

    if out.empty:
        raise ValueError(f"{etf}: no usable holdings rows after parsing issuer table.")

    # Some providers report weight as decimal instead of percent.
    # Example: 0.142857 means 14.2857%.
    if out["Weight"].abs().max() <= 1.5:
        out["Weight"] *= 100

    out["Yahoo Ticker"] = out.apply(
        lambda r: map_to_yahoo_symbol(
            r["Raw Ticker"],
            r["Name"],
            r["Identifier"],
        ),
        axis=1,
    )

    if not INCLUDE_CASH_FUTURES_SWAPS:
        combined = (
            out["Raw Ticker"].fillna("").astype(str)
            + " "
            + out["Name"].fillna("").astype(str)
            + " "
            + out["Identifier"].fillna("").astype(str)
        )

        # Important for MAGS:
        # Do not remove rows merely because they are swaps.
        # MAGS exposure can combine stock positions and total return swaps.
        out = out[~combined.map(looks_like_bad_row)]

    out = out[out["Yahoo Ticker"].notna()]
    out = out[out["Yahoo Ticker"].astype(str).str.strip() != ""]
    out = out[out["Weight"].abs() > 0.000001]

    if out.empty:
        raise ValueError(f"{etf}: no equity holdings remained after cleaning.")

    out = (
        out.groupby(["ETF", "Yahoo Ticker"], dropna=False)
        .agg({
            "Raw Ticker": lambda x: "; ".join(sorted(set(map(str, x))))[:300],
            "Name": lambda x: "; ".join(sorted(set(map(str, x)))[:8])[:500],
            "Weight": "sum",
            "Shares Held": "sum",
            "Identifier": lambda x: "; ".join(sorted(set(map(str, x)))[:8])[:300],
        })
        .reset_index()
        .sort_values("Weight", ascending=False)
        .reset_index(drop=True)
    )

    return out


def sanity_check_weight_total(holdings, etf, low=70, high=130):
    total_weight = holdings["Weight"].sum()

    if total_weight < low or total_weight > high:
        raise ValueError(
            f"{etf}: parsed weight total looks wrong: {total_weight:.2f}%. "
            "This usually means the wrong weight column was parsed."
        )

    return total_weight


# ============================================================
# ISSUER HOLDINGS PULLERS
# ============================================================

def pull_spmo_companiesmarketcap():
    """
    Pull complete SPMO holdings from CompaniesMarketCap.
    This replaces the old Invesco/Playwright method.
    """

    url = ETF_CONFIG["SPMO"]["url"]
    r = requests_get(url)

    # Main method: HTML table parsing.
    try:
        tables = pd.read_html(StringIO(r.text))

        candidates = []

        for df in tables:
            df = df.copy()
            df.columns = make_unique_columns(df.columns)

            cols_lower = " ".join(str(c).lower() for c in df.columns)

            has_weight = "weight" in cols_lower or "%" in cols_lower
            has_ticker = "ticker" in cols_lower or "symbol" in cols_lower
            has_name = "name" in cols_lower or "company" in cols_lower

            if has_weight and has_ticker and has_name:
                candidates.append(df)

        if candidates:
            raw = choose_best_holdings_table(candidates)
            raw.columns = make_unique_columns(raw.columns)

            rename_map = {}
            for c in raw.columns:
                lc = str(c).strip().lower()
                if lc in ["weight %", "weight", "%"]:
                    rename_map[c] = "Weight"
                elif lc in ["symbol", "ticker"]:
                    rename_map[c] = "Ticker"
                elif lc in ["company", "company name", "name"]:
                    rename_map[c] = "Name"
                elif "shares" in lc:
                    rename_map[c] = "Shares Held"

            raw = raw.rename(columns=rename_map)

            test = normalize_holdings(raw, "SPMO")

            if len(test) >= 80:
                return raw

            raise ValueError(f"CompaniesMarketCap table parsed only {len(test)} SPMO rows.")

    except Exception as e:
        table_error = repr(e)
    else:
        table_error = "No matching HTML table found."

    # Fallback method: parse visible text lines.
    soup = BeautifulSoup(r.text, "html.parser")
    lines = [
        x.strip()
        for x in soup.get_text("\n", strip=True).splitlines()
        if x.strip()
    ]

    rows = []
    i = 0

    while i + 3 < len(lines):
        weight = parse_num(lines[i])

        if weight is not None and "%" in lines[i]:
            name = lines[i + 1].strip()
            ticker = lines[i + 2].strip()
            shares = parse_num(lines[i + 3])

            ticker_ok = bool(re.match(r"^[A-Z0-9.\-]{1,12}$", ticker))

            if ticker_ok and name and shares is not None:
                rows.append({
                    "Ticker": ticker,
                    "Name": name,
                    "Weight": weight,
                    "Shares Held": shares,
                })
                i += 4
                continue

        i += 1

    if not rows:
        debug_path = OUTPUT_DIR / "SPMO_companiesmarketcap_debug.html"
        debug_path.write_text(r.text, encoding="utf-8")
        raise ValueError(
            "Could not parse SPMO holdings from CompaniesMarketCap. "
            f"HTML-table error was: {table_error}. "
            f"Saved debug HTML to {debug_path}"
        )

    raw = pd.DataFrame(rows)
    raw = raw.drop_duplicates(subset=["Ticker", "Name", "Weight", "Shares Held"])

    test = normalize_holdings(raw, "SPMO")

    if len(test) < 80:
        raise ValueError(
            f"SPMO parse looked incomplete. Parsed only {len(test)} usable holdings."
        )

    return raw


def standardize_mags_candidate(raw, source_name):
    """
    Normalize a MAGS candidate table and validate that it looks like MAGS.
    Returns raw if valid; raises otherwise.
    """
    test = normalize_holdings(raw, "MAGS")

    required = {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"}
    got = set(test["Yahoo Ticker"].astype(str))

    missing = required - got
    if missing:
        raise ValueError(f"MAGS {source_name}: missing required names/tickers: {sorted(missing)}")

    total = test["Weight"].sum()
    if total < 90 or total > 120:
        raise ValueError(f"MAGS {source_name}: bad total exposure weight {total:.2f}%")

    return raw


def try_roundhill_download_links_for_mags(page_html, base_url):
    soup = BeautifulSoup(page_html, "html.parser")
    candidate_urls = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        text = a.get_text(" ", strip=True).lower()
        combo = f"{href} {text}".lower()

        if (
            ("csv" in combo or "download" in combo or "holdings" in combo)
            and ("mags" in combo or "download csv" in combo or "download" in text)
        ):
            full = urljoin(base_url, href)
            if full not in candidate_urls:
                candidate_urls.append(full)

    errors = []

    for dl_url in candidate_urls:
        try:
            rr = requests_get(dl_url)

            if len(rr.content) <= 50:
                continue

            dfs = read_any_file_to_tables(rr.content)

            for df in dfs:
                try:
                    df = df.copy()
                    df.columns = make_unique_columns(df.columns)
                    return standardize_mags_candidate(df, f"Roundhill download {dl_url}")
                except Exception as e:
                    errors.append(repr(e))

        except Exception as e:
            errors.append(f"{dl_url}: {repr(e)}")

    raise ValueError("No valid Roundhill MAGS download link worked. " + " | ".join(errors[:5]))


def try_roundhill_html_tables_for_mags(page_html):
    errors = []

    try:
        tables = pd.read_html(StringIO(page_html))
    except Exception as e:
        raise ValueError(f"No Roundhill MAGS HTML tables found: {e}")

    for df in tables:
        try:
            df = df.copy()
            df.columns = make_unique_columns(df.columns)
            return standardize_mags_candidate(df, "Roundhill HTML table")
        except Exception as e:
            errors.append(repr(e))

    raise ValueError("No valid Roundhill MAGS HTML table worked. " + " | ".join(errors[:5]))


def try_yfinance_mags_holdings():
    """
    yfinance sometimes exposes ETF fund holdings through funds_data.top_holdings.
    This is a good fallback when Roundhill's live holdings table is rendered by JS.
    """
    t = yf.Ticker("MAGS")
    fd = getattr(t, "funds_data", None)

    if fd is None:
        raise ValueError("yfinance funds_data is not available for MAGS.")

    yh = getattr(fd, "top_holdings", None)

    if yh is None or not isinstance(yh, pd.DataFrame) or yh.empty:
        raise ValueError("yfinance top_holdings is empty for MAGS.")

    df = yh.copy().reset_index()
    df.columns = make_unique_columns(df.columns)

    rename_map = {}

    for c in df.columns:
        lc = str(c).strip().lower()

        if lc in ["symbol", "ticker"] or "symbol" in lc or "ticker" in lc:
            rename_map[c] = "Ticker"
        elif lc in ["holding", "name", "holding name"] or "name" in lc:
            rename_map[c] = "Name"
        elif "percent" in lc or "weight" in lc:
            rename_map[c] = "Weight"

    df = df.rename(columns=rename_map)

    if "Ticker" not in df.columns:
        # Very common yfinance shape: index column is the ticker after reset_index().
        first_col = df.columns[0]
        df = df.rename(columns={first_col: "Ticker"})

    if "Name" not in df.columns:
        df["Name"] = df["Ticker"]

    return standardize_mags_candidate(df, "yfinance fund holdings")


def try_etfdb_mags_holdings():
    """
    Non-issuer fallback. Used only if issuer/yfinance data fail.
    """
    url = ETF_CONFIG["MAGS"]["fallback_url"]
    r = requests_get(url)

    try:
        tables = pd.read_html(StringIO(r.text))
    except Exception as e:
        raise ValueError(f"ETFDB MAGS tables unavailable: {e}")

    errors = []

    for df in tables:
        try:
            df = df.copy()
            df.columns = make_unique_columns(df.columns)

            # Common ETFDB table variants.
            rename_map = {}
            for c in df.columns:
                lc = str(c).strip().lower()
                if "symbol" in lc or "ticker" in lc:
                    rename_map[c] = "Ticker"
                elif "holding" in lc or "company" in lc or "name" in lc:
                    rename_map[c] = "Name"
                elif "%" in lc or "assets" in lc or "weight" in lc:
                    rename_map[c] = "Weight"

            df = df.rename(columns=rename_map)
            return standardize_mags_candidate(df, "ETFDB")
        except Exception as e:
            errors.append(repr(e))

    raise ValueError("ETFDB MAGS fallback did not find valid holdings. " + " | ".join(errors[:5]))


def pull_mags_roundhill_issuer_page():
    """
    Better MAGS logic:
    1. Try to pull real Roundhill holdings from download links.
    2. Try Roundhill HTML tables.
    3. Try yfinance ETF fund holdings.
    4. Try ETFDB as a non-issuer fallback.
    5. Last resort: equal-weight model, clearly flagged in Source Note later.
    """

    url = ETF_CONFIG["MAGS"]["url"]
    r = requests_get(url)

    attempts = []

    for label, func in [
        ("Roundhill download links", lambda: try_roundhill_download_links_for_mags(r.text, url)),
        ("Roundhill HTML tables", lambda: try_roundhill_html_tables_for_mags(r.text)),
        ("yfinance fund holdings", try_yfinance_mags_holdings),
        ("ETFDB fallback", try_etfdb_mags_holdings),
    ]:
        try:
            raw = func()
            print(f"MAGS holdings source used: {label}")
            return raw
        except Exception as e:
            attempts.append(f"{label}: {repr(e)}")

    print("WARNING: MAGS real holdings not found. Using equal-weight model 100/7.")
    print("This is exposure approximation, not exact current issuer weight.")
    print("MAGS attempts failed:")
    for x in attempts:
        print(" -", x[:500])

    w = 100 / 7

    return pd.DataFrame([
        {"Ticker": "AAPL", "Name": "Apple Inc.", "Weight": w},
        {"Ticker": "MSFT", "Name": "Microsoft Corp.", "Weight": w},
        {"Ticker": "GOOGL", "Name": "Alphabet Inc.", "Weight": w},
        {"Ticker": "AMZN", "Name": "Amazon.com Inc.", "Weight": w},
        {"Ticker": "META", "Name": "Meta Platforms Inc.", "Weight": w},
        {"Ticker": "NVDA", "Name": "NVIDIA Corp.", "Weight": w},
        {"Ticker": "TSLA", "Name": "Tesla Inc.", "Weight": w},
    ])


def pull_evolve_tech_csv():
    last_error = None

    for url in ETF_CONFIG["TECH.TO"]["csv_urls"]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=45)

            if r.status_code == 200 and len(r.content) > 50:
                dfs = read_any_file_to_tables(r.content)

                if dfs:
                    return choose_best_holdings_table(dfs)

            last_error = f"{r.status_code} for {url}"

        except Exception as e:
            last_error = str(e)

    raise ValueError(f"TECH.TO Evolve issuer CSV failed. Last error: {last_error}")


def pull_globalx_chps_page():
    r = requests_get(ETF_CONFIG["CHPS.TO"]["url"])

    try:
        tables = pd.read_html(StringIO(r.text))

        if tables:
            raw = choose_best_holdings_table(tables)

            if len(normalize_holdings(raw, "CHPS.TO")) >= 10:
                return raw

    except Exception:
        pass

    soup = BeautifulSoup(r.text, "html.parser")
    lines = [
        x.strip()
        for x in soup.get_text("\n", strip=True).splitlines()
        if x.strip()
    ]

    rows = []
    start = next(
        (i for i, line in enumerate(lines) if line.lower() == "top holdings"),
        None,
    )

    if start is None:
        raise ValueError("Could not find CHPS.TO issuer holdings on Global X page.")

    i = start

    while i < len(lines) and lines[i].lower() != "security name":
        i += 1

    i += 2

    while i + 1 < len(lines):
        name = lines[i]
        weight = parse_num(lines[i + 1])

        if "holdings are subject" in name.lower():
            break

        if weight is not None:
            rows.append({
                "Ticker": map_name_to_yahoo(name),
                "Name": name,
                "Weight": weight,
            })
            i += 2
        else:
            i += 1

    if not rows:
        raise ValueError("Could not parse CHPS.TO issuer holdings from Global X page.")

    return pd.DataFrame(rows)


def pull_blackrock_soxx():
    product_id = ETF_CONFIG["SOXX"]["product_id"]
    file_name = ETF_CONFIG["SOXX"]["file_name"]

    urls = [
        (
            f"https://www.ishares.com/us/products/{product_id}/ishares-semiconductor-etf/"
            f"1467271812596.ajax?fileType=csv&fileName={file_name}&dataType=fund"
        ),
        (
            f"https://www.ishares.com/us/products/{product_id}/ishares-phlx-semiconductor-etf/"
            f"1467271812596.ajax?fileType=csv&fileName={file_name}&dataType=fund"
        ),
        (
            f"https://www.ishares.com/us/products/{product_id}/"
            f"1467271812596.ajax?fileType=csv&fileName={file_name}&dataType=fund"
        ),
    ]

    last_error = None

    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=45)

            if r.status_code == 200:
                dfs = read_any_file_to_tables(r.content)

                if dfs:
                    return choose_best_holdings_table(dfs)

            last_error = f"{r.status_code} for {url}"

        except Exception as e:
            last_error = str(e)

    raise ValueError(f"SOXX BlackRock issuer CSV failed. Last error: {last_error}")


def pull_vaneck_smh_page():
    """
    Pull SMH holdings from VanEck direct XLSX.
    Force the true portfolio weight column: % of Net Assets.
    """

    url = ETF_CONFIG["SMH"]["url"]
    r = requests_get(url)

    if len(r.content) < 1000:
        raise ValueError("SMH VanEck XLSX download returned too little content.")

    candidates = []

    try:
        xls = pd.ExcelFile(BytesIO(r.content))

        for sheet in xls.sheet_names:
            raw = pd.read_excel(BytesIO(r.content), sheet_name=sheet, header=None)

            for i in range(min(160, len(raw))):
                row_text = " ".join(raw.iloc[i].dropna().astype(str).str.lower().tolist())

                has_ticker = "ticker" in row_text or "symbol" in row_text
                has_name = "holding name" in row_text or "security" in row_text or "name" in row_text
                has_weight = "% of net assets" in row_text or "% of net asset" in row_text

                if has_ticker and has_name and has_weight:
                    df = raw.iloc[i + 1:].copy()
                    df.columns = make_unique_columns(raw.iloc[i].tolist())
                    df = df.dropna(how="all")
                    candidates.append(df)

    except Exception:
        pass

    if not candidates:
        dfs = read_any_file_to_tables(r.content)
        candidates = dfs

    best = None
    best_score = -1

    for df in candidates:
        try:
            df = df.copy()
            df.columns = make_unique_columns(df.columns)

            cols_text = " ".join(str(c).lower() for c in df.columns)

            if "% of net assets" in cols_text:
                score = 1000
            elif "% of net asset" in cols_text:
                score = 900
            else:
                score = 0

            score += len(df)

            if score > best_score:
                best = df
                best_score = score

        except Exception:
            pass

    if best is None:
        debug_path = OUTPUT_DIR / "SMH_vaneck_download_debug.bin"
        debug_path.write_bytes(r.content)
        raise ValueError(
            "Could not find SMH holdings table with % of Net Assets. "
            f"Saved debug file to {debug_path}"
        )

    raw = best.copy()
    raw.columns = make_unique_columns(raw.columns)

    rename_map = {}

    for c in raw.columns:
        lc = str(c).strip().lower()

        if lc in ["ticker", "symbol"]:
            rename_map[c] = "Ticker"
        elif "holding name" in lc or lc == "name" or lc == "security":
            rename_map[c] = "Name"
        elif "% of net assets" in lc or "% of net asset" in lc:
            rename_map[c] = "Weight"
        elif "shares" in lc or "quantity" in lc:
            rename_map[c] = "Shares Held"
        elif "identifier" in lc or "figi" in lc or "cusip" in lc or "isin" in lc:
            rename_map[c] = "Identifier"

    raw = raw.rename(columns=rename_map)

    required = ["Ticker", "Name", "Weight"]
    missing = [c for c in required if c not in raw.columns]

    if missing:
        raise ValueError(
            f"SMH missing required columns after rename: {missing}. "
            f"Columns found: {list(raw.columns)}"
        )

    raw = raw[
        ~(
            raw["Ticker"].astype(str).str.contains("CASH|USD", case=False, na=False)
            | raw["Name"].astype(str).str.contains(
                "cash|other|receivable|payable|collateral|treasury",
                case=False,
                na=False,
            )
        )
    ].copy()

    raw = raw.dropna(how="all")

    test = normalize_holdings(raw, "SMH")
    total_weight = test["Weight"].sum()

    if len(test) < 20:
        raise ValueError(
            f"SMH VanEck XLSX parse looked incomplete. Parsed only {len(test)} usable equity holdings."
        )

    if total_weight < 90 or total_weight > 105:
        raise ValueError(
            f"SMH weight sanity check failed. Parsed total weight = {total_weight:.2f}%. "
            "This usually means the wrong column was parsed."
        )

    return raw


def pull_ssga_xlk():
    r = requests_get(ETF_CONFIG["XLK"]["url"])
    return choose_best_holdings_table(read_any_file_to_tables(r.content))


def pull_issuer_holdings(etf):
    etf = etf.upper()

    if etf == "TECH":
        etf = "TECH.TO"

    if etf == "CHPS":
        etf = "CHPS.TO"

    print(f"\nPulling {etf} issuer holdings...")
    print(f"Issuer: {ETF_CONFIG[etf]['issuer']}")

    if etf == "SPMO":
        raw = pull_spmo_companiesmarketcap()
    elif etf == "MAGS":
        raw = pull_mags_roundhill_issuer_page()
    elif etf == "TECH.TO":
        raw = pull_evolve_tech_csv()
    elif etf == "CHPS.TO":
        raw = pull_globalx_chps_page()
    elif etf == "SOXX":
        raw = pull_blackrock_soxx()
    elif etf == "SMH":
        raw = pull_vaneck_smh_page()
    elif etf == "XLK":
        raw = pull_ssga_xlk()
    else:
        raise ValueError(f"No ETF config found for {etf}")

    holdings = normalize_holdings(raw, etf)

    if etf == "MAGS":
        # MAGS can be stock + swap exposure, so the total may be slightly above/below 100.
        sanity_check_weight_total(holdings, etf, low=90, high=120)
    elif etf == "SMH":
        sanity_check_weight_total(holdings, etf, low=90, high=105)

    holdings["Source Note"] = f"Issuer-first: {ETF_CONFIG[etf]['issuer']}"

    # Flag MAGS if it used the equal-weight fallback.
    if etf == "MAGS" and len(holdings) == 7:
        vals = holdings["Weight"].round(6).unique()
        if len(vals) == 1 and abs(vals[0] - round(100 / 7, 6)) < 0.0005:
            holdings["Source Note"] = "MAGS fallback: equal-weight approximation 100/7, not exact current daily weight"

    print(f"{etf}: normalized holdings = {len(holdings)}")
    print(f"{etf}: total normalized weight = {holdings['Weight'].sum():.2f}%")

    return holdings


# ============================================================
# YAHOO ANALYST TARGETS
# ============================================================

def pull_yahoo_targets(symbol):
    out = {
        "Yahoo Ticker": symbol,
        "Current Price": None,
        "Target Low": None,
        "Target Mean": None,
        "Target High": None,
        "Target Median": None,
        "Analyst Count": None,
        "Yahoo Error": None,
    }

    try:
        t = yf.Ticker(symbol)

        try:
            targets = t.get_analyst_price_targets()

            if isinstance(targets, dict):
                out["Target Low"] = targets.get("low")
                out["Target Mean"] = targets.get("mean")
                out["Target High"] = targets.get("high")
                out["Target Median"] = targets.get("median")
                out["Analyst Count"] = targets.get("numberOfAnalysts")

            elif isinstance(targets, pd.DataFrame) and not targets.empty:
                row = targets.iloc[0]
                out["Target Low"] = row.get("low")
                out["Target Mean"] = row.get("mean")
                out["Target High"] = row.get("high")
                out["Target Median"] = row.get("median")
                out["Analyst Count"] = row.get("numberOfAnalysts")

        except Exception as e:
            out["Yahoo Error"] = f"target error: {e}"

        try:
            fast = t.fast_info

            if hasattr(fast, "get"):
                out["Current Price"] = fast.get("last_price")
            else:
                out["Current Price"] = getattr(fast, "last_price", None)

        except Exception:
            pass

        if out["Current Price"] is None or pd.isna(out["Current Price"]):
            hist = t.history(period="5d", auto_adjust=False)

            if hist is not None and not hist.empty:
                out["Current Price"] = hist["Close"].dropna().iloc[-1]

    except Exception as e:
        out["Yahoo Error"] = str(e)

    return out


def add_yahoo_targets(holdings):
    rows = []
    symbols = sorted(holdings["Yahoo Ticker"].dropna().astype(str).unique())

    for i, symbol in enumerate(symbols, 1):
        print(f"Yahoo analyst targets {i}/{len(symbols)}: {symbol}")
        rows.append(pull_yahoo_targets(symbol))
        time.sleep(YAHOO_SLEEP_SECONDS)

    targets = pd.DataFrame(rows)
    merged = holdings.merge(targets, on="Yahoo Ticker", how="left")

    numeric_cols = [
        "Current Price",
        "Target Low",
        "Target Mean",
        "Target High",
        "Target Median",
        "Analyst Count",
    ]

    for c in numeric_cols:
        if c in merged.columns:
            merged[c] = pd.to_numeric(merged[c], errors="coerce")

    return merged


# ============================================================
# RETURN CALCULATIONS
# ============================================================

def calculate_returns(df):
    out = df.copy()

    out["Weight Decimal"] = out["Weight"] / 100.0

    out["Low Return"] = out["Target Low"] / out["Current Price"] - 1
    out["Mean Return"] = out["Target Mean"] / out["Current Price"] - 1
    out["High Return"] = out["Target High"] / out["Current Price"] - 1
    out["Median Return"] = out["Target Median"] / out["Current Price"] - 1

    out["Weighted Low Return"] = out["Weight Decimal"] * out["Low Return"]
    out["Weighted Mean Return"] = out["Weight Decimal"] * out["Mean Return"]
    out["Weighted High Return"] = out["Weight Decimal"] * out["High Return"]
    out["Weighted Median Return"] = out["Weight Decimal"] * out["Median Return"]

    return out


def summarize_etf(df, etf):
    covered = df.dropna(
        subset=["Current Price", "Target Low", "Target Mean", "Target High"]
    ).copy()

    return {
        "ETF": etf,
        "Total Holdings Used": len(df),
        "Covered Holdings": len(covered),
        "Total Weight Used": df["Weight Decimal"].sum(),
        "Covered Weight": covered["Weight Decimal"].sum(),
        "Raw Worst Case Return": covered["Weighted Low Return"].sum(),
        "Raw Mean Case Return": covered["Weighted Mean Return"].sum(),
        "Raw Best Case Return": covered["Weighted High Return"].sum(),
        "Raw Median Case Return": covered["Weighted Median Return"].sum(),
    }


def print_summary(summary):
    print("\n" + "=" * 72)
    print(f"{summary['ETF']} Analyst Target Implied 1-Year Performance")
    print("=" * 72)

    print(f"Total holdings used: {summary['Total Holdings Used']}")
    print(f"Covered holdings:    {summary['Covered Holdings']}")
    print(f"Total weight used:   {summary['Total Weight Used']:.2%}")
    print(f"Covered weight:      {summary['Covered Weight']:.2%}")

    print("\nRaw ETF impact, uncovered holdings treated as 0 contribution:")
    print(f"Worst case:  {summary['Raw Worst Case Return']:.2%}")
    print(f"Mean case:   {summary['Raw Mean Case Return']:.2%}")
    print(f"Best case:   {summary['Raw Best Case Return']:.2%}")
    print(f"Median case: {summary['Raw Median Case Return']:.2%}")


# ============================================================
# RUNNERS
# ============================================================

def run_one_etf(etf):
    if etf.upper() == "TECH":
        etf = "TECH.TO"

    if etf.upper() == "CHPS":
        etf = "CHPS.TO"

    holdings = pull_issuer_holdings(etf)
    enriched = add_yahoo_targets(holdings)
    enriched = calculate_returns(enriched)

    summary = summarize_etf(enriched, etf)
    print_summary(summary)

    output_cols = [
        "ETF",
        "Yahoo Ticker",
        "Raw Ticker",
        "Name",
        "Weight",
        "Weight Decimal",
        "Current Price",
        "Target Low",
        "Target Mean",
        "Target High",
        "Target Median",
        "Analyst Count",
        "Low Return",
        "Mean Return",
        "High Return",
        "Median Return",
        "Weighted Low Return",
        "Weighted Mean Return",
        "Weighted High Return",
        "Weighted Median Return",
        "Shares Held",
        "Identifier",
        "Source Note",
        "Yahoo Error",
    ]

    enriched = (
        enriched[[c for c in output_cols if c in enriched.columns]]
        .sort_values("Weight", ascending=False)
        .reset_index(drop=True)
    )

    return enriched, summary


def main():
    """
    Run all ETFs and create only the Excel workbook.
    No CSV files are written.
    """
    return main_with_excel()


# ============================================================
# EXCEL REPORT EXPORTER
# Summary sheet + one sheet per ETF
# ============================================================

def get_etf_current_price(etf):
    """
    Pull ETF current price from Yahoo Finance.
    For Canadian tickers, keep the .TO suffix.
    """
    try:
        t = yf.Ticker(etf)
        fast = t.fast_info

        price = None

        if hasattr(fast, "get"):
            price = fast.get("last_price")
        else:
            price = getattr(fast, "last_price", None)

        if price is None or pd.isna(price):
            hist = t.history(period="5d", auto_adjust=False)
            if hist is not None and not hist.empty:
                price = hist["Close"].dropna().iloc[-1]

        return float(price) if price is not None and not pd.isna(price) else None

    except Exception:
        return None


def build_excel_summary_rows(summary_df):
    """
    Build summary rows for the first sheet.

    reliable = Covered Weight
    Average  = Raw Mean Case Return
    Median   = Raw Median Case Return
    WORST    = Raw Worst Case Return
    BEST     = Raw Best Case Return
    Current pr = ETF current price from Yahoo
    worst price = Current pr * (1 + Raw Worst Case Return)
    """
    rows = []

    for _, r in summary_df.iterrows():
        etf = r["ETF"]
        current_price = get_etf_current_price(etf)
        worst_return = r.get("Raw Worst Case Return")

        worst_price = None
        if current_price is not None and pd.notna(worst_return):
            worst_price = current_price * (1 + worst_return)

        rows.append({
            "ETF": etf,
            "reliable": r.get("Covered Weight"),
            "Average": r.get("Raw Mean Case Return"),
            "Median": r.get("Raw Median Case Return"),
            "WORST": r.get("Raw Worst Case Return"),
            "BEST": r.get("Raw Best Case Return"),
            "Current pr": current_price,
            "worst price": worst_price,
        })

    return pd.DataFrame(rows)


def prepare_detail_sheet(details):
    """
    Create ETF detail-sheet data like your second screenshot.
    Uses each holding's portfolio weight and Yahoo target-implied returns.
    """
    df = details.copy()

    out = pd.DataFrame()
    out["Ticker"] = df["Yahoo Ticker"]
    out["Name"] = df["Name"]
    out["% of Portfolio"] = df["Weight Decimal"]
    out["Worst"] = df["Low Return"]
    out["Average"] = df["Mean Return"]
    out["Median"] = df["Median Return"]
    out["Best"] = df["High Return"]
    out["Worst Target"] = df["Target Low"]
    out["Average Target"] = df["Target Mean"]
    out["Median Target"] = df["Target Median"]
    out["Best Target"] = df["Target High"]
    out["Current"] = df["Current Price"]
    out["Analyst Count"] = df["Analyst Count"]

    if "Source Note" in df.columns:
        out["Source Note"] = df["Source Note"]

    out = out.sort_values("% of Portfolio", ascending=False).reset_index(drop=True)
    return out


def export_excel_report(all_details, summaries, output_path=None, report_date=None):
    """
    Create a formatted Excel report.

    Sheets:
    1. Summary
       Columns: ETF, reliable, Average, Median, WORST, BEST, Current pr, worst price

    2. One sheet per ETF
       Columns similar to your page 2 screenshot:
       Ticker, % of Portfolio, Worst, Average, Median, Best,
       Yahoo target prices, Current price, Analyst Count, Name
    """
    if output_path is None:
        today_code = pd.Timestamp.today().strftime("%y%m%d")
        output_path = OUTPUT_DIR / f"ETF_analyst_report_{today_code}.xlsx"
    else:
        output_path = Path(output_path)

    if report_date is None:
        today = pd.Timestamp.today()
        report_date = f"{today.month}/{today.day}/{today.year}"

    summary_df = pd.DataFrame(summaries)
    summary_rows = build_excel_summary_rows(summary_df)

    # XlsxWriter is used only for the final formatted report.
    # Install if needed:
    # !pip install XlsxWriter
    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        workbook = writer.book

        # ---------- Formats ----------
        title_fmt = workbook.add_format({
            "bold": True,
            "font_size": 12,
            "align": "center",
            "valign": "vcenter",
            "border": 1,
        })

        header_fmt = workbook.add_format({
            "bold": True,
            "align": "center",
            "valign": "vcenter",
            "border": 1,
            "bg_color": "#E2F0D9",
        })

        header_white_fmt = workbook.add_format({
            "bold": True,
            "align": "center",
            "valign": "vcenter",
            "border": 1,
            "bg_color": "#FFFFFF",
        })

        normal_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
        })

        text_fmt = workbook.add_format({
            "border": 1,
            "align": "left",
            "valign": "vcenter",
        })

        pct_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "0.00%",
        })

        pct_red_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "0.00%",
            "font_color": "#FF0000",
        })

        pct_green_fill_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "0.00%",
            "bg_color": "#C6E0B4",
        })

        money_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "#,##0.00",
        })

        money_red_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "#,##0.00",
            "font_color": "#FF0000",
        })

        total_fmt = workbook.add_format({
            "bold": True,
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "0.00%",
        })

        # ---------- Summary sheet ----------
        sheet_name = "Summary"
        worksheet = workbook.add_worksheet(sheet_name)
        writer.sheets[sheet_name] = worksheet

        headers = ["", "reliable", "Average", "Median", "WORST", "BEST", "Current pr", "worst price"]

        start_row = 3
        start_col = 1

        worksheet.write(start_row, start_col, report_date, header_fmt)
        for j, h in enumerate(headers[1:], start_col + 1):
            worksheet.write(start_row, j, h, header_white_fmt)

        for i, row in summary_rows.iterrows():
            r = start_row + 1 + i
            worksheet.write(r, start_col, row["ETF"], text_fmt)
            worksheet.write_number(r, start_col + 1, row["reliable"] if pd.notna(row["reliable"]) else 0, pct_fmt)
            worksheet.write_number(r, start_col + 2, row["Average"] if pd.notna(row["Average"]) else 0, pct_fmt)
            worksheet.write_number(r, start_col + 3, row["Median"] if pd.notna(row["Median"]) else 0, pct_fmt)
            worksheet.write_number(r, start_col + 4, row["WORST"] if pd.notna(row["WORST"]) else 0, pct_fmt)
            worksheet.write_number(r, start_col + 5, row["BEST"] if pd.notna(row["BEST"]) else 0, pct_fmt)

            if pd.notna(row["Current pr"]):
                worksheet.write_number(r, start_col + 6, row["Current pr"], money_fmt)
            else:
                worksheet.write(r, start_col + 6, "-", normal_fmt)

            if pd.notna(row["worst price"]):
                worksheet.write_number(r, start_col + 7, row["worst price"], money_red_fmt)
            else:
                worksheet.write(r, start_col + 7, "-", normal_fmt)

        end_row = start_row + len(summary_rows)

        worksheet.conditional_format(f"C5:F{end_row + 1}", {
            "type": "cell",
            "criteria": "<",
            "value": 0,
            "format": pct_red_fmt,
        })
        worksheet.conditional_format(f"C5:F{end_row + 1}", {
            "type": "cell",
            "criteria": ">=",
            "value": 0.3,
            "format": pct_green_fill_fmt,
        })

        worksheet.set_column("B:B", 12)
        worksheet.set_column("C:F", 12)
        worksheet.set_column("G:I", 13)
        worksheet.freeze_panes(start_row + 1, 0)

        # ---------- ETF detail sheets ----------
        for details in all_details:
            if details.empty:
                continue

            etf_values = details["ETF"].dropna().astype(str).unique()

            if len(etf_values) > 0:
                etf = etf_values[0]
            else:
                etf = f"ETF_{len(writer.sheets)}"

            safe_sheet_base = (
                etf.replace(".", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("?", "_")
                .replace("*", "_")
                .replace("[", "_")
                .replace("]", "_")
                .replace(":", "_")
            )
            safe_sheet_base = safe_sheet_base[:31] if safe_sheet_base else f"ETF_{len(writer.sheets)}"

            safe_sheet = safe_sheet_base
            counter = 1
            used_sheets_lower = {name.lower() for name in writer.sheets.keys()}

            while safe_sheet.lower() in used_sheets_lower:
                suffix = f"_{counter}"
                safe_sheet = safe_sheet_base[:31 - len(suffix)] + suffix
                counter += 1

            detail = prepare_detail_sheet(details)

            ws = workbook.add_worksheet(safe_sheet)
            writer.sheets[safe_sheet] = ws

            # Title / top band
            ws.write(0, 0, report_date, header_white_fmt)
            ws.write(0, 1, "YTD", header_white_fmt)
            ws.write(0, 2, "Worst", header_white_fmt)
            ws.write(0, 3, "Average", header_white_fmt)
            ws.write(0, 4, "Median", header_white_fmt)
            ws.write(0, 5, "Best", header_white_fmt)
            ws.merge_range(0, 7, 0, 11, "Yahoo Finance", title_fmt)

            main_headers = [
                etf,
                "% of Portfolio",
                "Worst",
                "Average",
                "Median",
                "Best",
                "",
                "Worst",
                "Average",
                "Median",
                "Best",
                "Current",
                "Analyst Count",
                "Name",
                "Source Note",
            ]

            for c, h in enumerate(main_headers):
                ws.write(1, c, h, header_white_fmt)

            data_start = 2
            for i, row in detail.iterrows():
                r = data_start + i
                ws.write(r, 0, row["Ticker"], text_fmt)
                ws.write_number(r, 1, row["% of Portfolio"] if pd.notna(row["% of Portfolio"]) else 0, pct_fmt)

                for col_idx, col_name in enumerate(["Worst", "Average", "Median", "Best"], start=2):
                    val = row[col_name]
                    if pd.notna(val):
                        ws.write_number(r, col_idx, val, pct_fmt)
                    else:
                        ws.write(r, col_idx, "-", normal_fmt)

                ws.write(r, 6, "", normal_fmt)

                for col_idx, col_name in enumerate(["Worst Target", "Average Target", "Median Target", "Best Target", "Current"], start=7):
                    val = row[col_name]
                    if pd.notna(val):
                        fmt = money_red_fmt if col_name == "Current" else money_fmt
                        ws.write_number(r, col_idx, val, fmt)
                    else:
                        ws.write(r, col_idx, "-", normal_fmt)

                if pd.notna(row["Analyst Count"]):
                    ws.write_number(r, 12, row["Analyst Count"], normal_fmt)
                else:
                    ws.write(r, 12, "-", normal_fmt)

                ws.write(r, 13, row["Name"], text_fmt)

                if "Source Note" in detail.columns:
                    ws.write(r, 14, row.get("Source Note", ""), text_fmt)
                else:
                    ws.write(r, 14, "", text_fmt)

            total_row = data_start + len(detail) + 1
            ws.write(total_row, 0, "TOTAL", header_white_fmt)
            ws.write_formula(total_row, 1, f"=SUM(B{data_start + 1}:B{data_start + len(detail)})", total_fmt)
            ws.write_formula(total_row, 2, f"=SUMPRODUCT(B{data_start + 1}:B{data_start + len(detail)},C{data_start + 1}:C{data_start + len(detail)})", total_fmt)
            ws.write_formula(total_row, 3, f"=SUMPRODUCT(B{data_start + 1}:B{data_start + len(detail)},D{data_start + 1}:D{data_start + len(detail)})", total_fmt)
            ws.write_formula(total_row, 4, f"=SUMPRODUCT(B{data_start + 1}:B{data_start + len(detail)},E{data_start + 1}:E{data_start + len(detail)})", total_fmt)
            ws.write_formula(total_row, 5, f"=SUMPRODUCT(B{data_start + 1}:B{data_start + len(detail)},F{data_start + 1}:F{data_start + len(detail)})", total_fmt)

            last_data_excel_row = data_start + len(detail)
            ws.conditional_format(f"C3:F{last_data_excel_row}", {
                "type": "cell",
                "criteria": "<",
                "value": 0,
                "format": pct_red_fmt,
            })
            ws.conditional_format(f"C3:F{last_data_excel_row}", {
                "type": "cell",
                "criteria": ">=",
                "value": 0.3,
                "format": pct_green_fill_fmt,
            })

            ws.freeze_panes(2, 1)
            ws.set_column("A:A", 11)
            ws.set_column("B:B", 14)
            ws.set_column("C:F", 11)
            ws.set_column("G:G", 3)
            ws.set_column("H:L", 13)
            ws.set_column("M:M", 12)
            ws.set_column("N:N", 34)
            ws.set_column("O:O", 50)

        print(f"Saved Excel report: {output_path}")
        return output_path


def main_with_excel():
    """
    Run all ETFs and create the Excel workbook.
    """
    all_details = []
    summaries = []
    failures = []

    for etf in ETFS:
        try:
            details, summary = run_one_etf(etf)
            all_details.append(details)
            summaries.append(summary)

        except Exception as e:
            print("\n" + "!" * 72)
            print(f"FAILED: {etf}")
            print(repr(e))
            print("!" * 72)
            failures.append({"ETF": etf, "Error": repr(e)})

    if summaries and all_details:
        export_excel_report(all_details, summaries)

    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"{f['ETF']}: {f['Error']}")

    return all_details, summaries, failures


# ============================================================
# JUPYTER RUN OPTIONS
# ============================================================

# In Jupyter, run everything and generate only the Excel report:
# all_details, summaries, failures = main_with_excel()
#
# Or just run:
# main_with_excel()
#
# Or only one ETF first to test in memory:
# smh_details, smh_summary = run_one_etf("SMH")
# display(smh_details.head(20))
#
# mags_details, mags_summary = run_one_etf("MAGS")
# display(mags_details.head(20))
#
# If you already have all_details + summaries in memory:
# export_excel_report(all_details, summaries)



# ============================================================
# OVERRIDES: exact live-source fixes for SMH and MAGS
# Added after the original functions so they replace earlier definitions.
# ============================================================

# Use the exact SMH source requested by the user: VanEck offshore UCITS page.
ETF_CONFIG["SMH"]["issuer"] = "VanEck Offshore UCITS"
ETF_CONFIG["SMH"]["url"] = "https://www.vaneck.com/offshore/en/investments/semiconductor-etf/holdings/"
ETF_CONFIG["SMH"]["backup_urls"] = [
    # Same UCITS ETF, different country route. This often loads when offshore blocks scripts/region cookies.
    "https://www.vaneck.com/lu/en/investments/semiconductor-etf/portfolio/",
]

# Roundhill live page requested by the user.
ETF_CONFIG["MAGS"]["issuer"] = "Roundhill live Top Holdings"
ETF_CONFIG["MAGS"]["url"] = "https://www.roundhillinvestments.com/etf/mags/"
ETF_CONFIG["MAGS"]["factsheet_url"] = "https://www.roundhillinvestments.com/assets/pdfs/MAGS_Factsheet.pdf"

# These two pages render holdings with JavaScript. Requests alone often returns placeholders.
USE_PLAYWRIGHT_FOR_DYNAMIC_HOLDINGS = True
AUTO_INSTALL_PLAYWRIGHT_IF_MISSING = True
PLAYWRIGHT_TIMEOUT_SECONDS = 120


def ensure_playwright_available():
    """
    Roundhill and VanEck may render holdings dynamically.
    In Jupyter this function auto-installs Playwright if missing.
    """
    try:
        import playwright  # noqa: F401
        return
    except Exception:
        pass

    if not AUTO_INSTALL_PLAYWRIGHT_IF_MISSING:
        raise RuntimeError(
            "Playwright is required for live Roundhill/VanEck holdings. Run:\n"
            "!pip install playwright\n"
            "!python -m playwright install chromium"
        )

    import subprocess
    import sys

    print("Playwright not found. Installing playwright + chromium browser...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


def fetch_rendered_artifacts(url, click_texts=None, download_texts=None, wait_seconds=8):
    """
    Fetch rendered HTML/text using Playwright in a subprocess.
    Subprocess avoids Jupyter's common sync_playwright event-loop error.

    Returns dict with html, text, optional download_bytes, and download_name.
    """
    if not USE_PLAYWRIGHT_FOR_DYNAMIC_HOLDINGS:
        raise RuntimeError("USE_PLAYWRIGHT_FOR_DYNAMIC_HOLDINGS is False")

    ensure_playwright_available()

    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    click_texts = click_texts or []
    download_texts = download_texts or []

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        script_path = td / "render_page.py"
        html_path = td / "page.html"
        text_path = td / "page.txt"
        meta_path = td / "meta.json"
        download_path = td / "download.bin"

        script_code = r'''
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

url = sys.argv[1]
html_path = Path(sys.argv[2])
text_path = Path(sys.argv[3])
meta_path = Path(sys.argv[4])
download_path = Path(sys.argv[5])
click_texts = json.loads(sys.argv[6])
download_texts = json.loads(sys.argv[7])
wait_seconds = float(sys.argv[8])

meta = {"downloaded": False, "download_name": None, "errors": []}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        viewport={"width": 1500, "height": 1200},
        accept_downloads=True,
    )
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # Handle common VanEck / cookie / disclosure modals.
    common_clicks = [
        "I agree",
        "Accept & Continue",
        "Accept and Continue",
        "Accept",
        "Agree",
        "Close important disclosure",
        "Close",
        "No Thanks",
    ]

    for txt in common_clicks:
        try:
            loc = page.get_by_text(txt, exact=False).first
            if loc.count() > 0:
                loc.click(timeout=2500)
                page.wait_for_timeout(700)
        except Exception:
            pass

    # Scroll to force lazy-loaded tables/widgets.
    for _ in range(5):
        try:
            page.evaluate("window.scrollBy(0, Math.floor(document.body.scrollHeight / 5));")
            page.wait_for_timeout(900)
        except Exception:
            pass

    # Click tabs/sections requested by caller.
    for txt in click_texts:
        try:
            loc = page.get_by_text(txt, exact=False).first
            if loc.count() > 0:
                loc.click(timeout=3500)
                page.wait_for_timeout(1200)
        except Exception as e:
            meta["errors"].append(f"click {txt}: {e}")

    # More scroll after clicks.
    for _ in range(4):
        try:
            page.evaluate("window.scrollBy(0, Math.floor(document.body.scrollHeight / 4));")
            page.wait_for_timeout(900)
        except Exception:
            pass

    # Try to download CSV/XLSX if caller asked for it.
    for txt in download_texts:
        if meta["downloaded"]:
            break
        try:
            loc = page.get_by_text(txt, exact=False).first
            if loc.count() > 0:
                with page.expect_download(timeout=6000) as download_info:
                    loc.click(timeout=3500)
                dl = download_info.value
                dl.save_as(str(download_path))
                meta["downloaded"] = True
                meta["download_name"] = dl.suggested_filename
                page.wait_for_timeout(500)
        except Exception as e:
            meta["errors"].append(f"download {txt}: {e}")

    page.wait_for_timeout(int(wait_seconds * 1000))

    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception as e:
        meta["errors"].append(f"write html: {e}")

    try:
        text_path.write_text(page.locator("body").inner_text(timeout=5000), encoding="utf-8")
    except Exception as e:
        meta["errors"].append(f"write text: {e}")

    browser.close()

meta_path.write_text(json.dumps(meta), encoding="utf-8")
'''
        script_path.write_text(script_code, encoding="utf-8")

        cmd = [
            sys.executable,
            str(script_path),
            url,
            str(html_path),
            str(text_path),
            str(meta_path),
            str(download_path),
            json.dumps(click_texts),
            json.dumps(download_texts),
            str(wait_seconds),
        ]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PLAYWRIGHT_TIMEOUT_SECONDS,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                "Playwright render failed.\n"
                f"STDOUT:\n{proc.stdout[:2000]}\n"
                f"STDERR:\n{proc.stderr[:4000]}"
            )

        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        out = {
            "html": html_path.read_text(encoding="utf-8") if html_path.exists() else "",
            "text": text_path.read_text(encoding="utf-8") if text_path.exists() else "",
            "download_bytes": None,
            "download_name": meta.get("download_name"),
            "errors": meta.get("errors", []),
        }

        if download_path.exists() and download_path.stat().st_size > 0:
            out["download_bytes"] = download_path.read_bytes()

        return out


def try_tables_from_html_for_candidate(html, etf, validator):
    errors = []
    try:
        tables = pd.read_html(StringIO(html))
    except Exception as e:
        raise ValueError(f"No HTML tables found for {etf}: {e}")

    for df in tables:
        try:
            df = df.copy()
            df.columns = make_unique_columns(df.columns)
            return validator(df, f"{etf} rendered HTML table")
        except Exception as e:
            errors.append(repr(e))

    raise ValueError(f"No valid {etf} holdings table from rendered HTML. " + " | ".join(errors[:8]))


def extract_pdf_text_from_bytes(pdf_bytes):
    # Optional fallback for Roundhill factsheet only.
    try:
        from pypdf import PdfReader
    except Exception:
        try:
            from PyPDF2 import PdfReader
        except Exception as e:
            raise ValueError(
                "PDF text fallback requires pypdf or PyPDF2. Install with: !pip install pypdf"
            ) from e

    reader = PdfReader(BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pass
    return "\n".join(pages)


# ---------------- MAGS live Top Holdings parser ----------------

def standardize_mags_candidate(raw, source_name):
    test = normalize_holdings(raw, "MAGS")

    required = {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"}
    got = set(test["Yahoo Ticker"].astype(str))
    missing = required - got

    if missing:
        raise ValueError(f"MAGS {source_name}: missing required MAGS tickers: {sorted(missing)}")

    total = test["Weight"].sum()
    if total < 95 or total > 105:
        raise ValueError(f"MAGS {source_name}: parsed total weight looks wrong: {total:.2f}%")

    return raw


def parse_mags_top_holdings_from_text(text, source_name="Roundhill page text"):
    """
    Parse Roundhill Top Holdings visible text.
    Works for both live page rows like Alphabet GOOGL 16.44%
    and factsheet rows like Meta Platforms Inc 14.34%.
    """
    if not text:
        raise ValueError("No text to parse for MAGS.")

    clean = re.sub(r"\s+", " ", text)
    lower = clean.lower()
    starts = [m.start() for m in re.finditer(r"top holdings", lower)]

    segments = []
    for s in starts:
        e_candidates = []
        for marker in ["performance", "distributions", "premium/discount", "fund exposures", "sector breakdown", "faq"]:
            j = lower.find(marker, s + 20)
            if j > s:
                e_candidates.append(j)
        e = min(e_candidates) if e_candidates else min(len(clean), s + 2500)
        segments.append(clean[s:e])

    if not segments:
        segments = [clean]

    best_rows = []

    for seg in segments:
        rows = []
        used = set()

        ticker_to_name = {
            "AAPL": "Apple Inc",
            "MSFT": "Microsoft Corp",
            "GOOGL": "Alphabet Inc",
            "AMZN": "Amazon.com Inc",
            "META": "Meta Platforms Inc",
            "NVDA": "NVIDIA Corp",
            "TSLA": "Tesla Inc",
        }

        # Explicit ticker rows, e.g. Alphabet GOOGL 16.44%
        for ticker, name in ticker_to_name.items():
            ticker_re = re.escape(ticker)
            pattern = rf"([A-Za-z0-9 .,&'\-/]{{2,80}}?)\s+{ticker_re}\s+(-?\d{{1,3}}(?:\.\d+)?)\s*%"
            m = re.search(pattern, seg, flags=re.I)
            if m:
                rows.append({"Ticker": ticker, "Name": name, "Weight": float(m.group(2))})
                used.add(ticker)

        # Name rows, e.g. Meta Platforms Inc 14.34%
        name_patterns = [
            ("GOOGL", ["Alphabet Inc", "Alphabet"]),
            ("AMZN", ["Amazon.com Inc", "Amazon"]),
            ("AAPL", ["Apple Inc", "Apple"]),
            ("META", ["Meta Platforms Inc", "Meta"]),
            ("MSFT", ["Microsoft Corp", "Microsoft"]),
            ("NVDA", ["NVIDIA Corp", "NVIDIA", "NVidia"]),
            ("TSLA", ["Tesla Inc", "Tesla"]),
        ]

        for ticker, names in name_patterns:
            if ticker in used:
                continue
            for name in names:
                pattern = rf"{re.escape(name)}\s+(?:{ticker}\s+)?(-?\d{{1,3}}(?:\.\d+)?)\s*%"
                m = re.search(pattern, seg, flags=re.I)
                if m:
                    rows.append({"Ticker": ticker, "Name": name, "Weight": float(m.group(1))})
                    used.add(ticker)
                    break

        if len(rows) > len(best_rows):
            best_rows = rows

    if len(best_rows) < 7:
        raise ValueError(f"MAGS text parser found only {len(best_rows)} of 7 holdings from {source_name}.")

    raw = pd.DataFrame(best_rows).drop_duplicates(subset=["Ticker"], keep="first")
    return standardize_mags_candidate(raw, source_name)


def parse_mags_from_download_bytes(content_bytes, source_name):
    dfs = read_any_file_to_tables(content_bytes)
    errors = []

    for df in dfs:
        try:
            df = df.copy()
            df.columns = make_unique_columns(df.columns)
            return standardize_mags_candidate(df, source_name)
        except Exception as e:
            errors.append(repr(e))

    try:
        text = content_bytes.decode("utf-8-sig", errors="ignore")
        return parse_mags_top_holdings_from_text(text, source_name)
    except Exception as e:
        errors.append(repr(e))

    raise ValueError(f"Could not parse MAGS download bytes from {source_name}. " + " | ".join(errors[:8]))


def try_roundhill_live_page_for_mags():
    url = ETF_CONFIG["MAGS"]["url"]

    try:
        r = requests_get(url)
        try:
            return try_tables_from_html_for_candidate(r.text, "MAGS", standardize_mags_candidate)
        except Exception:
            pass
        try:
            text = BeautifulSoup(r.text, "html.parser").get_text("\n", strip=True)
            return parse_mags_top_holdings_from_text(text, "Roundhill requests HTML text")
        except Exception:
            pass
    except Exception:
        pass

    art = fetch_rendered_artifacts(
        url,
        click_texts=["Top Holdings"],
        download_texts=["Download CSV", "CSV"],
        wait_seconds=5,
    )

    if art.get("download_bytes"):
        try:
            return parse_mags_from_download_bytes(art["download_bytes"], "Roundhill live Download CSV")
        except Exception as e:
            print("MAGS live CSV download parsed badly:", repr(e))

    try:
        return try_tables_from_html_for_candidate(art.get("html", ""), "MAGS", standardize_mags_candidate)
    except Exception as e:
        print("MAGS rendered HTML table parsed badly:", repr(e))

    return parse_mags_top_holdings_from_text(art.get("text", ""), "Roundhill rendered live page text")


def try_roundhill_factsheet_for_mags():
    """
    Same-issuer fallback, not equal-weight approximation.
    Uses Roundhill's factsheet top-holdings if the live page cannot be rendered.
    """
    url = ETF_CONFIG["MAGS"].get("factsheet_url")
    if not url:
        raise ValueError("No Roundhill factsheet URL configured.")

    r = requests_get(url)
    text = extract_pdf_text_from_bytes(r.content)
    raw = parse_mags_top_holdings_from_text(text, "Roundhill factsheet PDF Top Holdings")
    print("WARNING: MAGS live page was not usable. Used Roundhill factsheet Top Holdings instead.")
    return raw


def pull_mags_roundhill_issuer_page():
    """
    Pull MAGS using Roundhill's actual Top Holdings weights.
    Important: this function does NOT fall back to equal 100/7 anymore.
    """
    errors = []

    for label, func in [
        ("Roundhill live Top Holdings page", try_roundhill_live_page_for_mags),
        ("Roundhill factsheet PDF Top Holdings", try_roundhill_factsheet_for_mags),
    ]:
        try:
            raw = func()
            print(f"MAGS holdings source used: {label}")
            return raw
        except Exception as e:
            errors.append(f"{label}: {repr(e)}")

    raise ValueError(
        "MAGS actual Roundhill Top Holdings weights could not be parsed. "
        "I refused to use the old equal-split 100/7 fallback. Attempts:\n - "
        + "\n - ".join(errors)
    )


# ---------------- SMH VanEck offshore UCITS parser ----------------

def standardize_smh_candidate(raw, source_name):
    test = normalize_holdings(raw, "SMH")

    if len(test) < 20:
        raise ValueError(f"SMH {source_name}: only {len(test)} usable equity rows found; expected around 25.")

    total = test["Weight"].sum()
    if total < 90 or total > 105:
        raise ValueError(f"SMH {source_name}: parsed total weight looks wrong: {total:.2f}%")

    return raw


def parse_smh_from_download_bytes(content_bytes, source_name):
    dfs = read_any_file_to_tables(content_bytes)
    errors = []

    for df in dfs:
        try:
            df = df.copy()
            df.columns = make_unique_columns(df.columns)
            return standardize_smh_candidate(df, source_name)
        except Exception as e:
            errors.append(repr(e))

    raise ValueError(f"Could not parse SMH downloaded holdings from {source_name}. " + " | ".join(errors[:8]))


def parse_smh_holdings_from_text(text, source_name="VanEck rendered text"):
    """
    Fallback parser for dynamic VanEck tables when they render as div text rather than <table>.
    """
    if not text:
        raise ValueError("No text to parse for SMH.")

    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines()]
    lines = [x for x in lines if x]

    lower_lines = [x.lower() for x in lines]
    start = next((i for i, x in enumerate(lower_lines) if x == "holdings" or x.strip() == "holdings"), 0)
    end = len(lines)
    for marker in ["portfolio", "documents", "index", "main risks"]:
        j = next((i for i in range(start + 1, len(lines)) if lower_lines[i].strip() == marker), None)
        if j:
            end = min(end, j)
    segment = lines[start:end]

    rows = []

    joined = "\n".join(segment)
    for m in re.finditer(
        r"\b(?P<ticker>[A-Z0-9.\-]{1,10}(?:\s+[A-Z]{2})?)\s+"
        r"(?P<name>[A-Z][A-Za-z0-9 &.,'\-/]{2,90}?)\s+"
        r"(?P<weight>\d{1,2}(?:\.\d+)?)\s*%",
        joined,
    ):
        ticker = m.group("ticker").strip()
        name = m.group("name").strip()
        weight = float(m.group("weight"))

        if not looks_like_bad_row(f"{ticker} {name}") and weight > 0:
            rows.append({"Ticker": ticker, "Name": name, "Weight": weight})

    ticker_re = re.compile(r"^[A-Z0-9.\-]{1,10}(?:\s+[A-Z]{2})?$")
    weight_re = re.compile(r"^(\d{1,2}(?:\.\d+)?)\s*%$")

    for i, line in enumerate(segment):
        if not ticker_re.match(line):
            continue

        ticker = line
        name = None
        weight = None

        for j in range(i + 1, min(i + 7, len(segment))):
            if name is None and not weight_re.match(segment[j]) and not ticker_re.match(segment[j]):
                name = segment[j]
            wm = weight_re.match(segment[j])
            if wm:
                weight = float(wm.group(1))
                break

        if name and weight is not None and not looks_like_bad_row(f"{ticker} {name}"):
            rows.append({"Ticker": ticker, "Name": name, "Weight": weight})

    if not rows:
        raise ValueError(f"SMH text parser found no rows from {source_name}.")

    raw = pd.DataFrame(rows)
    raw = raw.drop_duplicates(subset=["Ticker", "Name", "Weight"]).reset_index(drop=True)

    return standardize_smh_candidate(raw, source_name)


def try_vaneck_page_for_smh(url):
    """
    Try requests and rendered Playwright page for the VanEck UCITS SMH page.
    """
    errors = []

    try:
        r = requests_get(url)
        try:
            return try_tables_from_html_for_candidate(r.text, "SMH", standardize_smh_candidate)
        except Exception as e:
            errors.append(f"requests html table: {repr(e)}")

        try:
            text = BeautifulSoup(r.text, "html.parser").get_text("\n", strip=True)
            return parse_smh_holdings_from_text(text, f"VanEck requests text {url}")
        except Exception as e:
            errors.append(f"requests text: {repr(e)}")

        soup = BeautifulSoup(r.text, "html.parser")
        links = []
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            label = a.get_text(" ", strip=True).lower()
            combo = f"{href} {label}".lower()
            if any(x in combo for x in ["holdings", "download", "csv", "xlsx"]):
                full = urljoin(url, href)
                if full not in links:
                    links.append(full)

        for dl_url in links:
            try:
                rr = requests_get(dl_url)
                if len(rr.content) > 200:
                    return parse_smh_from_download_bytes(rr.content, f"VanEck linked download {dl_url}")
            except Exception as e:
                errors.append(f"linked download {dl_url}: {repr(e)}")

    except Exception as e:
        errors.append(f"requests page: {repr(e)}")

    art = fetch_rendered_artifacts(
        url,
        click_texts=["Holdings", "View all", "View All", "Portfolio"],
        download_texts=["Download CSV", "Download XLSX", "Export", "Export data", "Download"],
        wait_seconds=6,
    )

    if art.get("download_bytes"):
        try:
            return parse_smh_from_download_bytes(art["download_bytes"], f"VanEck rendered download {url}")
        except Exception as e:
            errors.append(f"rendered download: {repr(e)}")

    try:
        return try_tables_from_html_for_candidate(art.get("html", ""), "SMH", standardize_smh_candidate)
    except Exception as e:
        errors.append(f"rendered html table: {repr(e)}")

    try:
        return parse_smh_holdings_from_text(art.get("text", ""), f"VanEck rendered text {url}")
    except Exception as e:
        errors.append(f"rendered text: {repr(e)}")

    raise ValueError(f"SMH VanEck page failed for {url}. " + " | ".join(errors[:12]))


def pull_vaneck_smh_page():
    """
    Pull SMH from the requested VanEck offshore UCITS holdings page.
    Important: this is NOT the old U.S. SMH XLSX endpoint.
    """
    urls = [ETF_CONFIG["SMH"]["url"]] + ETF_CONFIG["SMH"].get("backup_urls", [])
    errors = []

    for url in urls:
        try:
            raw = try_vaneck_page_for_smh(url)
            print(f"SMH holdings source used: {url}")
            return raw
        except Exception as e:
            errors.append(f"{url}: {repr(e)}")

    raise ValueError(
        "SMH VanEck offshore/UCITS holdings could not be parsed. "
        "I refused to use the old U.S. SMH XLSX endpoint because you asked for the offshore page. Attempts:\n - "
        + "\n - ".join(errors)
    )



# ============================================================
# OVERRIDES: QQQ + PE ratio + VIX summary + cleaner detail sheets
# Added after source-specific parsers so these replace earlier definitions.
# ============================================================

import numpy as np

# Add QQQ and use CompaniesMarketCap holdings page.
if "QQQ" not in ETFS:
    ETFS.append("QQQ")

ETF_CONFIG["QQQ"] = {
    "issuer": "CompaniesMarketCap",
    "url": "https://companiesmarketcap.com/invesco-qqq-trust/holdings/",
}


def pull_companiesmarketcap_holdings_generic(etf, min_rows=20):
    """
    Pull ETF holdings from CompaniesMarketCap visible holdings page.
    Works for SPMO and QQQ style pages with columns:
    Weight %, Name, Ticker, Shares Held.
    """
    url = ETF_CONFIG[etf]["url"]
    r = requests_get(url)

    table_error = None

    # Method 1: pandas HTML tables if available.
    try:
        tables = pd.read_html(StringIO(r.text))
        candidates = []

        for df in tables:
            df = df.copy()
            df.columns = make_unique_columns(df.columns)
            cols_lower = " ".join(str(c).lower() for c in df.columns)

            has_weight = "weight" in cols_lower or "%" in cols_lower
            has_ticker = "ticker" in cols_lower or "symbol" in cols_lower
            has_name = "name" in cols_lower or "company" in cols_lower

            if has_weight and has_ticker and has_name:
                candidates.append(df)

        if candidates:
            raw = choose_best_holdings_table(candidates)
            raw.columns = make_unique_columns(raw.columns)

            rename_map = {}
            for c in raw.columns:
                lc = str(c).strip().lower()
                if lc in ["weight %", "weight", "%"] or ("weight" in lc and "%" in lc):
                    rename_map[c] = "Weight"
                elif lc in ["symbol", "ticker"]:
                    rename_map[c] = "Ticker"
                elif lc in ["company", "company name", "name"]:
                    rename_map[c] = "Name"
                elif "shares" in lc:
                    rename_map[c] = "Shares Held"

            raw = raw.rename(columns=rename_map)
            test = normalize_holdings(raw, etf)

            if len(test) >= min_rows:
                return raw

            raise ValueError(f"{etf} CompaniesMarketCap HTML table parsed only {len(test)} rows.")

    except Exception as e:
        table_error = repr(e)

    # Method 2: parse visible text lines.
    soup = BeautifulSoup(r.text, "html.parser")
    lines = [x.strip() for x in soup.get_text("\n", strip=True).splitlines() if x.strip()]

    rows = []
    i = 0
    while i + 3 < len(lines):
        weight = parse_num(lines[i])

        if weight is not None and "%" in lines[i]:
            name = lines[i + 1].strip()
            ticker = lines[i + 2].strip()
            shares = parse_num(lines[i + 3])
            ticker_ok = bool(re.match(r"^[A-Z0-9.\-]{1,12}$", ticker))

            if ticker_ok and name and shares is not None:
                rows.append({
                    "Ticker": ticker,
                    "Name": name,
                    "Weight": weight,
                    "Shares Held": shares,
                })
                i += 4
                continue

        i += 1

    if not rows:
        debug_path = OUTPUT_DIR / f"{etf}_companiesmarketcap_debug.html"
        debug_path.write_text(r.text, encoding="utf-8")
        raise ValueError(
            f"Could not parse {etf} holdings from CompaniesMarketCap. "
            f"HTML-table error was: {table_error}. Saved debug HTML to {debug_path}"
        )

    raw = pd.DataFrame(rows).drop_duplicates(subset=["Ticker", "Name", "Weight", "Shares Held"])
    test = normalize_holdings(raw, etf)

    if len(test) < min_rows:
        raise ValueError(f"{etf} CompaniesMarketCap parse looked incomplete. Parsed only {len(test)} rows.")

    return raw


def pull_qqq_companiesmarketcap():
    """Pull QQQ holdings from CompaniesMarketCap."""
    return pull_companiesmarketcap_holdings_generic("QQQ", min_rows=90)


# Replace SPMO with the generic parser too; same logic but clearer and reusable.
def pull_spmo_companiesmarketcap():
    """Pull SPMO holdings from CompaniesMarketCap."""
    return pull_companiesmarketcap_holdings_generic("SPMO", min_rows=80)


def pull_issuer_holdings(etf):
    etf = etf.upper()

    if etf == "TECH":
        etf = "TECH.TO"

    if etf == "CHPS":
        etf = "CHPS.TO"

    print(f"\nPulling {etf} issuer holdings...")
    print(f"Issuer: {ETF_CONFIG[etf]['issuer']}")

    if etf == "SPMO":
        raw = pull_spmo_companiesmarketcap()
    elif etf == "QQQ":
        raw = pull_qqq_companiesmarketcap()
    elif etf == "MAGS":
        raw = pull_mags_roundhill_issuer_page()
    elif etf == "TECH.TO":
        raw = pull_evolve_tech_csv()
    elif etf == "CHPS.TO":
        raw = pull_globalx_chps_page()
    elif etf == "SOXX":
        raw = pull_blackrock_soxx()
    elif etf == "SMH":
        raw = pull_vaneck_smh_page()
    elif etf == "XLK":
        raw = pull_ssga_xlk()
    else:
        raise ValueError(f"No ETF config found for {etf}")

    holdings = normalize_holdings(raw, etf)

    if etf == "MAGS":
        sanity_check_weight_total(holdings, etf, low=90, high=120)
    elif etf == "SMH":
        sanity_check_weight_total(holdings, etf, low=90, high=105)
    elif etf in ["SPMO", "QQQ"]:
        # CompaniesMarketCap pages can include small cash/futures rows which are removed,
        # so do not require exactly 100%.
        sanity_check_weight_total(holdings, etf, low=85, high=105)

    holdings["Source Note"] = f"Issuer-first: {ETF_CONFIG[etf]['issuer']}"

    print(f"{etf}: normalized holdings = {len(holdings)}")
    print(f"{etf}: total normalized weight = {holdings['Weight'].sum():.2f}%")

    return holdings


# ============================================================
# PE ratio support
# ============================================================

def _safe_get_info_value(ticker_obj, keys):
    """Safely read yfinance info values without breaking the run."""
    try:
        info = ticker_obj.get_info()
    except Exception:
        try:
            info = ticker_obj.info
        except Exception:
            info = {}

    if not isinstance(info, dict):
        return None

    for k in keys:
        v = info.get(k)
        if v is not None and not pd.isna(v):
            return v

    return None


def pull_yahoo_targets(symbol):
    out = {
        "Yahoo Ticker": symbol,
        "Current Price": None,
        "Target Low": None,
        "Target Mean": None,
        "Target High": None,
        "Target Median": None,
        "Analyst Count": None,
        "Trailing PE": None,
        "Forward PE": None,
        "Yahoo Error": None,
    }

    try:
        t = yf.Ticker(symbol)

        try:
            targets = t.get_analyst_price_targets()

            if isinstance(targets, dict):
                out["Target Low"] = targets.get("low")
                out["Target Mean"] = targets.get("mean")
                out["Target High"] = targets.get("high")
                out["Target Median"] = targets.get("median")
                out["Analyst Count"] = targets.get("numberOfAnalysts")

            elif isinstance(targets, pd.DataFrame) and not targets.empty:
                row = targets.iloc[0]
                out["Target Low"] = row.get("low")
                out["Target Mean"] = row.get("mean")
                out["Target High"] = row.get("high")
                out["Target Median"] = row.get("median")
                out["Analyst Count"] = row.get("numberOfAnalysts")

        except Exception as e:
            out["Yahoo Error"] = f"target error: {e}"

        try:
            fast = t.fast_info
            if hasattr(fast, "get"):
                out["Current Price"] = fast.get("last_price")
            else:
                out["Current Price"] = getattr(fast, "last_price", None)
        except Exception:
            pass

        if out["Current Price"] is None or pd.isna(out["Current Price"]):
            hist = t.history(period="5d", auto_adjust=False)
            if hist is not None and not hist.empty:
                out["Current Price"] = hist["Close"].dropna().iloc[-1]

        # PE ratios: Yahoo sometimes exposes only one of these.
        try:
            out["Trailing PE"] = _safe_get_info_value(t, ["trailingPE", "trailingPe"])
            out["Forward PE"] = _safe_get_info_value(t, ["forwardPE", "forwardPe"])
        except Exception as e:
            if out["Yahoo Error"]:
                out["Yahoo Error"] += f" | PE error: {e}"
            else:
                out["Yahoo Error"] = f"PE error: {e}"

    except Exception as e:
        out["Yahoo Error"] = str(e)

    return out


def add_yahoo_targets(holdings):
    rows = []
    symbols = sorted(holdings["Yahoo Ticker"].dropna().astype(str).unique())

    for i, symbol in enumerate(symbols, 1):
        print(f"Yahoo analyst targets + PE {i}/{len(symbols)}: {symbol}")
        rows.append(pull_yahoo_targets(symbol))
        time.sleep(YAHOO_SLEEP_SECONDS)

    targets = pd.DataFrame(rows)
    merged = holdings.merge(targets, on="Yahoo Ticker", how="left")

    numeric_cols = [
        "Current Price",
        "Target Low",
        "Target Mean",
        "Target High",
        "Target Median",
        "Analyst Count",
        "Trailing PE",
        "Forward PE",
    ]

    for c in numeric_cols:
        if c in merged.columns:
            merged[c] = pd.to_numeric(merged[c], errors="coerce")

    return merged


def weighted_harmonic_from_column(df, pe_col):
    """
    Calculate ETF-style weighted PE from one PE column.

    This uses the harmonic method:
        weighted PE = covered weight / sum(weight / PE)

    Only positive PE values are used. Negative PE, zero PE, and missing PE are ignored.
    The coverage weight tells you how much of the ETF had usable PE data.
    """
    x = df.copy()

    if pe_col not in x.columns:
        return None, 0.0

    x = x.dropna(subset=[pe_col, "Weight Decimal"])
    x = x[(x[pe_col] > 0) & (x["Weight Decimal"] > 0)]

    if x.empty:
        return None, 0.0

    covered_weight = x["Weight Decimal"].sum()
    denom = (x["Weight Decimal"] / x[pe_col]).sum()

    if denom <= 0:
        return None, covered_weight

    return covered_weight / denom, covered_weight


def weighted_harmonic_pe(df):
    """
    ETF-style portfolio PE.
    Uses trailing PE first; if missing, uses forward PE.
    Harmonic weighted PE = covered weight / sum(weight / PE).
    """
    x = df.copy()

    if "Trailing PE" not in x.columns:
        x["Trailing PE"] = np.nan
    if "Forward PE" not in x.columns:
        x["Forward PE"] = np.nan

    x["PE Used"] = x["Trailing PE"]
    x.loc[x["PE Used"].isna(), "PE Used"] = x.loc[x["PE Used"].isna(), "Forward PE"]

    return weighted_harmonic_from_column(x, "PE Used")


def weighted_forward_pe(df):
    """
    ETF-style weighted forward PE using only Yahoo Forward PE.
    No trailing PE fallback is used here.
    """
    return weighted_harmonic_from_column(df, "Forward PE")


def summarize_etf(df, etf):
    covered = df.dropna(
        subset=["Current Price", "Target Low", "Target Mean", "Target High"]
    ).copy()

    pe_ratio, pe_covered_weight = weighted_harmonic_pe(df)
    forward_pe_ratio, forward_pe_covered_weight = weighted_forward_pe(df)

    return {
        "ETF": etf,
        "Total Holdings Used": len(df),
        "Covered Holdings": len(covered),
        "Total Weight Used": df["Weight Decimal"].sum(),
        "Covered Weight": covered["Weight Decimal"].sum(),
        "Raw Worst Case Return": covered["Weighted Low Return"].sum(),
        "Raw Mean Case Return": covered["Weighted Mean Return"].sum(),
        "Raw Best Case Return": covered["Weighted High Return"].sum(),
        "Raw Median Case Return": covered["Weighted Median Return"].sum(),
        "PE Ratio": pe_ratio,
        "PE Coverage Weight": pe_covered_weight,
        "Forward PE": forward_pe_ratio,
        "Forward PE Coverage Weight": forward_pe_covered_weight,
    }


def print_summary(summary):
    print("\n" + "=" * 72)
    print(f"{summary['ETF']} Analyst Target Implied 1-Year Performance")
    print("=" * 72)

    print(f"Total holdings used: {summary['Total Holdings Used']}")
    print(f"Covered holdings:    {summary['Covered Holdings']}")
    print(f"Total weight used:   {summary['Total Weight Used']:.2%}")
    print(f"Covered weight:      {summary['Covered Weight']:.2%}")

    pe_ratio = summary.get("PE Ratio")
    pe_cov = summary.get("PE Coverage Weight", 0)
    if pe_ratio is not None and pd.notna(pe_ratio):
        print(f"Weighted PE ratio:   {pe_ratio:.2f}x, PE coverage weight {pe_cov:.2%}")
    else:
        print("Weighted PE ratio:   unavailable")

    forward_pe = summary.get("Forward PE")
    forward_pe_cov = summary.get("Forward PE Coverage Weight", 0)
    if forward_pe is not None and pd.notna(forward_pe):
        print(f"Weighted forward PE: {forward_pe:.2f}x, forward PE coverage weight {forward_pe_cov:.2%}")
    else:
        print("Weighted forward PE: unavailable")

    print("\nRaw ETF impact, uncovered holdings treated as 0 contribution:")
    print(f"Worst case:  {summary['Raw Worst Case Return']:.2%}")
    print(f"Mean case:   {summary['Raw Mean Case Return']:.2%}")
    print(f"Best case:   {summary['Raw Best Case Return']:.2%}")
    print(f"Median case: {summary['Raw Median Case Return']:.2%}")


def run_one_etf(etf):
    if etf.upper() == "TECH":
        etf = "TECH.TO"

    if etf.upper() == "CHPS":
        etf = "CHPS.TO"

    holdings = pull_issuer_holdings(etf)
    enriched = add_yahoo_targets(holdings)
    enriched = calculate_returns(enriched)

    summary = summarize_etf(enriched, etf)
    print_summary(summary)

    output_cols = [
        "ETF",
        "Yahoo Ticker",
        "Raw Ticker",
        "Name",
        "Weight",
        "Weight Decimal",
        "Current Price",
        "Target Low",
        "Target Mean",
        "Target High",
        "Target Median",
        "Analyst Count",
        "Trailing PE",
        "Forward PE",
        "Low Return",
        "Mean Return",
        "High Return",
        "Median Return",
        "Weighted Low Return",
        "Weighted Mean Return",
        "Weighted High Return",
        "Weighted Median Return",
        "Shares Held",
        "Identifier",
        "Source Note",
        "Yahoo Error",
    ]

    enriched = (
        enriched[[c for c in output_cols if c in enriched.columns]]
        .sort_values("Weight", ascending=False)
        .reset_index(drop=True)
    )

    return enriched, summary


# ============================================================
# VIX percentile support for the first Excel sheet
# ============================================================

def vix_percentile_stats(data, years):
    end_date = data.index.max()
    start_date = end_date - pd.DateOffset(years=years)
    subset = data[data.index >= start_date]["VIX"].dropna()
    latest_vix = subset.iloc[-1]
    percentile_rank = (subset < latest_vix).mean() * 100

    return {
        "Period": f"Last {years} Years",
        "Start Date": subset.index.min().date(),
        "End Date": subset.index.max().date(),
        "Trading Days": len(subset),
        "Median": subset.median(),
        "30th Percentile": np.percentile(subset, 30),
        "70th Percentile": np.percentile(subset, 70),
        "Mean": subset.mean(),
        "Min": subset.min(),
        "Max": subset.max(),
        "Latest VIX": latest_vix,
        "Percentile Rank": percentile_rank,
    }


def build_vix_summary(periods=(20, 10, 5, 3)):
    try:
        vix = yf.download(
            "^VIX",
            period="25y",
            auto_adjust=False,
            progress=False,
        )

        if vix is None or vix.empty:
            raise ValueError("No VIX data returned from yfinance.")

        # yfinance can return MultiIndex columns in some versions.
        if isinstance(vix.columns, pd.MultiIndex):
            if ("Close", "^VIX") in vix.columns:
                vix = vix[("Close", "^VIX")].to_frame("VIX")
            else:
                close_cols = [c for c in vix.columns if c[0] == "Close"]
                vix = vix[close_cols[0]].to_frame("VIX")
        else:
            vix = vix[["Close"]].rename(columns={"Close": "VIX"})

        vix = vix.dropna()
        vix.index = pd.to_datetime(vix.index)

        summary = pd.DataFrame([vix_percentile_stats(vix, y) for y in periods])

        numeric_cols = [
            "Median",
            "30th Percentile",
            "70th Percentile",
            "Mean",
            "Min",
            "Max",
            "Latest VIX",
            "Percentile Rank",
        ]
        summary[numeric_cols] = summary[numeric_cols].round(2)

        return summary, None

    except Exception as e:
        return pd.DataFrame(), repr(e)


# ============================================================
# Excel summary/detail overrides
# ============================================================

def build_excel_summary_rows(summary_df):
    rows = []

    for _, r in summary_df.iterrows():
        etf = r["ETF"]
        current_price = get_etf_current_price(etf)
        worst_return = r.get("Raw Worst Case Return")

        worst_price = None
        if current_price is not None and pd.notna(worst_return):
            worst_price = current_price * (1 + worst_return)

        rows.append({
            "ETF": etf,
            "reliable": r.get("Covered Weight"),
            "Average": r.get("Raw Mean Case Return"),
            "Median": r.get("Raw Median Case Return"),
            "WORST": r.get("Raw Worst Case Return"),
            "BEST": r.get("Raw Best Case Return"),
            "Current pr": current_price,
            "worst price": worst_price,
            "PE Ratio": r.get("PE Ratio"),
            "PE coverage": r.get("PE Coverage Weight"),
            "Forward PE": r.get("Forward PE"),
            "Forward PE coverage": r.get("Forward PE Coverage Weight"),
        })

    return pd.DataFrame(rows)


def prepare_detail_sheet(details):
    df = details.copy()

    out = pd.DataFrame()
    out["Ticker"] = df["Yahoo Ticker"]
    out["Name"] = df["Name"]
    out["% of Portfolio"] = df["Weight Decimal"]
    out["Worst"] = df["Low Return"]
    out["Average"] = df["Mean Return"]
    out["Median"] = df["Median Return"]
    out["Best"] = df["High Return"]
    out["Worst Target"] = df["Target Low"]
    out["Average Target"] = df["Target Mean"]
    out["Median Target"] = df["Target Median"]
    out["Best Target"] = df["Target High"]
    out["Current"] = df["Current Price"]

    # Keep PE in details for debugging, but do not include Analyst Count in Excel sheet.
    if "Trailing PE" in df.columns:
        out["Trailing PE"] = df["Trailing PE"]
    if "Forward PE" in df.columns:
        out["Forward PE"] = df["Forward PE"]

    if "Source Note" in df.columns:
        out["Source Note"] = df["Source Note"]

    out = out.sort_values("% of Portfolio", ascending=False).reset_index(drop=True)
    return out


def export_excel_report(all_details, summaries, output_path=None, report_date=None):
    """
    Create a formatted Excel report.

    Updates in this version:
    - Summary sheet includes weighted PE ratio and PE coverage.
    - Summary sheet includes weighted forward PE and forward PE coverage.
    - Summary sheet includes VIX percentile analysis block.
    - Detail sheets remove Analyst Count column.
    """
    if output_path is None:
        output_path = OUTPUT_DIR / "ETF_analyst_target_report.xlsx"
    else:
        output_path = Path(output_path)

    if report_date is None:
        today = pd.Timestamp.today()
        report_date = f"{today.month}/{today.day}/{today.year}"

    summary_df = pd.DataFrame(summaries)
    summary_rows = build_excel_summary_rows(summary_df)
    vix_summary, vix_error = build_vix_summary()

    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        workbook = writer.book

        title_fmt = workbook.add_format({
            "bold": True,
            "font_size": 12,
            "align": "center",
            "valign": "vcenter",
            "border": 1,
        })

        section_fmt = workbook.add_format({
            "bold": True,
            "font_size": 12,
            "align": "left",
            "valign": "vcenter",
        })

        header_fmt = workbook.add_format({
            "bold": True,
            "align": "center",
            "valign": "vcenter",
            "border": 1,
            "bg_color": "#E2F0D9",
        })

        header_white_fmt = workbook.add_format({
            "bold": True,
            "align": "center",
            "valign": "vcenter",
            "border": 1,
            "bg_color": "#FFFFFF",
        })

        normal_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
        })

        text_fmt = workbook.add_format({
            "border": 1,
            "align": "left",
            "valign": "vcenter",
        })

        pct_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "0.00%",
        })

        pct_red_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "0.00%",
            "font_color": "#FF0000",
        })

        pct_green_fill_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "0.00%",
            "bg_color": "#C6E0B4",
        })

        money_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "#,##0.00",
        })

        money_red_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "#,##0.00",
            "font_color": "#FF0000",
        })

        number_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "0.00",
        })

        int_fmt = workbook.add_format({
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "#,##0",
        })

        total_fmt = workbook.add_format({
            "bold": True,
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "0.00%",
        })

        # ---------- Summary sheet ----------
        sheet_name = "Summary"
        worksheet = workbook.add_worksheet(sheet_name)
        writer.sheets[sheet_name] = worksheet

        headers = [
            "",
            "reliable",
            "Average",
            "Median",
            "WORST",
            "BEST",
            "Current pr",
            "worst price",
            "PE Ratio",
            "PE coverage",
            "Forward PE",
            "Forward PE coverage",
        ]

        start_row = 3
        start_col = 1

        worksheet.write(start_row, start_col, report_date, header_fmt)
        for j, h in enumerate(headers[1:], start_col + 1):
            worksheet.write(start_row, j, h, header_white_fmt)

        for i, row in summary_rows.iterrows():
            r = start_row + 1 + i
            worksheet.write(r, start_col, row["ETF"], text_fmt)
            worksheet.write_number(r, start_col + 1, row["reliable"] if pd.notna(row["reliable"]) else 0, pct_fmt)
            worksheet.write_number(r, start_col + 2, row["Average"] if pd.notna(row["Average"]) else 0, pct_fmt)
            worksheet.write_number(r, start_col + 3, row["Median"] if pd.notna(row["Median"]) else 0, pct_fmt)
            worksheet.write_number(r, start_col + 4, row["WORST"] if pd.notna(row["WORST"]) else 0, pct_fmt)
            worksheet.write_number(r, start_col + 5, row["BEST"] if pd.notna(row["BEST"]) else 0, pct_fmt)

            if pd.notna(row["Current pr"]):
                worksheet.write_number(r, start_col + 6, row["Current pr"], money_fmt)
            else:
                worksheet.write(r, start_col + 6, "-", normal_fmt)

            if pd.notna(row["worst price"]):
                worksheet.write_number(r, start_col + 7, row["worst price"], money_red_fmt)
            else:
                worksheet.write(r, start_col + 7, "-", normal_fmt)

            if pd.notna(row["PE Ratio"]):
                worksheet.write_number(r, start_col + 8, row["PE Ratio"], number_fmt)
            else:
                worksheet.write(r, start_col + 8, "-", normal_fmt)

            if pd.notna(row["PE coverage"]):
                worksheet.write_number(r, start_col + 9, row["PE coverage"], pct_fmt)
            else:
                worksheet.write(r, start_col + 9, "-", normal_fmt)

            if pd.notna(row["Forward PE"]):
                worksheet.write_number(r, start_col + 10, row["Forward PE"], number_fmt)
            else:
                worksheet.write(r, start_col + 10, "-", normal_fmt)

            if pd.notna(row["Forward PE coverage"]):
                worksheet.write_number(r, start_col + 11, row["Forward PE coverage"], pct_fmt)
            else:
                worksheet.write(r, start_col + 11, "-", normal_fmt)

        end_row = start_row + len(summary_rows)

        worksheet.conditional_format(f"C5:F{end_row + 1}", {
            "type": "cell",
            "criteria": "<",
            "value": 0,
            "format": pct_red_fmt,
        })
        worksheet.conditional_format(f"C5:F{end_row + 1}", {
            "type": "cell",
            "criteria": ">=",
            "value": 0.3,
            "format": pct_green_fill_fmt,
        })

        # ---------- VIX block on first page ----------
        vix_start = end_row + 4
        worksheet.write(vix_start, start_col, "VIX percentile analysis", section_fmt)

        if vix_error:
            worksheet.write(vix_start + 1, start_col, f"VIX data unavailable: {vix_error}", text_fmt)
        else:
            vix_headers = list(vix_summary.columns)
            for j, h in enumerate(vix_headers, start_col):
                worksheet.write(vix_start + 1, j, h, header_white_fmt)

            for i, row in vix_summary.iterrows():
                rr = vix_start + 2 + i
                for j, h in enumerate(vix_headers, start_col):
                    val = row[h]

                    if h in ["Period"]:
                        worksheet.write(rr, j, val, text_fmt)
                    elif h in ["Start Date", "End Date"]:
                        worksheet.write(rr, j, str(val), normal_fmt)
                    elif h == "Trading Days":
                        worksheet.write_number(rr, j, int(val), int_fmt)
                    elif h == "Percentile Rank":
                        worksheet.write_number(rr, j, val / 100.0, pct_fmt)
                    else:
                        worksheet.write_number(rr, j, float(val), number_fmt)

        worksheet.set_column("B:B", 14)
        worksheet.set_column("C:F", 12)
        worksheet.set_column("G:L", 13)
        worksheet.set_column("M:P", 15)
        worksheet.freeze_panes(start_row + 1, 0)

        # ---------- ETF detail sheets ----------
        for details in all_details:
            if details.empty:
                continue

            etf_values = details["ETF"].dropna().astype(str).unique()
            etf = etf_values[0] if len(etf_values) > 0 else f"ETF_{len(writer.sheets)}"

            safe_sheet_base = (
                etf.replace(".", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("?", "_")
                .replace("*", "_")
                .replace("[", "_")
                .replace("]", "_")
                .replace(":", "_")
            )
            safe_sheet_base = safe_sheet_base[:31] if safe_sheet_base else f"ETF_{len(writer.sheets)}"

            safe_sheet = safe_sheet_base
            counter = 1
            used_sheets_lower = {name.lower() for name in writer.sheets.keys()}

            while safe_sheet.lower() in used_sheets_lower:
                suffix = f"_{counter}"
                safe_sheet = safe_sheet_base[:31 - len(suffix)] + suffix
                counter += 1

            detail = prepare_detail_sheet(details)

            ws = workbook.add_worksheet(safe_sheet)
            writer.sheets[safe_sheet] = ws

            # Title / top band
            ws.write(0, 0, report_date, header_white_fmt)
            ws.write(0, 1, "YTD", header_white_fmt)
            ws.write(0, 2, "Worst", header_white_fmt)
            ws.write(0, 3, "Average", header_white_fmt)
            ws.write(0, 4, "Median", header_white_fmt)
            ws.write(0, 5, "Best", header_white_fmt)
            ws.merge_range(0, 7, 0, 11, "Yahoo Finance", title_fmt)

            main_headers = [
                etf,
                "% of Portfolio",
                "Worst",
                "Average",
                "Median",
                "Best",
                "",
                "Worst",
                "Average",
                "Median",
                "Best",
                "Current",
                "Name",
                "Source Note",
            ]

            for c, h in enumerate(main_headers):
                ws.write(1, c, h, header_white_fmt)

            data_start = 2
            for i, row in detail.iterrows():
                r = data_start + i
                ws.write(r, 0, row["Ticker"], text_fmt)
                ws.write_number(r, 1, row["% of Portfolio"] if pd.notna(row["% of Portfolio"]) else 0, pct_fmt)

                for col_idx, col_name in enumerate(["Worst", "Average", "Median", "Best"], start=2):
                    val = row[col_name]
                    if pd.notna(val):
                        ws.write_number(r, col_idx, val, pct_fmt)
                    else:
                        ws.write(r, col_idx, "-", normal_fmt)

                ws.write(r, 6, "", normal_fmt)

                for col_idx, col_name in enumerate(["Worst Target", "Average Target", "Median Target", "Best Target", "Current"], start=7):
                    val = row[col_name]
                    if pd.notna(val):
                        fmt = money_red_fmt if col_name == "Current" else money_fmt
                        ws.write_number(r, col_idx, val, fmt)
                    else:
                        ws.write(r, col_idx, "-", normal_fmt)

                ws.write(r, 12, row["Name"], text_fmt)

                if "Source Note" in detail.columns:
                    ws.write(r, 13, row.get("Source Note", ""), text_fmt)
                else:
                    ws.write(r, 13, "", text_fmt)

            total_row = data_start + len(detail) + 1
            ws.write(total_row, 0, "TOTAL", header_white_fmt)
            ws.write_formula(total_row, 1, f"=SUM(B{data_start + 1}:B{data_start + len(detail)})", total_fmt)
            ws.write_formula(total_row, 2, f"=SUMPRODUCT(B{data_start + 1}:B{data_start + len(detail)},C{data_start + 1}:C{data_start + len(detail)})", total_fmt)
            ws.write_formula(total_row, 3, f"=SUMPRODUCT(B{data_start + 1}:B{data_start + len(detail)},D{data_start + 1}:D{data_start + len(detail)})", total_fmt)
            ws.write_formula(total_row, 4, f"=SUMPRODUCT(B{data_start + 1}:B{data_start + len(detail)},E{data_start + 1}:E{data_start + len(detail)})", total_fmt)
            ws.write_formula(total_row, 5, f"=SUMPRODUCT(B{data_start + 1}:B{data_start + len(detail)},F{data_start + 1}:F{data_start + len(detail)})", total_fmt)

            last_data_excel_row = data_start + len(detail)
            ws.conditional_format(f"C3:F{last_data_excel_row}", {
                "type": "cell",
                "criteria": "<",
                "value": 0,
                "format": pct_red_fmt,
            })
            ws.conditional_format(f"C3:F{last_data_excel_row}", {
                "type": "cell",
                "criteria": ">=",
                "value": 0.3,
                "format": pct_green_fill_fmt,
            })

            ws.freeze_panes(2, 1)
            ws.set_column("A:A", 11)
            ws.set_column("B:B", 14)
            ws.set_column("C:F", 11)
            ws.set_column("G:G", 3)
            ws.set_column("H:L", 13)
            ws.set_column("M:M", 34)
            ws.set_column("N:N", 50)

        print(f"Saved Excel report: {output_path}")
        return output_path


def main_with_excel():
    """Run all ETFs and create the Excel workbook."""
    all_details = []
    summaries = []
    failures = []

    for etf in ETFS:
        try:
            details, summary = run_one_etf(etf)
            all_details.append(details)
            summaries.append(summary)

        except Exception as e:
            print("\n" + "!" * 72)
            print(f"FAILED: {etf}")
            print(repr(e))
            print("!" * 72)
            failures.append({"ETF": etf, "Error": repr(e)})

    if summaries and all_details:
        export_excel_report(all_details, summaries)

    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"{f['ETF']}: {f['Error']}")

    return all_details, summaries, failures

# Jupyter examples:
# all_details, summaries, failures = main_with_excel()
# qqq_details, qqq_summary = run_one_etf("QQQ")
# export_excel_report(all_details, summaries)



# ============================================================
# FINAL OVERRIDE: QQQ + SPMO FROM INVESCO OFFICIAL PAGE VIA BROWSER CAPTURE
# Why this override exists:
# - Direct requests to dng-api.invesco.com may return 406 Not Acceptable.
# - The standalone QQQ script worked because Playwright opened the official page
#   and captured the holdings API response generated by the page.
# - This section reuses that same method inside the full ETF report.
# ============================================================

INVESCO_OFFICIAL_PAGE_URLS = {
    "QQQ": "https://www.invesco.com/qqq-etf/en/about.html",
    "SPMO": "https://www.invesco.com/us/en/financial-products/etfs/invesco-sp-500-momentum-etf.html",
}

# Update config labels/URLs so Source Note and print output are clear.
ETF_CONFIG["QQQ"] = {
    "issuer": "Invesco official page browser-captured holdings API",
    "url": INVESCO_OFFICIAL_PAGE_URLS["QQQ"],
}
ETF_CONFIG["SPMO"] = {
    "issuer": "Invesco official page browser-captured holdings API",
    "url": INVESCO_OFFICIAL_PAGE_URLS["SPMO"],
}


def _inv_clean_col(x):
    return re.sub(r"\s+", " ", str(x).strip())


def _inv_make_unique_columns(cols):
    seen = {}
    out = []
    for c in cols:
        if isinstance(c, tuple):
            c = " ".join(str(x) for x in c if str(x) != "nan")
        c = _inv_clean_col(c)
        if not c or c.lower() == "nan":
            c = "Column"
        if c in seen:
            seen[c] += 1
            out.append(f"{c}.{seen[c]}")
        else:
            seen[c] = 0
            out.append(c)
    return out


def _inv_parse_num(x):
    if x is None or pd.isna(x):
        return None
    s = str(x).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = (
        s.replace(",", "")
        .replace("%", "")
        .replace("$", "")
        .replace("−", "-")
        .replace("—", "")
        .replace("(", "")
        .replace(")", "")
        .strip()
    )
    if s.lower() in ["", "-", "--", "nan", "none", "n/a", "na", "null"]:
        return None
    try:
        val = float(s)
        return -val if neg else val
    except Exception:
        return None


def _inv_normalize_ticker(x):
    if x is None or pd.isna(x):
        return None
    s = str(x).strip().upper()
    s = s.replace(" EQUITY", "").replace(" COMMON STOCK", "").strip()
    s = s.replace("/", ".")
    s = re.sub(r"\s+", "", s)
    bad = {
        "COMPANY", "ALLOCATION", "HIGH", "LOW", "HOLDINGS", "TOTAL", "WEIGHT",
        "DATE", "ASOF", "VIEW", "ALL", "REFRESH", "UNABLE", "LOAD", "DATA",
        "CASH", "USD", "NAV", "INDEX", "FUND",
    }
    if s in bad:
        return None
    # US tickers, including BRK.B style.
    if re.fullmatch(r"[A-Z]{1,6}(?:[.\-][A-Z])?", s):
        return s.replace("-", ".")
    return None


def _inv_looks_like_company_name(x):
    if x is None or pd.isna(x):
        return False
    s = str(x).strip()
    if len(s) < 2:
        return False
    if _inv_normalize_ticker(s):
        return False
    low = s.lower()
    bad_phrases = [
        "allocation", "holdings", "unable to load", "refresh", "view all",
        "as of", "company", "high", "low", "sort", "nasdaq-100",
        "fund holdings", "subject to change", "buy/sell", "recommendations",
        "market value", "shares", "ticker", "weight", "download",
    ]
    return not any(b in low for b in bad_phrases)


def _inv_dataframe_from_rows(rows, source):
    """Normalize possible Invesco holding rows into Ticker/Name/Weight."""
    if not rows:
        return pd.DataFrame()

    raw = pd.DataFrame(rows)
    if raw.empty:
        return pd.DataFrame()

    raw.columns = _inv_make_unique_columns(raw.columns)

    ticker_col = None
    name_col = None
    weight_col = None
    shares_col = None

    ticker_candidates = [
        "ticker", "symbol", "holding ticker", "trading symbol", "ticker symbol",
        "local ticker", "security ticker",
    ]
    name_candidates = [
        "company", "company name", "name", "holding", "holding name",
        "security", "security name", "description", "issuer", "issuer name",
    ]
    weight_candidates = [
        "weight", "allocation", "alloc", "percentage", "percent", "%",
        "% of fund", "% of assets", "% of net assets", "market value weight",
        "fund weight", "portfolio weight",
    ]
    shares_candidates = ["shares", "shares held", "quantity"]

    for c in raw.columns:
        lc = str(c).lower().strip()
        if ticker_col is None and any(x == lc or x in lc for x in ticker_candidates):
            if not any(b in lc for b in ["cusip", "isin", "sedol", "currency"]):
                ticker_col = c
        if name_col is None and any(x == lc or x in lc for x in name_candidates):
            if not any(b in lc for b in ["currency"]):
                name_col = c
        if weight_col is None and any(x == lc or x in lc for x in weight_candidates):
            if not any(b in lc for b in ["market value usd", "marketvalueusd", "notional", "shares", "price", "nav"]):
                weight_col = c
        if shares_col is None and any(x == lc or x in lc for x in shares_candidates):
            shares_col = c

    # Sometimes ticker is a generic identifier column. Detect by values.
    if ticker_col is None:
        for c in raw.columns:
            sample = raw[c].dropna().astype(str).head(30).map(_inv_normalize_ticker)
            if len(sample) and sample.notna().mean() > 0.5:
                ticker_col = c
                break

    # Sometimes weight column is not clearly named. Detect by numeric/% values.
    if weight_col is None:
        best_col = None
        best_score = 0
        for c in raw.columns:
            lc = str(c).lower().strip()
            if any(b in lc for b in ["market value", "notional", "shares", "price", "nav"]):
                continue
            vals = raw[c].dropna().astype(str).head(50)
            if len(vals) == 0:
                continue
            pct_score = vals.str.contains("%", regex=False).mean()
            nums = vals.map(_inv_parse_num)
            numeric_score = nums.notna().mean()
            combined_score = pct_score + numeric_score * 0.5
            if combined_score > best_score:
                best_score = combined_score
                best_col = c
        if best_score > 0.4:
            weight_col = best_col

    if ticker_col is None or weight_col is None:
        return pd.DataFrame()

    if name_col is None:
        name_col = ticker_col

    out = pd.DataFrame({
        "Ticker": raw[ticker_col].map(_inv_normalize_ticker),
        "Name": raw[name_col].astype(str).str.strip(),
        "Weight": raw[weight_col].map(_inv_parse_num),
        "Shares Held": raw[shares_col].map(_inv_parse_num) if shares_col else None,
        "Source": source,
    })

    out = out.dropna(subset=["Ticker", "Weight"])
    out = out[out["Ticker"].astype(str).str.strip() != ""]
    out = out[out["Weight"].abs() > 0.000001]

    if not out.empty and out["Weight"].abs().max() <= 1.5:
        out["Weight"] *= 100

    combined = out["Ticker"].fillna("").astype(str) + " " + out["Name"].fillna("").astype(str)
    out = out[~combined.str.lower().str.contains(
        "cash|treasury|collateral|receivable|payable|total|disclaimer|unable to load|refresh|swap collateral|repo",
        regex=True,
        na=False,
    )]

    if out.empty:
        return pd.DataFrame()

    out = (
        out.groupby("Ticker", as_index=False)
        .agg({
            "Name": lambda x: next((v for v in x if _inv_looks_like_company_name(v)), str(x.iloc[0])),
            "Weight": "sum",
            "Shares Held": "sum",
            "Source": lambda x: "; ".join(sorted(set(map(str, x)))[:3]),
        })
        .sort_values("Weight", ascending=False)
        .reset_index(drop=True)
    )

    return out


def _inv_walk_json_lists(obj):
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            yield obj
        for x in obj:
            yield from _inv_walk_json_lists(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _inv_walk_json_lists(v)


def _inv_parse_json_bytes(content_bytes, source):
    out = []
    try:
        import json
        text = content_bytes.decode("utf-8-sig", errors="ignore")
        obj = json.loads(text)
    except Exception:
        return out

    for rows in _inv_walk_json_lists(obj):
        try:
            df = _inv_dataframe_from_rows(rows, source)
            if not df.empty:
                out.append(df)
        except Exception:
            pass
    return out


def _inv_parse_html_tables(html, source):
    out = []
    try:
        tables = pd.read_html(StringIO(html))
    except Exception:
        return out
    for i, table in enumerate(tables):
        try:
            df = table.copy()
            df.columns = _inv_make_unique_columns(df.columns)
            normalized = _inv_dataframe_from_rows(df.to_dict("records"), f"{source}:html_table_{i}")
            if not normalized.empty:
                out.append(normalized)
        except Exception:
            pass
    return out


def _inv_parse_file_bytes(content_bytes, content_type, source):
    out = []
    ct = (content_type or "").lower()

    # JSON first. The successful standalone script found the Invesco data this way.
    out.extend(_inv_parse_json_bytes(content_bytes, source))

    # Excel files.
    if "excel" in ct or "spreadsheet" in ct or content_bytes[:2] == b"PK":
        try:
            xls = pd.ExcelFile(BytesIO(content_bytes))
            for sheet in xls.sheet_names:
                try:
                    raw = pd.read_excel(BytesIO(content_bytes), sheet_name=sheet)
                    df = _inv_dataframe_from_rows(raw.to_dict("records"), f"{source}:xlsx:{sheet}")
                    if not df.empty:
                        out.append(df)
                except Exception:
                    pass
        except Exception:
            pass

    # CSV/text. Use dtype=str and low_memory=False to avoid noisy DtypeWarning.
    try:
        text = content_bytes.decode("utf-8-sig", errors="ignore")
        for sep in [",", "\t", "|"]:
            for skip in range(0, 10):
                try:
                    raw = pd.read_csv(StringIO(text), sep=sep, skiprows=skip, dtype=str, low_memory=False)
                    if raw.shape[1] >= 2:
                        df = _inv_dataframe_from_rows(raw.to_dict("records"), f"{source}:csv")
                        if not df.empty:
                            out.append(df)
                except Exception:
                    pass
    except Exception:
        pass

    # HTML tables.
    try:
        text = content_bytes.decode("utf-8", errors="ignore")
        out.extend(_inv_parse_html_tables(text, source))
    except Exception:
        pass

    return out


def _inv_parse_text_lines(text, source):
    rows = []
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines()]
    lines = [x for x in lines if x]

    same_line_pattern = re.compile(
        r"^(?P<company>.+?)\s+(?P<ticker>[A-Z]{1,6}(?:[.\-][A-Z])?)\s+(?P<weight>\d{1,2}(?:\.\d+)?)\s*%$"
    )
    for line in lines:
        m = same_line_pattern.match(line)
        if m:
            ticker = _inv_normalize_ticker(m.group("ticker"))
            weight = _inv_parse_num(m.group("weight"))
            company = m.group("company").strip()
            if ticker and weight is not None and _inv_looks_like_company_name(company):
                rows.append({"Ticker": ticker, "Name": company, "Weight": weight})

    for i, line in enumerate(lines):
        if not re.fullmatch(r"\d{1,2}(?:\.\d+)?\s*%", line):
            continue
        weight = _inv_parse_num(line)
        if weight is None:
            continue
        ticker = None
        ticker_idx = None
        company = None
        for j in range(max(0, i - 4), i):
            maybe = _inv_normalize_ticker(lines[j])
            if maybe:
                ticker = maybe
                ticker_idx = j
        if ticker_idx is not None:
            for k in range(ticker_idx - 1, max(-1, ticker_idx - 5), -1):
                if k >= 0 and _inv_looks_like_company_name(lines[k]):
                    company = lines[k]
                    break
        if ticker is None and i >= 2:
            maybe_ticker = _inv_normalize_ticker(lines[i - 1])
            maybe_company = lines[i - 2]
            if maybe_ticker and _inv_looks_like_company_name(maybe_company):
                ticker = maybe_ticker
                company = maybe_company
        if ticker and company:
            rows.append({"Ticker": ticker, "Name": company, "Weight": weight})

    return _inv_dataframe_from_rows(rows, source)


def _inv_choose_best_candidate(candidates, etf):
    min_rows = 90 if etf == "QQQ" else 80
    scored = []
    for df in candidates:
        if df is None or df.empty:
            continue
        df = df.copy().dropna(subset=["Ticker", "Weight"])
        if df.empty:
            continue
        total = df["Weight"].sum()
        n = len(df)
        source_text = " ".join(df.get("Source", pd.Series(dtype=str)).astype(str).head(3).tolist()).lower()
        score = n
        if 90 <= total <= 110:
            score += 200
        if n >= min_rows:
            score += 100
        if "dng-api.invesco.com" in source_text:
            score += 80
        if "holdings/fund" in source_text:
            score += 80
        if n <= 15:
            score -= 100
        scored.append((score, n, total, df))

    if not scored:
        return pd.DataFrame()

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][3].copy()


def _inv_accept_cookies_if_present(page):
    possible_texts = [
        "Accept All", "Accept all", "I Accept", "Accept", "Agree", "Continue", "Allow all",
        "Reject All", "Reject all",
    ]
    for txt in possible_texts:
        try:
            loc = page.get_by_text(txt, exact=False).first
            if loc.count() > 0:
                loc.click(timeout=1500)
                time.sleep(1)
                return
        except Exception:
            pass


def _inv_click_holdings_buttons(page, etf):
    candidates = [
        "View all Holdings",
        "View all holdings",
        "See all holdings",
        "See all Holdings",
        "All QQQ holdings",
        "All holdings",
        "Holdings",
    ]
    for txt in candidates:
        try:
            loc = page.get_by_text(txt, exact=False).first
            if loc.count() > 0:
                loc.scroll_into_view_if_needed(timeout=3000)
                time.sleep(0.5)
                loc.click(timeout=3000)
                time.sleep(2)
        except Exception:
            pass


def _inv_scroll_page(page):
    try:
        height = page.evaluate("document.body.scrollHeight")
    except Exception:
        height = 8000
    y = 0
    step = 700
    while y < height + 1000:
        try:
            page.evaluate(f"window.scrollTo(0, {y})")
        except Exception:
            pass
        time.sleep(0.35)
        y += step
        try:
            height = page.evaluate("document.body.scrollHeight")
        except Exception:
            pass


def pull_invesco_official_page_holdings(etf, headless=True):
    """
    Pull QQQ/SPMO from the official Invesco page using the same browser-capture
    approach that worked in the standalone QQQ script.

    Do not call dng-api directly here; direct requests can return 406.
    """
    etf = etf.upper()
    if etf not in INVESCO_OFFICIAL_PAGE_URLS:
        raise ValueError(f"No Invesco official page URL configured for {etf}")

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except Exception as e:
        raise RuntimeError(
            "Playwright is required for QQQ/SPMO Invesco official-page capture. Run:\n"
            "pip install playwright\n"
            "python -m playwright install chromium"
        ) from e

    from urllib.parse import urlparse
    from datetime import datetime

    page_url = INVESCO_OFFICIAL_PAGE_URLS[etf]
    debug_dir = OUTPUT_DIR / f"{etf}_invesco_page_debug"
    responses_dir = debug_dir / "responses"
    debug_dir.mkdir(exist_ok=True, parents=True)
    responses_dir.mkdir(exist_ok=True, parents=True)

    captured = []
    network_urls = []

    print(f"Opening official Invesco page for {etf}: {page_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1200},
            user_agent=HEADERS.get("User-Agent", "Mozilla/5.0"),
            locale="en-US",
        )
        page = context.new_page()

        def on_response(response):
            url = response.url
            network_urls.append(url)
            low_url = url.lower()
            ct = response.headers.get("content-type", "")
            keywords = [
                "holding", "holdings", "allocation", "fund", "etf", etf.lower(),
                "product", "portfolio", "component", "security", "shareclasses",
            ]
            if not any(k in low_url for k in keywords):
                return
            try:
                body = response.body()
                if body and len(body) > 20:
                    captured.append({"url": url, "content_type": ct, "body": body})
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(page_url, wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)
        _inv_accept_cookies_if_present(page)
        _inv_scroll_page(page)
        _inv_click_holdings_buttons(page, etf)
        _inv_scroll_page(page)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass

        html = page.content()
        try:
            text = page.locator("body").inner_text(timeout=15000)
        except Exception:
            text = ""

        try:
            (debug_dir / "rendered_page.html").write_text(html, encoding="utf-8")
            (debug_dir / "rendered_text.txt").write_text(text, encoding="utf-8")
            (debug_dir / "network_urls.txt").write_text("\n".join(network_urls), encoding="utf-8")
            page.screenshot(path=str(debug_dir / "screenshot.png"), full_page=True)
        except Exception:
            pass

        for i, item in enumerate(captured):
            try:
                parsed_url = urlparse(item["url"])
                suffix = Path(parsed_url.path).suffix or ".bin"
                if len(suffix) > 8:
                    suffix = ".bin"
                (responses_dir / f"response_{i:03d}{suffix}").write_bytes(item["body"])
            except Exception:
                pass

        browser.close()

    candidates = []

    # 1. Captured network responses. This is the reliable path.
    for item in captured:
        candidates.extend(_inv_parse_file_bytes(item["body"], item["content_type"], item["url"]))

    # 2. Rendered HTML tables.
    candidates.extend(_inv_parse_html_tables(html, "rendered_html"))

    # 3. Rendered text fallback.
    text_df = _inv_parse_text_lines(text, "rendered_text")
    if not text_df.empty:
        candidates.append(text_df)

    best = _inv_choose_best_candidate(candidates, etf)

    if best.empty:
        raise RuntimeError(
            f"Could not parse {etf} holdings from Invesco official page. Debug files saved in:\n"
            f"  {debug_dir.resolve()}"
        )

    best = best.copy()
    best["Source"] = best.get("Source", "")
    best["Pulled At"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    best["Page URL"] = page_url

    total_weight = best["Weight"].sum()
    n = len(best)
    print(f"{etf}: browser-captured Invesco holdings rows = {n}")
    print(f"{etf}: browser-captured total weight = {total_weight:.2f}%")

    min_rows = 90 if etf == "QQQ" else 80
    if n < min_rows or not (85 <= total_weight <= 110):
        raise ValueError(
            f"{etf} Invesco official page parse sanity check failed. "
            f"Rows={n}, total weight={total_weight:.2f}%. Debug: {debug_dir.resolve()}"
        )

    # Normalize expected column names for the existing report parser.
    out = best.rename(columns={"Ticker": "Ticker", "Name": "Name", "Weight": "Weight"})
    keep = [c for c in ["Ticker", "Name", "Weight", "Shares Held", "Source", "Pulled At", "Page URL"] if c in out.columns]
    return out[keep].sort_values("Weight", ascending=False).reset_index(drop=True)


def pull_qqq_invesco_browser():
    return pull_invesco_official_page_holdings("QQQ", headless=True)


def pull_spmo_invesco_browser():
    return pull_invesco_official_page_holdings("SPMO", headless=True)


# Override the report's final dispatcher so QQQ/SPMO use the working browser-capture method.
def pull_issuer_holdings(etf):
    etf = etf.upper()

    if etf == "TECH":
        etf = "TECH.TO"

    if etf == "CHPS":
        etf = "CHPS.TO"

    print(f"\nPulling {etf} issuer holdings...")
    print(f"Issuer: {ETF_CONFIG[etf]['issuer']}")

    if etf == "SPMO":
        raw = pull_spmo_invesco_browser()
    elif etf == "QQQ":
        raw = pull_qqq_invesco_browser()
    elif etf == "MAGS":
        raw = pull_mags_roundhill_issuer_page()
    elif etf == "TECH.TO":
        raw = pull_evolve_tech_csv()
    elif etf == "CHPS.TO":
        raw = pull_globalx_chps_page()
    elif etf == "SOXX":
        raw = pull_blackrock_soxx()
    elif etf == "SMH":
        raw = pull_vaneck_smh_page()
    elif etf == "XLK":
        raw = pull_ssga_xlk()
    else:
        raise ValueError(f"No ETF config found for {etf}")

    holdings = normalize_holdings(raw, etf)

    if etf == "MAGS":
        sanity_check_weight_total(holdings, etf, low=90, high=120)
    elif etf == "SMH":
        sanity_check_weight_total(holdings, etf, low=90, high=105)
    elif etf in ["SPMO", "QQQ"]:
        sanity_check_weight_total(holdings, etf, low=85, high=110)

    holdings["Source Note"] = f"Issuer-first: {ETF_CONFIG[etf]['issuer']}"

    print(f"{etf}: normalized holdings = {len(holdings)}")
    print(f"{etf}: total normalized weight = {holdings['Weight'].sum():.2f}%")

    return holdings

# ============================================================
# SCRIPT RUN
# ============================================================

if __name__ == "__main__":
    main_with_excel()
