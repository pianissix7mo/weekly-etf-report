
# ============================================================
# ETF HOLDINGS + YAHOO ANALYST TARGET XLSX REPORT
# Clean complete version
# ============================================================

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
import warnings
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import numpy as np
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
AUTO_INSTALL_PLAYWRIGHT_IF_MISSING = True
PLAYWRIGHT_TIMEOUT_SECONDS = 150

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

INVESCO_OFFICIAL_PAGE_URLS = {
    "QQQ": "https://www.invesco.com/qqq-etf/en/about.html",
    "SPMO": "https://www.invesco.com/us/en/financial-products/etfs/invesco-sp-500-momentum-etf.html",
}

ETF_CONFIG = {
    "SPMO": {"issuer": "Invesco official page browser-captured holdings API", "url": INVESCO_OFFICIAL_PAGE_URLS["SPMO"]},
    "QQQ": {"issuer": "Invesco official page browser-captured holdings API", "url": INVESCO_OFFICIAL_PAGE_URLS["QQQ"]},
    "MAGS": {
        "issuer": "Roundhill live Top Holdings",
        "url": "https://www.roundhillinvestments.com/etf/mags/",
        "factsheet_url": "https://www.roundhillinvestments.com/assets/pdfs/MAGS_Factsheet.pdf",
    },
    "TECH.TO": {
        "issuer": "Evolve ETFs",
        "csv_urls": [
            "https://evolveetfs.com/wp-content/uploads/holdings/TECH.csv",
            "https://evolveetfs.com/wp-content/uploads/holdings/TECH.CSV",
        ],
    },
    "CHPS.TO": {"issuer": "Global X Canada", "url": "https://www.globalx.ca/product/chps"},
    "SOXX": {"issuer": "iShares / BlackRock", "product_id": "239705", "file_name": "SOXX_holdings"},
    "SMH": {
        "issuer": "VanEck US direct XLSX",
        "url": "https://www.vaneck.com/us/en/etf/equity/smh/holdings/download/xlsx/",
        "backup_urls": [
            "https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/holdings/",
            "https://www.vaneck.com/offshore/en/investments/semiconductor-etf/holdings/",
            "https://www.vaneck.com/lu/en/investments/semiconductor-etf/portfolio/",
        ],
    },
    "XLK": {"issuer": "State Street / SSGA", "url": "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-xlk.xlsx"},
}

TICKER_COLS = ["Ticker", "Ticker Symbol", "Symbol", "Trading Symbol", "Holding Ticker", "Bloomberg Ticker", "Exchange Ticker"]
NAME_COLS = ["Name", "Holding", "Holdings", "Holding Name", "Security Name", "Security", "Description", "Company", "Company Name", "Issuer"]
SHARES_COLS = ["Shares", "Shares Held", "Quantity", "Shares/Par Value", "Par Value"]
ID_COLS = ["Identifier", "FIGI", "CUSIP", "ISIN", "SEDOL"]

# ============================================================
# HELPERS
# ============================================================

def clean_col(c):
    return re.sub(r"\s+", " ", str(c).strip())


def make_unique_columns(cols):
    seen, out = {}, []
    for c in cols:
        if isinstance(c, tuple):
            c = " ".join(str(x) for x in c if str(x) != "nan")
        c = clean_col(c)
        if not c or c.lower() == "nan":
            c = "Column"
        if c in seen:
            seen[c] += 1
            out.append(f"{c}.{seen[c]}")
        else:
            seen[c] = 0
            out.append(c)
    return out


def first_series(df, col):
    x = df[col]
    return x.iloc[:, 0] if isinstance(x, pd.DataFrame) else x


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


def parse_num(x):
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except Exception:
        pass
    s = str(x).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.replace(",", "").replace("%", "").replace("$", "").replace("−", "-").replace("—", "").replace("(", "").replace(")", "").replace("x", "").strip()
    if s.lower() in ["", "-", "--", "nan", "none", "n/a", "na", "null"]:
        return None
    try:
        v = float(s)
        return -v if neg else v
    except Exception:
        return None


def safe_float_value(x):
    try:
        if isinstance(x, pd.Series):
            x = x.dropna()
            if x.empty:
                return None
            x = x.iloc[0]
        if isinstance(x, (list, tuple)):
            if not x:
                return None
            x = x[0]
        return parse_num(x)
    except Exception:
        return None


def normalize_growth_rate(x):
    v = safe_float_value(x)
    if v is None:
        return None
    return v / 100.0 if abs(v) > 1.5 else v


def requests_get(url):
    r = requests.get(url, headers=HEADERS, timeout=45)
    r.raise_for_status()
    return r


def find_weight_col_strict(df):
    bad_terms = ["market value", "notional", "net asset value", "total net assets", "shares", "quantity", "price", "nav", "identifier", "figi", "cusip", "isin", "sedol"]
    preferred = ["% of Net Assets", "% of Net Asset", "% of Net Assets (%)", "ETF Weight", "Holding Percent", "Holding %", "Weight", "Weight (%)", "Weight %", "% Weight", "% of Fund", "% of Assets", "Portfolio Weight", "Fund Weight", "Holding Weight", "Allocation"]
    lower_map = {clean_col(c).lower(): c for c in df.columns}
    for name in preferred:
        if name.lower() in lower_map:
            return lower_map[name.lower()]
    for c in df.columns:
        lc = clean_col(c).lower()
        if any(b in lc for b in bad_terms):
            continue
        if "%" in lc and any(k in lc for k in ["asset", "weight", "fund", "portfolio", "allocation"]):
            return c
    for c in df.columns:
        lc = clean_col(c).lower()
        if any(b in lc for b in bad_terms):
            continue
        if any(k in lc for k in ["weight", "percent", "allocation"]):
            return c
    return None


def find_table_in_excel_raw(raw):
    header_row = None
    for i in range(min(160, len(raw))):
        txt = " ".join(raw.iloc[i].dropna().astype(str).str.lower().tolist())
        score = 0
        if "ticker" in txt or "symbol" in txt:
            score += 1
        if any(x in txt for x in ["name", "security", "holding", "company"]):
            score += 1
        if any(x in txt for x in ["weight", "% of", "net assets", "holding percent", "allocation"]):
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
    try:
        xls = pd.ExcelFile(BytesIO(content_bytes))
        for sheet in xls.sheet_names:
            raw = pd.read_excel(BytesIO(content_bytes), sheet_name=sheet, header=None)
            df = find_table_in_excel_raw(raw)
            if df is not None and not df.empty:
                dfs.append(df)
    except Exception:
        pass
    try:
        text = content_bytes.decode("utf-8-sig", errors="ignore")
        for sep in [",", "\t", "|"]:
            for skip in range(0, 35):
                try:
                    df = pd.read_csv(StringIO(text), sep=sep, skiprows=skip, dtype=str, low_memory=False)
                    if df.shape[1] >= 3 and len(df) > 0:
                        dfs.append(df)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        text = content_bytes.decode("utf-8", errors="ignore")
        for df in pd.read_html(StringIO(text)):
            if df.shape[1] >= 3:
                dfs.append(df)
    except Exception:
        pass
    return dfs


def choose_best_holdings_table(dfs):
    best, best_score = None, -1
    for df in dfs:
        if df is None:
            continue
        df = df.copy().dropna(how="all")
        if df.empty or df.shape[1] < 3:
            continue
        df.columns = make_unique_columns(df.columns)
        ticker_col = find_col(df, TICKER_COLS, contains=["ticker", "symbol"])
        name_col = find_col(df, NAME_COLS, contains=["name", "security", "holding", "description", "company"])
        weight_col = find_weight_col_strict(df)
        score = (4 if ticker_col else 0) + (3 if name_col else 0) + (5 if weight_col else 0) + min(len(df), 500) / 100
        if weight_col and any(x in clean_col(weight_col).lower() for x in ["% of net assets", "etf weight", "holding percent", "allocation"]):
            score += 5
        if score > best_score:
            best, best_score = df, score
    if best is None:
        raise ValueError("Could not identify holdings table.")
    return best

# ============================================================
# TICKER NORMALIZATION
# ============================================================

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
    bad_terms = ["cash", "cash equivalent", "treasury", "t-bill", "t bill", "money market", "collateral", "repo", "repurchase", "total", "disclaimer", "receivable", "payable"]
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


def normalize_holdings(raw_df, etf):
    df = raw_df.copy().dropna(how="all")
    df.columns = make_unique_columns(df.columns)
    if df.empty:
        raise ValueError(f"{etf}: issuer holdings table is empty.")
    ticker_col = find_col(df, TICKER_COLS, contains=["ticker", "symbol"])
    name_col = find_col(df, NAME_COLS, contains=["name", "security", "holding", "description", "company"])
    weight_col = find_weight_col_strict(df)
    shares_col = find_col(df, SHARES_COLS, contains=["shares", "quantity"])
    id_col = find_col(df, ID_COLS, contains=["cusip", "isin", "sedol", "identifier", "figi"])
    if weight_col is None:
        raise ValueError(f"{etf}: could not find real weight column. Columns={list(df.columns)}")
    out = pd.DataFrame()
    out["Raw Ticker"] = first_series(df, ticker_col).astype(str).str.strip() if ticker_col else ""
    out["Name"] = first_series(df, name_col).astype(str).str.strip() if name_col else ""
    out["Weight"] = first_series(df, weight_col).map(parse_num)
    out["Shares Held"] = first_series(df, shares_col).map(parse_num) if shares_col else None
    out["Identifier"] = first_series(df, id_col).astype(str).str.strip() if id_col else ""
    out["ETF"] = etf
    out = out.dropna(subset=["Weight"])
    if out.empty:
        raise ValueError(f"{etf}: no usable holdings rows after parsing issuer table.")
    if out["Weight"].abs().max() <= 1.5:
        out["Weight"] *= 100
    out["Yahoo Ticker"] = out.apply(lambda r: map_to_yahoo_symbol(r["Raw Ticker"], r["Name"], r["Identifier"]), axis=1)
    if not INCLUDE_CASH_FUTURES_SWAPS:
        combined = out["Raw Ticker"].fillna("").astype(str) + " " + out["Name"].fillna("").astype(str) + " " + out["Identifier"].fillna("").astype(str)
        out = out[~combined.map(looks_like_bad_row)]
    out = out[out["Yahoo Ticker"].notna()]
    out = out[out["Yahoo Ticker"].astype(str).str.strip() != ""]
    out = out[out["Weight"].abs() > 0.000001]
    if out.empty:
        raise ValueError(f"{etf}: no equity holdings remained after cleaning.")
    return (out.groupby(["ETF", "Yahoo Ticker"], dropna=False)
        .agg({
            "Raw Ticker": lambda x: "; ".join(sorted(set(map(str, x))))[:300],
            "Name": lambda x: "; ".join(sorted(set(map(str, x)))[:8])[:500],
            "Weight": "sum", "Shares Held": "sum",
            "Identifier": lambda x: "; ".join(sorted(set(map(str, x)))[:8])[:300],
        }).reset_index().sort_values("Weight", ascending=False).reset_index(drop=True))


def sanity_check_weight_total(holdings, etf, low=70, high=130):
    total = holdings["Weight"].sum()
    if total < low or total > high:
        raise ValueError(f"{etf}: parsed weight total looks wrong: {total:.2f}%.")
    return total

# ============================================================
# PLAYWRIGHT / DYNAMIC PAGE HELPERS
# ============================================================

def ensure_playwright_available():
    try:
        import playwright  # noqa
        return
    except Exception:
        pass
    if not AUTO_INSTALL_PLAYWRIGHT_IF_MISSING:
        raise RuntimeError("Playwright is required. Run: pip install playwright && python -m playwright install chromium")
    print("Playwright not found. Installing playwright + chromium browser...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


def fetch_rendered_artifacts(url, click_texts=None, download_texts=None, wait_seconds=8):
    ensure_playwright_available()
    click_texts = click_texts or []
    download_texts = download_texts or []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        script_path, html_path, text_path, meta_path, download_path = td / "render_page.py", td / "page.html", td / "page.txt", td / "meta.json", td / "download.bin"
        script_code = r"""
import json, sys
from pathlib import Path
from playwright.sync_api import sync_playwright
url, html_path, text_path, meta_path, download_path = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5])
click_texts, download_texts, wait_seconds = json.loads(sys.argv[6]), json.loads(sys.argv[7]), float(sys.argv[8])
meta = {"downloaded": False, "download_name": None, "errors": []}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36", locale="en-US", viewport={"width":1500,"height":1200}, accept_downloads=True)
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    for txt in ["I agree", "Accept & Continue", "Accept and Continue", "Accept All", "Accept all", "Accept", "Agree", "Close important disclosure", "Close", "No Thanks"]:
        try:
            loc = page.get_by_text(txt, exact=False).first
            if loc.count() > 0:
                loc.click(timeout=2500); page.wait_for_timeout(700)
        except Exception: pass
    for _ in range(5):
        try:
            page.evaluate("window.scrollBy(0, Math.floor(document.body.scrollHeight / 5));"); page.wait_for_timeout(900)
        except Exception: pass
    for txt in click_texts:
        try:
            loc = page.get_by_text(txt, exact=False).first
            if loc.count() > 0:
                loc.click(timeout=3500); page.wait_for_timeout(1200)
        except Exception as e: meta["errors"].append(f"click {txt}: {e}")
    for _ in range(4):
        try:
            page.evaluate("window.scrollBy(0, Math.floor(document.body.scrollHeight / 4));"); page.wait_for_timeout(900)
        except Exception: pass
    for txt in download_texts:
        if meta["downloaded"]: break
        try:
            loc = page.get_by_text(txt, exact=False).first
            if loc.count() > 0:
                with page.expect_download(timeout=6000) as download_info:
                    loc.click(timeout=3500)
                dl = download_info.value; dl.save_as(str(download_path)); meta["downloaded"] = True; meta["download_name"] = dl.suggested_filename
        except Exception as e: meta["errors"].append(f"download {txt}: {e}")
    page.wait_for_timeout(int(wait_seconds * 1000))
    html_path.write_text(page.content(), encoding="utf-8")
    try: text_path.write_text(page.locator("body").inner_text(timeout=5000), encoding="utf-8")
    except Exception as e: meta["errors"].append(f"write text: {e}")
    browser.close()
meta_path.write_text(json.dumps(meta), encoding="utf-8")
"""
        script_path.write_text(script_code, encoding="utf-8")
        cmd = [sys.executable, str(script_path), url, str(html_path), str(text_path), str(meta_path), str(download_path), json.dumps(click_texts), json.dumps(download_texts), str(wait_seconds)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=PLAYWRIGHT_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            raise RuntimeError(f"Playwright render failed.\nSTDOUT:\n{proc.stdout[:2000]}\nSTDERR:\n{proc.stderr[:4000]}")
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        return {
            "html": html_path.read_text(encoding="utf-8") if html_path.exists() else "",
            "text": text_path.read_text(encoding="utf-8") if text_path.exists() else "",
            "download_bytes": download_path.read_bytes() if download_path.exists() and download_path.stat().st_size > 0 else None,
            "download_name": meta.get("download_name"),
            "errors": meta.get("errors", []),
        }


def try_tables_from_html_for_candidate(html, etf, validator):
    errors = []
    try:
        tables = pd.read_html(StringIO(html))
    except Exception as e:
        raise ValueError(f"No HTML tables found for {etf}: {e}")
    for df in tables:
        try:
            df = df.copy(); df.columns = make_unique_columns(df.columns)
            return validator(df, f"{etf} rendered HTML table")
        except Exception as e:
            errors.append(repr(e))
    raise ValueError(f"No valid {etf} holdings table from rendered HTML. " + " | ".join(errors[:8]))

# ============================================================
# INVESCO OFFICIAL PAGE CAPTURE FOR QQQ/SPMO
# ============================================================

def _inv_normalize_ticker(x):
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except Exception: pass
    s = str(x).strip().upper().replace(" EQUITY", "").replace(" COMMON STOCK", "").replace("/", ".")
    s = re.sub(r"\s+", "", s)
    if s in {"COMPANY", "ALLOCATION", "HIGH", "LOW", "HOLDINGS", "TOTAL", "WEIGHT", "DATE", "ASOF", "VIEW", "ALL", "REFRESH", "UNABLE", "LOAD", "DATA", "CASH", "USD", "NAV", "INDEX", "FUND"}:
        return None
    return s.replace("-", ".") if re.fullmatch(r"[A-Z]{1,6}(?:[.\-][A-Z])?", s) else None


def _inv_looks_like_company_name(x):
    if x is None:
        return False
    try:
        if pd.isna(x): return False
    except Exception: pass
    s = str(x).strip()
    if len(s) < 2 or _inv_normalize_ticker(s):
        return False
    low = s.lower()
    return not any(b in low for b in ["allocation", "holdings", "unable to load", "refresh", "view all", "as of", "company", "high", "low", "sort", "fund holdings", "market value", "shares", "ticker", "weight", "download"])


def _inv_dataframe_from_rows(rows, source):
    if not rows:
        return pd.DataFrame()
    raw = pd.DataFrame(rows)
    if raw.empty:
        return pd.DataFrame()
    raw.columns = make_unique_columns(raw.columns)
    ticker_col = name_col = weight_col = shares_col = None
    for c in raw.columns:
        lc = str(c).lower().strip()
        if ticker_col is None and any(x == lc or x in lc for x in ["ticker", "symbol", "trading symbol", "local ticker", "security ticker"]):
            if not any(b in lc for b in ["cusip", "isin", "sedol", "currency"]): ticker_col = c
        if name_col is None and any(x == lc or x in lc for x in ["company", "company name", "name", "holding", "holding name", "security", "security name", "description", "issuer"]):
            if "currency" not in lc: name_col = c
        if weight_col is None and any(x == lc or x in lc for x in ["weight", "allocation", "alloc", "percentage", "percent", "%", "% of fund", "% of assets", "% of net assets", "portfolio weight"]):
            if not any(b in lc for b in ["market value", "notional", "shares", "price", "nav"]): weight_col = c
        if shares_col is None and any(x in lc for x in ["shares", "quantity"]):
            shares_col = c
    if ticker_col is None:
        for c in raw.columns:
            sample = raw[c].dropna().astype(str).head(30).map(_inv_normalize_ticker)
            if len(sample) and sample.notna().mean() > 0.5:
                ticker_col = c; break
    if weight_col is None:
        best_col, best_score = None, 0
        for c in raw.columns:
            lc = str(c).lower().strip()
            if any(b in lc for b in ["market value", "notional", "shares", "price", "nav"]):
                continue
            vals = raw[c].dropna().astype(str).head(50)
            if len(vals) == 0: continue
            score = vals.str.contains("%", regex=False).mean() + vals.map(parse_num).notna().mean() * 0.5
            if score > best_score:
                best_col, best_score = c, score
        if best_score > 0.4:
            weight_col = best_col
    if ticker_col is None or weight_col is None:
        return pd.DataFrame()
    if name_col is None: name_col = ticker_col
    out = pd.DataFrame({
        "Ticker": raw[ticker_col].map(_inv_normalize_ticker),
        "Name": raw[name_col].astype(str).str.strip(),
        "Weight": raw[weight_col].map(parse_num),
        "Shares Held": raw[shares_col].map(parse_num) if shares_col else None,
        "Source": source,
    }).dropna(subset=["Ticker", "Weight"])
    if out.empty: return pd.DataFrame()
    if out["Weight"].abs().max() <= 1.5: out["Weight"] *= 100
    combined = out["Ticker"].astype(str) + " " + out["Name"].astype(str)
    out = out[~combined.str.lower().str.contains("cash|treasury|collateral|receivable|payable|total|disclaimer|unable to load|refresh|swap collateral|repo", regex=True, na=False)]
    if out.empty: return pd.DataFrame()
    return (out.groupby("Ticker", as_index=False)
        .agg({"Name": lambda x: next((v for v in x if _inv_looks_like_company_name(v)), str(x.iloc[0])), "Weight": "sum", "Shares Held": "sum", "Source": lambda x: "; ".join(sorted(set(map(str, x)))[:3])})
        .sort_values("Weight", ascending=False).reset_index(drop=True))


def _walk_json_lists(obj):
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            yield obj
        for x in obj:
            yield from _walk_json_lists(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_json_lists(v)


def _parse_json_bytes(content_bytes, source):
    out = []
    try:
        obj = json.loads(content_bytes.decode("utf-8-sig", errors="ignore"))
    except Exception:
        return out
    for rows in _walk_json_lists(obj):
        try:
            df = _inv_dataframe_from_rows(rows, source)
            if not df.empty: out.append(df)
        except Exception: pass
    return out


def _parse_file_bytes(content_bytes, content_type, source):
    out = []
    out.extend(_parse_json_bytes(content_bytes, source))
    try:
        text = content_bytes.decode("utf-8-sig", errors="ignore")
        for sep in [",", "\t", "|"]:
            for skip in range(0, 10):
                try:
                    raw = pd.read_csv(StringIO(text), sep=sep, skiprows=skip, dtype=str, low_memory=False)
                    if raw.shape[1] >= 2:
                        df = _inv_dataframe_from_rows(raw.to_dict("records"), f"{source}:csv")
                        if not df.empty: out.append(df)
                except Exception: pass
    except Exception: pass
    try:
        html = content_bytes.decode("utf-8", errors="ignore")
        for i, table in enumerate(pd.read_html(StringIO(html))):
            df = _inv_dataframe_from_rows(table.to_dict("records"), f"{source}:html_table_{i}")
            if not df.empty: out.append(df)
    except Exception: pass
    return out


def _choose_best_invesco(candidates, etf):
    min_rows = 90 if etf == "QQQ" else 80
    scored = []
    for df in candidates:
        if df is None or df.empty: continue
        df = df.copy().dropna(subset=["Ticker", "Weight"])
        if df.empty: continue
        total, n = df["Weight"].sum(), len(df)
        source_text = " ".join(df.get("Source", pd.Series(dtype=str)).astype(str).head(3).tolist()).lower()
        score = n + (200 if 90 <= total <= 110 else 0) + (100 if n >= min_rows else 0) + (80 if "dng-api.invesco.com" in source_text or "holdings/fund" in source_text else 0) - (100 if n <= 15 else 0)
        scored.append((score, df))
    if not scored: return pd.DataFrame()
    return sorted(scored, key=lambda x: x[0], reverse=True)[0][1].copy()


def pull_invesco_official_page_holdings(etf, headless=True):
    etf = etf.upper()
    ensure_playwright_available()
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    page_url = INVESCO_OFFICIAL_PAGE_URLS[etf]
    debug_dir = OUTPUT_DIR / f"{etf}_invesco_page_debug"
    responses_dir = debug_dir / "responses"
    debug_dir.mkdir(exist_ok=True, parents=True); responses_dir.mkdir(exist_ok=True, parents=True)
    captured, network_urls = [], []
    print(f"Opening official Invesco page for {etf}: {page_url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width":1440,"height":1200}, user_agent=HEADERS["User-Agent"], locale="en-US")
        page = context.new_page()
        def on_response(response):
            url = response.url; network_urls.append(url); low = url.lower(); ct = response.headers.get("content-type", "")
            if not any(k in low for k in ["holding", "holdings", "allocation", "fund", "etf", etf.lower(), "product", "portfolio", "component", "security", "shareclasses"]): return
            try:
                body = response.body()
                if body and len(body) > 20: captured.append({"url": url, "content_type": ct, "body": body})
            except Exception: pass
        page.on("response", on_response)
        page.goto(page_url, wait_until="domcontentloaded", timeout=90000); time.sleep(5)
        for txt in ["Accept All", "Accept all", "I Accept", "Accept", "Agree", "Continue", "Allow all", "Reject All", "Reject all"]:
            try:
                loc = page.get_by_text(txt, exact=False).first
                if loc.count() > 0: loc.click(timeout=1500); time.sleep(1); break
            except Exception: pass
        for _ in range(12):
            try: page.evaluate("window.scrollBy(0, 700)")
            except Exception: pass
            time.sleep(0.35)
        for txt in ["View all Holdings", "View all holdings", "See all holdings", "See all Holdings", "All QQQ holdings", "All holdings", "Holdings"]:
            try:
                loc = page.get_by_text(txt, exact=False).first
                if loc.count() > 0: loc.scroll_into_view_if_needed(timeout=3000); loc.click(timeout=3000); time.sleep(2)
            except Exception: pass
        for _ in range(10):
            try: page.evaluate("window.scrollBy(0, 700)")
            except Exception: pass
            time.sleep(0.35)
        try: page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError: pass
        html = page.content()
        try: text = page.locator("body").inner_text(timeout=15000)
        except Exception: text = ""
        try:
            (debug_dir / "rendered_page.html").write_text(html, encoding="utf-8")
            (debug_dir / "rendered_text.txt").write_text(text, encoding="utf-8")
            (debug_dir / "network_urls.txt").write_text("\n".join(network_urls), encoding="utf-8")
            page.screenshot(path=str(debug_dir / "screenshot.png"), full_page=True)
        except Exception: pass
        for i, item in enumerate(captured):
            try:
                suffix = Path(urlparse(item["url"]).path).suffix or ".bin"
                if len(suffix) > 8: suffix = ".bin"
                (responses_dir / f"response_{i:03d}{suffix}").write_bytes(item["body"])
            except Exception: pass
        browser.close()
    candidates = []
    for item in captured: candidates.extend(_parse_file_bytes(item["body"], item["content_type"], item["url"]))
    try:
        for i, table in enumerate(pd.read_html(StringIO(html))):
            df = _inv_dataframe_from_rows(table.to_dict("records"), f"rendered_html:{i}")
            if not df.empty: candidates.append(df)
    except Exception: pass
    best = _choose_best_invesco(candidates, etf)
    if best.empty:
        raise RuntimeError(f"Could not parse {etf} holdings from Invesco official page. Debug files saved in: {debug_dir.resolve()}")
    total, n = best["Weight"].sum(), len(best)
    print(f"{etf}: browser-captured Invesco holdings rows = {n}")
    print(f"{etf}: browser-captured total weight = {total:.2f}%")
    min_rows = 90 if etf == "QQQ" else 80
    if n < min_rows or not (85 <= total <= 110):
        raise ValueError(f"{etf} Invesco parse sanity check failed. Rows={n}, total={total:.2f}%. Debug: {debug_dir.resolve()}")
    best["Pulled At"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    best["Page URL"] = page_url
    keep = [c for c in ["Ticker", "Name", "Weight", "Shares Held", "Source", "Pulled At", "Page URL"] if c in best.columns]
    return best[keep].sort_values("Weight", ascending=False).reset_index(drop=True)


def pull_qqq_invesco_browser(): return pull_invesco_official_page_holdings("QQQ", headless=True)
def pull_spmo_invesco_browser(): return pull_invesco_official_page_holdings("SPMO", headless=True)

# ============================================================
# ETF PULLERS
# ============================================================

def extract_pdf_text_from_bytes(pdf_bytes):
    try:
        from pypdf import PdfReader
    except Exception:
        from PyPDF2 import PdfReader
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def standardize_mags_candidate(raw, source_name):
    test = normalize_holdings(raw, "MAGS")
    required = {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"}
    missing = required - set(test["Yahoo Ticker"].astype(str))
    if missing: raise ValueError(f"MAGS {source_name}: missing required tickers: {sorted(missing)}")
    total = test["Weight"].sum()
    if total < 95 or total > 105: raise ValueError(f"MAGS {source_name}: bad total {total:.2f}%")
    return raw


def parse_mags_top_holdings_from_text(text, source_name="Roundhill text"):
    if not text: raise ValueError("No text to parse for MAGS")
    clean = re.sub(r"\s+", " ", text); lower = clean.lower()
    starts = [m.start() for m in re.finditer(r"top holdings", lower)] or [0]
    ticker_to_name = {"AAPL":"Apple Inc", "MSFT":"Microsoft Corp", "GOOGL":"Alphabet Inc", "AMZN":"Amazon.com Inc", "META":"Meta Platforms Inc", "NVDA":"NVIDIA Corp", "TSLA":"Tesla Inc"}
    name_patterns = [("GOOGL", ["Alphabet Inc", "Alphabet"]), ("AMZN", ["Amazon.com Inc", "Amazon"]), ("AAPL", ["Apple Inc", "Apple"]), ("META", ["Meta Platforms Inc", "Meta"]), ("MSFT", ["Microsoft Corp", "Microsoft"]), ("NVDA", ["NVIDIA Corp", "NVIDIA", "NVidia"]), ("TSLA", ["Tesla Inc", "Tesla"])]
    best_rows = []
    for s in starts:
        ends = [lower.find(m, s+20) for m in ["performance", "distributions", "premium/discount", "fund exposures", "sector breakdown", "faq"]]
        ends = [e for e in ends if e > s]
        seg = clean[s:(min(ends) if ends else min(len(clean), s+2500))]
        rows, used = [], set()
        for ticker, name in ticker_to_name.items():
            m = re.search(rf"([A-Za-z0-9 .,&'\-/]{{2,80}}?)\s+{re.escape(ticker)}\s+(-?\d{{1,3}}(?:\.\d+)?)\s*%", seg, flags=re.I)
            if m: rows.append({"Ticker":ticker,"Name":name,"Weight":float(m.group(2))}); used.add(ticker)
        for ticker, names in name_patterns:
            if ticker in used: continue
            for name in names:
                m = re.search(rf"{re.escape(name)}\s+(?:{ticker}\s+)?(-?\d{{1,3}}(?:\.\d+)?)\s*%", seg, flags=re.I)
                if m: rows.append({"Ticker":ticker,"Name":name,"Weight":float(m.group(1))}); used.add(ticker); break
        if len(rows) > len(best_rows): best_rows = rows
    if len(best_rows) < 7: raise ValueError(f"MAGS parser found only {len(best_rows)} of 7 holdings")
    return standardize_mags_candidate(pd.DataFrame(best_rows).drop_duplicates(subset=["Ticker"]), source_name)


def pull_mags_roundhill_issuer_page():
    url = ETF_CONFIG["MAGS"]["url"]
    errors = []
    for label, func in [
        ("Roundhill requests text", lambda: parse_mags_top_holdings_from_text(BeautifulSoup(requests_get(url).text, "html.parser").get_text("\n", strip=True), "Roundhill requests text")),
        ("Roundhill rendered page", lambda: parse_mags_top_holdings_from_text(fetch_rendered_artifacts(url, click_texts=["Top Holdings"], download_texts=["Download CSV", "CSV"], wait_seconds=5)["text"], "Roundhill rendered page")),
        ("Roundhill factsheet", lambda: parse_mags_top_holdings_from_text(extract_pdf_text_from_bytes(requests_get(ETF_CONFIG["MAGS"]["factsheet_url"]).content), "Roundhill factsheet")),
    ]:
        try:
            raw = func(); print(f"MAGS holdings source used: {label}"); return raw
        except Exception as e:
            errors.append(f"{label}: {repr(e)}")
    raise ValueError("MAGS actual weights could not be parsed. No equal 100/7 fallback used. Attempts:\n - " + "\n - ".join(errors))


def standardize_smh_candidate(raw, source_name):
    test = normalize_holdings(raw, "SMH")
    if len(test) < 20: raise ValueError(f"SMH {source_name}: only {len(test)} rows")
    total = test["Weight"].sum()
    if total < 90 or total > 105: raise ValueError(f"SMH {source_name}: bad total {total:.2f}%")
    return raw


def parse_smh_from_download_bytes(content_bytes, source_name):
    errors = []
    for df in read_any_file_to_tables(content_bytes):
        try:
            df = df.copy(); df.columns = make_unique_columns(df.columns)
            return standardize_smh_candidate(df, source_name)
        except Exception as e: errors.append(repr(e))
    raise ValueError("Could not parse SMH download. " + " | ".join(errors[:8]))


def parse_smh_holdings_from_text(text, source_name="VanEck text"):
    if not text: raise ValueError("No text for SMH")
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
    joined = "\n".join(lines)
    rows = []
    for m in re.finditer(r"\b(?P<ticker>[A-Z0-9.\-]{1,10}(?:\s+[A-Z]{2})?)\s+(?P<name>[A-Z][A-Za-z0-9 &.,'\-/]{2,90}?)\s+(?P<weight>\d{1,2}(?:\.\d+)?)\s*%", joined):
        ticker, name, weight = m.group("ticker").strip(), m.group("name").strip(), float(m.group("weight"))
        if not looks_like_bad_row(f"{ticker} {name}") and weight > 0: rows.append({"Ticker":ticker,"Name":name,"Weight":weight})
    if not rows: raise ValueError("SMH text parser found no rows")
    return standardize_smh_candidate(pd.DataFrame(rows).drop_duplicates(subset=["Ticker", "Name", "Weight"]), source_name)


def pull_vaneck_smh_page():
    """Pull SMH holdings. Prefer the VanEck US direct XLSX endpoint because it is more stable than the page table."""
    urls = [ETF_CONFIG["SMH"]["url"]] + ETF_CONFIG["SMH"].get("backup_urls", [])
    errors = []

    for url in urls:
        try:
            r = requests_get(url)
            content_type = r.headers.get("content-type", "").lower()
            path = urlparse(url).path.lower()
            looks_like_download = (
                "spreadsheet" in content_type
                or "excel" in content_type
                or "application/octet-stream" in content_type
                or path.endswith((".xlsx", ".xls", ".csv"))
                or "/download/" in path
            )

            if looks_like_download and len(r.content) > 200:
                try:
                    return parse_smh_from_download_bytes(r.content, f"VanEck direct download: {url}")
                except Exception as e:
                    errors.append(f"direct download {url}: {repr(e)}")

            try:
                return try_tables_from_html_for_candidate(r.text, "SMH", standardize_smh_candidate)
            except Exception as e:
                errors.append(f"requests table {url}: {repr(e)}")

            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                combo = f"{a.get('href', '')} {a.get_text(' ', strip=True)}".lower()
                if any(x in combo for x in ["holdings", "download", "csv", "xlsx"]):
                    try:
                        rr = requests_get(urljoin(url, a.get("href", "")))
                        if len(rr.content) > 200:
                            return parse_smh_from_download_bytes(rr.content, "VanEck linked download")
                    except Exception as e:
                        errors.append(f"linked download: {repr(e)}")

            art = fetch_rendered_artifacts(
                url,
                click_texts=["Holdings", "View all", "View All", "Portfolio"],
                download_texts=["Download CSV", "Download XLSX", "Export", "Export data", "Download"],
                wait_seconds=6,
            )
            if art.get("download_bytes"):
                return parse_smh_from_download_bytes(art["download_bytes"], "VanEck rendered download")
            try:
                return try_tables_from_html_for_candidate(art.get("html", ""), "SMH", standardize_smh_candidate)
            except Exception as e:
                errors.append(f"rendered table: {repr(e)}")
            return parse_smh_holdings_from_text(art.get("text", ""), "VanEck rendered text")

        except Exception as e:
            errors.append(f"{url}: {repr(e)}")

    raise ValueError("SMH VanEck holdings could not be parsed. Attempts:\n - " + "\n - ".join(errors))

def pull_evolve_tech_csv():
    last_error = None
    for url in ETF_CONFIG["TECH.TO"]["csv_urls"]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=45)
            if r.status_code == 200 and len(r.content) > 50:
                dfs = read_any_file_to_tables(r.content)
                if dfs: return choose_best_holdings_table(dfs)
            last_error = f"{r.status_code} for {url}"
        except Exception as e: last_error = str(e)
    raise ValueError(f"TECH.TO Evolve CSV failed. Last error: {last_error}")


def pull_globalx_chps_page():
    r = requests_get(ETF_CONFIG["CHPS.TO"]["url"])
    try:
        tables = pd.read_html(StringIO(r.text))
        if tables:
            raw = choose_best_holdings_table(tables)
            if len(normalize_holdings(raw, "CHPS.TO")) >= 10: return raw
    except Exception: pass
    soup = BeautifulSoup(r.text, "html.parser")
    lines = [x.strip() for x in soup.get_text("\n", strip=True).splitlines() if x.strip()]
    start = next((i for i, line in enumerate(lines) if line.lower() == "top holdings"), None)
    if start is None: raise ValueError("Could not find CHPS.TO holdings")
    rows, i = [], start
    while i < len(lines) and lines[i].lower() != "security name": i += 1
    i += 2
    while i + 1 < len(lines):
        name, weight = lines[i], parse_num(lines[i+1])
        if "holdings are subject" in name.lower(): break
        if weight is not None: rows.append({"Ticker": map_name_to_yahoo(name), "Name": name, "Weight": weight}); i += 2
        else: i += 1
    if not rows: raise ValueError("Could not parse CHPS.TO holdings")
    return pd.DataFrame(rows)


def pull_blackrock_soxx():
    """
    Pull SOXX holdings from BlackRock/iShares.

    Fix:
      BlackRock's current holdings download URL uses:
        /us/products/239705/fund/1467271812596.ajax?...
      The older URL without /fund/ can return no usable holdings, which caused SOXX
      to fail and then disappear from the final emailed workbook.
    """
    product_id = ETF_CONFIG["SOXX"]["product_id"]
    file_name = ETF_CONFIG["SOXX"]["file_name"]

    product_page = f"https://www.ishares.com/us/products/{product_id}/ishares-phlx-semiconductor-etf"

    soxx_headers = dict(HEADERS)
    soxx_headers.update({
        "Accept": "text/csv,application/csv,application/vnd.ms-excel,application/octet-stream,*/*",
        "Referer": product_page,
    })

    # Put the current official BlackRock download endpoint first.
    # Keep older endpoint shapes as fallbacks in case BlackRock changes routing again.
    urls = [
        f"https://www.ishares.com/us/products/{product_id}/fund/1467271812596.ajax?dataType=fund&fileName={file_name}&fileType=csv",
        f"https://www.ishares.com/us/products/{product_id}/ishares-phlx-semiconductor-etf/fund/1467271812596.ajax?dataType=fund&fileName={file_name}&fileType=csv",
        f"https://www.ishares.com/us/products/{product_id}/ishares-phlx-semiconductor-etf/1467271812596.ajax?dataType=fund&fileName={file_name}&fileType=csv",
        f"https://www.ishares.com/us/products/{product_id}/1467271812596.ajax?dataType=fund&fileName={file_name}&fileType=csv",
    ]

    errors = []

    for url in urls:
        try:
            r = requests.get(url, headers=soxx_headers, timeout=60)

            if r.status_code != 200 or len(r.content) < 200:
                errors.append(f"{url}: status={r.status_code}, bytes={len(r.content)}")
                continue

            dfs = read_any_file_to_tables(r.content)
            if not dfs:
                preview = r.text[:300].replace("\n", " ")
                errors.append(f"{url}: no parseable tables. Preview={preview!r}")
                continue

            table = choose_best_holdings_table(dfs)

            # Validate before returning so a disclaimer/summary table cannot pass through.
            test = normalize_holdings(table, "SOXX")
            n = len(test)
            total = test["Weight"].sum()

            # SOXX is currently a concentrated semiconductor ETF with about 30 holdings.
            # Use a tolerant range because cash/derivative rows can be excluded.
            if n < 20 or not (85 <= total <= 110):
                errors.append(f"{url}: parsed table failed sanity check. rows={n}, total_weight={total:.2f}%")
                continue

            print(f"SOXX holdings source used: {url}")
            print(f"SOXX validation rows={n}, total_weight={total:.2f}%")
            return table

        except Exception as e:
            errors.append(f"{url}: {repr(e)}")

    # Last resort: render the official page and try to parse/download via Playwright.
    try:
        art = fetch_rendered_artifacts(
            product_page,
            click_texts=["Holdings", "All", "Detailed Holdings and Analytics"],
            download_texts=["Detailed Holdings and Analytics", "Data Download", "Download"],
            wait_seconds=8,
        )

        if art.get("download_bytes"):
            dfs = read_any_file_to_tables(art["download_bytes"])
            table = choose_best_holdings_table(dfs)
            test = normalize_holdings(table, "SOXX")
            n = len(test)
            total = test["Weight"].sum()
            if n >= 20 and 85 <= total <= 110:
                print("SOXX holdings source used: rendered iShares download")
                print(f"SOXX validation rows={n}, total_weight={total:.2f}%")
                return table
            errors.append(f"rendered download sanity failed: rows={n}, total_weight={total:.2f}%")

        try:
            table = try_tables_from_html_for_candidate(
                art.get("html", ""),
                "SOXX",
                lambda raw, source_name: raw,
            )
            test = normalize_holdings(table, "SOXX")
            n = len(test)
            total = test["Weight"].sum()
            if n >= 20 and 85 <= total <= 110:
                print("SOXX holdings source used: rendered iShares HTML")
                print(f"SOXX validation rows={n}, total_weight={total:.2f}%")
                return table
            errors.append(f"rendered HTML sanity failed: rows={n}, total_weight={total:.2f}%")
        except Exception as e:
            errors.append(f"rendered HTML parse failed: {repr(e)}")

    except Exception as e:
        errors.append(f"rendered page fallback failed: {repr(e)}")

    raise ValueError("SOXX BlackRock holdings could not be parsed. Attempts:\n - " + "\n - ".join(errors))



def pull_ssga_xlk():
    return choose_best_holdings_table(read_any_file_to_tables(requests_get(ETF_CONFIG["XLK"]["url"]).content))


def pull_issuer_holdings(etf):
    etf = etf.upper()
    if etf == "TECH": etf = "TECH.TO"
    if etf == "CHPS": etf = "CHPS.TO"
    print(f"\nPulling {etf} issuer holdings...")
    print(f"Issuer: {ETF_CONFIG[etf]['issuer']}")
    if etf == "SPMO": raw = pull_spmo_invesco_browser()
    elif etf == "QQQ": raw = pull_qqq_invesco_browser()
    elif etf == "MAGS": raw = pull_mags_roundhill_issuer_page()
    elif etf == "TECH.TO": raw = pull_evolve_tech_csv()
    elif etf == "CHPS.TO": raw = pull_globalx_chps_page()
    elif etf == "SOXX": raw = pull_blackrock_soxx()
    elif etf == "SMH": raw = pull_vaneck_smh_page()
    elif etf == "XLK": raw = pull_ssga_xlk()
    else: raise ValueError(f"No ETF config found for {etf}")
    holdings = normalize_holdings(raw, etf)
    if etf == "MAGS": sanity_check_weight_total(holdings, etf, low=90, high=120)
    elif etf == "SMH": sanity_check_weight_total(holdings, etf, low=90, high=105)
    elif etf == "SOXX": sanity_check_weight_total(holdings, etf, low=85, high=110)
    elif etf in ["SPMO", "QQQ"]: sanity_check_weight_total(holdings, etf, low=85, high=110)
    holdings["Source Note"] = f"Issuer-first: {ETF_CONFIG[etf]['issuer']}"
    print(f"{etf}: normalized holdings = {len(holdings)}")
    print(f"{etf}: total normalized weight = {holdings['Weight'].sum():.2f}%")
    return holdings

# ============================================================

# YAHOO DATA
# ============================================================

NUMERIC_YAHOO_COLUMNS = [
    "Current Price", "Target Low", "Target Mean", "Target High", "Target Median", "Analyst Count",
    "Trailing PE", "Forward PE", "YTD Return",
    "EPS Last Year", "EPS This Year Est Avg", "EPS Next Year Est Avg",
    "Growth Last Year", "Growth This Year Est", "Growth Next Year Est",
]


def get_ytd_return_from_yahoo(symbol):
    try:
        today = pd.Timestamp.now(tz="America/Toronto")
        hist = yf.Ticker(symbol).history(
            start=f"{today.year}-01-01",
            end=(today + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            auto_adjust=True,
            actions=False,
        )
        close = _clean_close_series(hist)
        if len(close) < 2:
            return None
        first_close = safe_float_value(close.iloc[0])
        last_close = safe_float_value(close.iloc[-1])
        if first_close in [None, 0] or last_close is None:
            return None
        return float(last_close / first_close - 1)
    except Exception:
        return None


def _yahoo_ticker_variants(symbol):
    s = str(symbol).strip()
    variants = [s]

    # Yahoo usually uses BRK-B / BF-B, while some issuer files show BRK.B / BF.B.
    # Do not rewrite exchange suffixes like .TO, .HK, .T, .KS.
    if re.fullmatch(r"[A-Z]{1,6}\.[A-Z]", s.upper()):
        variants.append(s.replace(".", "-"))

    return list(dict.fromkeys(variants))


def _clean_close_series(hist):
    """Return a clean adjusted close series from yfinance output."""
    try:
        if hist is None or hist.empty:
            return pd.Series(dtype=float)

        if isinstance(hist.columns, pd.MultiIndex):
            adj_cols = [c for c in hist.columns if str(c[0]).lower() == "adj close"]
            close_cols = [c for c in hist.columns if str(c[0]).lower() == "close"]
            chosen = adj_cols[0] if adj_cols else (close_cols[0] if close_cols else None)
            if chosen is None:
                return pd.Series(dtype=float)
            close = hist[chosen]
        else:
            if "Adj Close" in hist.columns:
                close = hist["Adj Close"]
            elif "Close" in hist.columns:
                close = hist["Close"]
            else:
                return pd.Series(dtype=float)

        close = pd.to_numeric(close, errors="coerce").dropna()
        if close.empty:
            return pd.Series(dtype=float)

        idx = pd.to_datetime(close.index, errors="coerce")
        try:
            idx = idx.tz_localize(None)
        except Exception:
            try:
                idx = idx.tz_convert(None)
            except Exception:
                pass

        close.index = idx
        close = close[~close.index.isna()].sort_index()
        return close
    except Exception:
        return pd.Series(dtype=float)


def _download_adjusted_close(symbol, start, end):
    """Try several yfinance paths and return split/dividend-adjusted close prices."""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    for sym in _yahoo_ticker_variants(symbol):
        for method in ["ticker_history", "download"]:
            try:
                if method == "ticker_history":
                    hist = yf.Ticker(sym).history(
                        start=start_ts.strftime("%Y-%m-%d"),
                        end=end_ts.strftime("%Y-%m-%d"),
                        auto_adjust=True,
                        actions=False,
                    )
                else:
                    hist = yf.download(
                        sym,
                        start=start_ts.strftime("%Y-%m-%d"),
                        end=end_ts.strftime("%Y-%m-%d"),
                        auto_adjust=True,
                        progress=False,
                        threads=False,
                    )
                close = _clean_close_series(hist)
                if len(close) >= 2:
                    return close
            except Exception:
                pass

        # Period fallback, then filter ourselves.
        for period in ["3y", "5y", "2y"]:
            for method in ["ticker_history", "download"]:
                try:
                    if method == "ticker_history":
                        hist = yf.Ticker(sym).history(period=period, auto_adjust=True, actions=False)
                    else:
                        hist = yf.download(sym, period=period, auto_adjust=True, progress=False, threads=False)
                    close = _clean_close_series(hist)
                    close = close[(close.index >= start_ts) & (close.index < end_ts)]
                    if len(close) >= 2:
                        return close
                except Exception:
                    pass

    return pd.Series(dtype=float)


def get_last_calendar_year_stock_return_from_yahoo(symbol):
    """
    Growth Last Year = previous full calendar-year adjusted stock return.
    Example: if run in 2026, this calculates 2025 return.
    Falls back to trailing 1-year adjusted return if needed.
    """
    try:
        today = pd.Timestamp.now(tz="America/Toronto").tz_localize(None)
    except Exception:
        today = pd.Timestamp.today()

    last_year = today.year - 1
    start = pd.Timestamp(year=last_year, month=1, day=1)
    end = pd.Timestamp(year=today.year, month=1, day=1)

    try:
        close = _download_adjusted_close(symbol, start, end)
        if len(close) >= 2:
            first_price = safe_float_value(close.iloc[0])
            last_price = safe_float_value(close.iloc[-1])
            if first_price not in [None, 0] and last_price is not None:
                return float(last_price / first_price - 1)
    except Exception:
        pass

    try:
        end2 = today + pd.Timedelta(days=1)
        start2 = today - pd.DateOffset(years=1)
        close = _download_adjusted_close(symbol, start2, end2)
        if len(close) >= 2:
            first_price = safe_float_value(close.iloc[0])
            last_price = safe_float_value(close.iloc[-1])
            if first_price not in [None, 0] and last_price is not None:
                return float(last_price / first_price - 1)
    except Exception:
        pass

    return None


def get_yf_dataframe(ticker_obj, names):
    for name in names:
        try:
            obj = getattr(ticker_obj, name, None)
            if obj is None:
                continue
            df = obj() if callable(obj) else obj
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df.copy()
        except Exception:
            pass
    return pd.DataFrame()


def norm_label(x):
    return re.sub(r"[^a-z0-9+\-]", "", str(x).lower())


def find_estimate_row(df, row_patterns):
    if df is None or df.empty:
        return None
    pats = [norm_label(x) for x in row_patterns]
    for idx in df.index:
        ni = norm_label(idx)
        for pat in pats:
            if ni == pat or pat in ni:
                return df.loc[idx]
    return None


def row_average_value(row):
    if row is None:
        return None
    if isinstance(row, pd.DataFrame):
        if row.empty:
            return None
        row = row.iloc[0]

    preferred = ["avg", "average", "avgestimate", "avg estimate", "avg.", "mean"]
    for col in row.index:
        nc = norm_label(col)
        if any(norm_label(p) in nc for p in preferred):
            v = safe_float_value(row[col])
            if v is not None:
                return v

    for col in row.index:
        v = safe_float_value(row[col])
        if v is not None:
            return v
    return None


def get_annual_eps_from_financials(ticker_obj):
    candidates = []
    for attr in ["income_stmt", "financials"]:
        try:
            candidates.append(getattr(ticker_obj, attr))
        except Exception:
            pass

    for func in ["get_income_stmt", "get_financials"]:
        try:
            f = getattr(ticker_obj, func)
            try:
                candidates.append(f(freq="yearly"))
            except TypeError:
                candidates.append(f())
        except Exception:
            pass

    for df in candidates:
        try:
            if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                continue

            row_key = None
            norm_index = {norm_label(i): i for i in df.index}
            for rn in ["Diluted EPS", "Basic EPS", "Normalized Diluted EPS", "DilutedEPS", "BasicEPS"]:
                nrn = norm_label(rn)
                for ni, original_idx in norm_index.items():
                    if nrn == ni or nrn in ni:
                        row_key = original_idx
                        break
                if row_key is not None:
                    break

            if row_key is None:
                continue

            row = pd.to_numeric(df.loc[row_key], errors="coerce").dropna()
            if row.empty:
                continue

            try:
                dates = pd.to_datetime(row.index, errors="coerce")
                if dates.notna().sum() >= 2:
                    row = row.iloc[np.argsort(dates.values)[::-1]]
            except Exception:
                pass

            return safe_float_value(row.iloc[0])
        except Exception:
            pass

    return None


def get_eps_estimates_from_yfinance(ticker_obj):
    df = get_yf_dataframe(ticker_obj, ["get_earnings_estimate", "earnings_estimate"])
    eps_this_year = None
    eps_next_year = None

    if not df.empty:
        eps_this_year = row_average_value(find_estimate_row(df, ["0y", "current year", "currentyear", "current fiscal year"]))
        eps_next_year = row_average_value(find_estimate_row(df, ["+1y", "next year", "nextyear", "next fiscal year"]))

    try:
        info = ticker_obj.get_info()
    except Exception:
        try:
            info = ticker_obj.info
        except Exception:
            info = {}

    if isinstance(info, dict):
        if eps_this_year is None:
            for k in ["epsCurrentYear", "currentYearEps", "earningsEstimateCurrentYear"]:
                eps_this_year = safe_float_value(info.get(k))
                if eps_this_year is not None:
                    break
        if eps_next_year is None:
            for k in ["epsNextYear", "nextYearEps", "earningsEstimateNextYear"]:
                eps_next_year = safe_float_value(info.get(k))
                if eps_next_year is not None:
                    break

    return eps_this_year, eps_next_year


def get_growth_estimates_from_yfinance(ticker_obj):
    """Yahoo Finance Analysis -> Growth Estimates. Only this/next year are used."""
    df = get_yf_dataframe(ticker_obj, ["get_growth_estimates", "growth_estimates"])
    if df.empty:
        return None, None, None

    def pick_row(patterns):
        return find_estimate_row(df, patterns)

    def pick_value(row):
        if row is None:
            return None
        if isinstance(row, pd.DataFrame):
            if row.empty:
                return None
            row = row.iloc[0]
        for col in row.index:
            if any(norm_label(p) in norm_label(col) for p in ["stock trend", "stocktrend", "stock", "estimate", "growth"]):
                v = normalize_growth_rate(row[col])
                if v is not None:
                    return v
        for col in row.index:
            v = normalize_growth_rate(row[col])
            if v is not None:
                return v
        return None

    last_row = pick_row(["-1y", "last year", "lastyear", "past year", "pastyear"])
    if last_row is None:
        last_row = pick_row(["-5y", "past 5 years", "past5years", "past 5 years per annum"])
    this_row = pick_row(["0y", "current year", "currentyear", "current fiscal year"])
    next_row = pick_row(["+1y", "next year", "nextyear", "next fiscal year"])

    return pick_value(last_row), pick_value(this_row), pick_value(next_row)


def get_eps_and_growth_data(ticker_obj):
    eps_last_year = get_annual_eps_from_financials(ticker_obj)
    eps_this_year, eps_next_year = get_eps_estimates_from_yfinance(ticker_obj)
    _ignore_last, growth_this_year, growth_next_year = get_growth_estimates_from_yfinance(ticker_obj)

    return {
        "EPS Last Year": eps_last_year,
        "EPS This Year Est Avg": eps_this_year,
        "EPS Next Year Est Avg": eps_next_year,
        "Growth This Year Est": growth_this_year,
        "Growth Next Year Est": growth_next_year,
    }


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
        "YTD Return": None,
        "EPS Last Year": None,
        "EPS This Year Est Avg": None,
        "EPS Next Year Est Avg": None,
        "Growth Last Year": None,
        "Growth This Year Est": None,
        "Growth Next Year Est": None,
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
            out["Current Price"] = fast.get("last_price") if hasattr(fast, "get") else getattr(fast, "last_price", None)
        except Exception:
            pass

        if out["Current Price"] is None or pd.isna(out["Current Price"]):
            try:
                hist = t.history(period="5d", auto_adjust=False)
                if hist is not None and not hist.empty:
                    out["Current Price"] = hist["Close"].dropna().iloc[-1]
            except Exception:
                pass

        try:
            info = t.get_info()
        except Exception:
            try:
                info = t.info
            except Exception:
                info = {}

        if isinstance(info, dict):
            out["Trailing PE"] = safe_float_value(info.get("trailingPE", info.get("trailingPe")))
            out["Forward PE"] = safe_float_value(info.get("forwardPE", info.get("forwardPe")))

        out["YTD Return"] = get_ytd_return_from_yahoo(symbol)

        try:
            out.update(get_eps_and_growth_data(t))
        except Exception as e:
            out["Yahoo Error"] = (out["Yahoo Error"] + " | " if out["Yahoo Error"] else "") + f"EPS/growth error: {e}"

        # Hard assignment after all other updates so it cannot be overwritten by Yahoo Growth Estimates.
        out["Growth Last Year"] = get_last_calendar_year_stock_return_from_yahoo(symbol)
        if out["Growth Last Year"] is None:
            out["Yahoo Error"] = (out["Yahoo Error"] + " | " if out["Yahoo Error"] else "") + "Growth Last Year unavailable from adjusted price history"

    except Exception as e:
        out["Yahoo Error"] = str(e)

    return out


def add_yahoo_targets(holdings):
    rows = []
    symbols = sorted(holdings["Yahoo Ticker"].dropna().astype(str).unique())

    for i, symbol in enumerate(symbols, 1):
        print(f"Yahoo targets + PE + YTD + EPS + Growth {i}/{len(symbols)}: {symbol}")
        rows.append(pull_yahoo_targets(symbol))
        time.sleep(YAHOO_SLEEP_SECONDS)

    targets = pd.DataFrame(rows)
    merged = holdings.merge(targets, on="Yahoo Ticker", how="left")

    for c in NUMERIC_YAHOO_COLUMNS:
        if c in merged.columns:
            merged[c] = pd.to_numeric(merged[c], errors="coerce")

    if "Growth Last Year" in merged.columns:
        missing = merged["Growth Last Year"].isna()
        if missing.any():
            for sym in merged.loc[missing, "Yahoo Ticker"].dropna().astype(str).unique():
                val = get_last_calendar_year_stock_return_from_yahoo(sym)
                if val is not None:
                    merged.loc[merged["Yahoo Ticker"].astype(str) == sym, "Growth Last Year"] = val

    return merged

# CALCULATIONS
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


def weighted_harmonic_from_column(df, pe_col):
    if pe_col not in df.columns: return None, 0.0
    x = df.dropna(subset=[pe_col, "Weight Decimal"]).copy()
    x = x[(x[pe_col] > 0) & (x["Weight Decimal"] > 0)]
    if x.empty: return None, 0.0
    covered_weight = x["Weight Decimal"].sum()
    denom = (x["Weight Decimal"] / x[pe_col]).sum()
    return (None, covered_weight) if denom <= 0 else (covered_weight / denom, covered_weight)


def weighted_harmonic_pe(df):
    x = df.copy()
    if "Trailing PE" not in x.columns: x["Trailing PE"] = np.nan
    if "Forward PE" not in x.columns: x["Forward PE"] = np.nan
    x["PE Used"] = x["Trailing PE"]
    x.loc[x["PE Used"].isna(), "PE Used"] = x.loc[x["PE Used"].isna(), "Forward PE"]
    return weighted_harmonic_from_column(x, "PE Used")


def weighted_forward_pe(df): return weighted_harmonic_from_column(df, "Forward PE")


def get_etf_current_price(etf):
    try:
        t = yf.Ticker(etf); fast = t.fast_info
        price = fast.get("last_price") if hasattr(fast, "get") else getattr(fast, "last_price", None)
        if price is None or pd.isna(price):
            hist = t.history(period="5d", auto_adjust=False)
            if hist is not None and not hist.empty: price = hist["Close"].dropna().iloc[-1]
        return float(price) if price is not None and not pd.isna(price) else None
    except Exception: return None


def summarize_etf(df, etf):
    covered = df.dropna(subset=["Current Price", "Target Low", "Target Mean", "Target High"]).copy()
    pe_ratio, pe_cov = weighted_harmonic_pe(df)
    fpe, fpe_cov = weighted_forward_pe(df)
    return {"ETF": etf, "Total Holdings Used": len(df), "Covered Holdings": len(covered), "Total Weight Used": df["Weight Decimal"].sum(), "Covered Weight": covered["Weight Decimal"].sum(), "ETF YTD Return": get_ytd_return_from_yahoo(etf), "Raw Worst Case Return": covered["Weighted Low Return"].sum(), "Raw Mean Case Return": covered["Weighted Mean Return"].sum(), "Raw Best Case Return": covered["Weighted High Return"].sum(), "Raw Median Case Return": covered["Weighted Median Return"].sum(), "PE Ratio": pe_ratio, "PE Coverage Weight": pe_cov, "Forward PE": fpe, "Forward PE Coverage Weight": fpe_cov}


def print_summary(s):
    print("\n" + "=" * 72); print(f"{s['ETF']} Analyst Target Implied 1-Year Performance"); print("=" * 72)
    print(f"Total holdings used: {s['Total Holdings Used']}")
    print(f"Covered holdings:    {s['Covered Holdings']}")
    print(f"Total weight used:   {s['Total Weight Used']:.2%}")
    print(f"Covered weight:      {s['Covered Weight']:.2%}")
    if s.get("ETF YTD Return") is not None and pd.notna(s.get("ETF YTD Return")): print(f"ETF YTD return:      {s['ETF YTD Return']:.2%}")
    print(f"Weighted PE ratio:   {s['PE Ratio']:.2f}x, coverage {s['PE Coverage Weight']:.2%}" if s.get("PE Ratio") is not None and pd.notna(s.get("PE Ratio")) else "Weighted PE ratio:   unavailable")
    print(f"Weighted forward PE: {s['Forward PE']:.2f}x, coverage {s['Forward PE Coverage Weight']:.2%}" if s.get("Forward PE") is not None and pd.notna(s.get("Forward PE")) else "Weighted forward PE: unavailable")
    print("\nRaw ETF impact, uncovered holdings treated as 0 contribution:")
    print(f"Worst case:  {s['Raw Worst Case Return']:.2%}"); print(f"Mean case:   {s['Raw Mean Case Return']:.2%}"); print(f"Best case:   {s['Raw Best Case Return']:.2%}"); print(f"Median case: {s['Raw Median Case Return']:.2%}")

# ============================================================
# FEAR/GREED + VIX
# ============================================================

def build_vix_summary(periods=(20, 10, 5, 3)):
    try:
        vix = yf.download("^VIX", period="25y", auto_adjust=False, progress=False)
        if vix is None or vix.empty: raise ValueError("No VIX data returned")
        if isinstance(vix.columns, pd.MultiIndex):
            close_cols = [c for c in vix.columns if c[0] == "Close"]
            vix = vix[close_cols[0]].to_frame("VIX")
        else:
            vix = vix[["Close"]].rename(columns={"Close": "VIX"})
        vix = vix.dropna(); vix.index = pd.to_datetime(vix.index)
        rows = []
        for years in periods:
            end = vix.index.max(); start = end - pd.DateOffset(years=years); subset = vix[vix.index >= start]["VIX"].dropna(); latest = subset.iloc[-1]
            rows.append({"Period": f"Last {years} Years", "Start Date": subset.index.min().date(), "End Date": subset.index.max().date(), "Trading Days": len(subset), "Median": subset.median(), "30th Percentile": np.percentile(subset, 30), "70th Percentile": np.percentile(subset, 70), "Mean": subset.mean(), "Min": subset.min(), "Max": subset.max(), "Latest VIX": latest, "Percentile Rank": (subset < latest).mean() * 100})
        summary = pd.DataFrame(rows)
        for c in ["Median", "30th Percentile", "70th Percentile", "Mean", "Min", "Max", "Latest VIX", "Percentile Rank"]: summary[c] = summary[c].round(2)
        return summary, None
    except Exception as e: return pd.DataFrame(), repr(e)


def fear_greed_rating_from_score(score):
    s = safe_float_value(score)
    if s is None: return None
    if s <= 24: return "Extreme Fear"
    if s <= 44: return "Fear"
    if s <= 55: return "Neutral"
    if s <= 75: return "Greed"
    return "Extreme Greed"


def parse_cnn_timestamp(ts):
    try:
        v = safe_float_value(ts)
        if v is None: return None
        dt = pd.to_datetime(v, unit="ms" if v > 10_000_000_000 else "s", utc=True)
        return dt.tz_convert("America/Toronto").strftime("%Y-%m-%d %H:%M")
    except Exception: return None


def build_fear_greed_summary():
    urls = ["https://production.dataviz.cnn.io/index/fearandgreed/graphdata", "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/2020-01-01"]
    headers = dict(HEADERS); headers["Referer"] = "https://www.cnn.com/markets/fear-and-greed"
    last_error = None
    for url in urls:
        try:
            data = requests.get(url, headers=headers, timeout=30).json()
            fg = data.get("fear_and_greed") or data.get("fearAndGreed") or data.get("fear_and_greed_now") or {}
            if not isinstance(fg, dict): continue
            score = safe_float_value(fg.get("score") or fg.get("value") or fg.get("current_score") or fg.get("fearGreedScore"))
            if score is None: continue
            rating = fg.get("rating") or fg.get("classification") or fg.get("status") or fear_greed_rating_from_score(score)
            timestamp = fg.get("timestamp") or fg.get("lastUpdated") or fg.get("asOf") or data.get("timestamp")
            as_of = parse_cnn_timestamp(timestamp) or pd.Timestamp.now(tz="America/Toronto").strftime("%Y-%m-%d %H:%M")
            return pd.DataFrame([{"Metric": "CNN Fear & Greed Index", "Score": score, "Rating": rating, "As of": as_of, "Source": "CNN"}]), None
        except Exception as e: last_error = repr(e)
    return pd.DataFrame(), last_error or "No usable CNN Fear & Greed data returned."

# ============================================================

# ============================================================
# EXCEL EXPORT + PE HISTORY
# ============================================================

REPORT_VERSION = "pe-history-graphs-layout-v10-redo-overlap-from-etf-tabs"
PE_HISTORY_PATH = OUTPUT_DIR / "ETF_PE_history.xlsx"
REPORT_PATH = OUTPUT_DIR / "ETF_analyst_report.xlsx"


def get_report_years():
    today = pd.Timestamp.now(tz="America/Toronto")
    current_year = int(today.year)
    return current_year - 1, current_year, current_year + 1


def update_pe_history(summaries, history_path=None):
    history_path = Path(history_path) if history_path else PE_HISTORY_PATH
    history_path.parent.mkdir(parents=True, exist_ok=True)
    today = pd.Timestamp.now(tz="America/Toronto").date()

    new_df = pd.DataFrame([
        {
            "Date": today,
            "ETF": s.get("ETF"),
            "PE Ratio": s.get("PE Ratio"),
            "Forward PE": s.get("Forward PE"),
            "PE coverage": s.get("PE Coverage Weight"),
            "Forward PE coverage": s.get("Forward PE Coverage Weight"),
            "YTD": s.get("ETF YTD Return"),
            "Covered Weight": s.get("Covered Weight"),
        }
        for s in summaries
    ])

    if history_path.exists():
        try:
            old_df = pd.read_excel(history_path)
            old_df["Date"] = pd.to_datetime(old_df["Date"], errors="coerce").dt.date
            combined = pd.concat([old_df, new_df], ignore_index=True)
        except Exception as e:
            print(f"Could not read existing PE history, rebuilding it. Error: {e}")
            combined = new_df
    else:
        combined = new_df

    combined = combined.dropna(subset=["Date", "ETF"], how="any")
    combined = combined.drop_duplicates(subset=["Date", "ETF"], keep="last")
    combined = combined.sort_values(["ETF", "Date"]).reset_index(drop=True)
    combined.to_excel(history_path, index=False)

    print(f"Saved PE history: {history_path}")
    return combined


def load_pe_history(history_path=None):
    history_path = Path(history_path) if history_path else PE_HISTORY_PATH
    if not history_path.exists():
        return pd.DataFrame(columns=["Date", "ETF", "PE Ratio", "Forward PE"])

    try:
        hist = pd.read_excel(history_path)
        hist["Date"] = pd.to_datetime(hist["Date"], errors="coerce")
        hist = hist.dropna(subset=["Date", "ETF"])
        return hist.sort_values(["ETF", "Date"])
    except Exception as e:
        print(f"Could not load PE history: {e}")
        return pd.DataFrame(columns=["Date", "ETF", "PE Ratio", "Forward PE"])


def build_excel_summary_rows(summary_df):
    rows = []
    for _, r in summary_df.iterrows():
        etf = r["ETF"]
        current_price = get_etf_current_price(etf)
        worst_return = r.get("Raw Worst Case Return")

        rows.append({
            "ETF": etf,
            "reliable": r.get("Covered Weight"),
            "YTD": r.get("ETF YTD Return"),
            "Average": r.get("Raw Mean Case Return"),
            "Median": r.get("Raw Median Case Return"),
            "WORST": r.get("Raw Worst Case Return"),
            "BEST": r.get("Raw Best Case Return"),
            "Current pr": current_price,
            "worst price": current_price * (1 + worst_return)
            if current_price is not None and pd.notna(worst_return)
            else None,
            "PE Ratio": r.get("PE Ratio"),
            "PE coverage": r.get("PE Coverage Weight"),
            "Forward PE": r.get("Forward PE"),
            "Forward PE coverage": r.get("Forward PE Coverage Weight"),
        })

    return pd.DataFrame(rows)


def prepare_detail_sheet(details):
    df = details.copy()

    if "Growth Last Year" not in df.columns:
        df["Growth Last Year"] = np.nan

    if "Yahoo Ticker" in df.columns:
        missing = df["Growth Last Year"].isna()
        if missing.any():
            for sym in df.loc[missing, "Yahoo Ticker"].dropna().astype(str).unique():
                val = get_last_calendar_year_stock_return_from_yahoo(sym)
                if val is not None:
                    df.loc[df["Yahoo Ticker"].astype(str) == sym, "Growth Last Year"] = val

    out = pd.DataFrame()
    out["Ticker"] = df["Yahoo Ticker"]
    out["% of Portfolio"] = df["Weight Decimal"]
    out["YTD"] = df["YTD Return"]
    out["Worst"] = df["Low Return"]
    out["Average"] = df["Mean Return"]
    out["Median"] = df["Median Return"]
    out["Best"] = df["High Return"]
    out["Current"] = df["Current Price"]
    out["Current PE"] = df["Trailing PE"]
    out["Forward PE"] = df["Forward PE"]
    out["Growth Last Year"] = df["Growth Last Year"]
    out["Growth This Year Est"] = df["Growth This Year Est"]
    out["Growth Next Year Est"] = df["Growth Next Year Est"]
    out["Target Ticker"] = df["Yahoo Ticker"]
    out["Worst Target"] = df["Target Low"]
    out["Average Target"] = df["Target Mean"]
    out["Median Target"] = df["Target Median"]
    out["Best Target"] = df["Target High"]
    out["EPS Last Year"] = df["EPS Last Year"]
    out["EPS This Year Est Avg"] = df["EPS This Year Est Avg"]
    out["EPS Next Year Est Avg"] = df["EPS Next Year Est Avg"]

    return out.sort_values("% of Portfolio", ascending=False).reset_index(drop=True)


def _write_number_or_dash(ws, row, col, val, num_fmt, dash_fmt):
    if val is not None and pd.notna(val):
        ws.write_number(row, col, float(val), num_fmt)
    else:
        ws.write(row, col, "-", dash_fmt)


def _write_summary_pe_history_data_and_charts(workbook, worksheet, writer, pe_history, chart_start_row, start_col=1):
    if pe_history is None or pe_history.empty:
        worksheet.write(chart_start_row, start_col, "PE history charts will appear after more runs are saved.")
        return

    hist = pe_history.copy()
    hist["Date"] = pd.to_datetime(hist["Date"], errors="coerce")
    hist = hist.dropna(subset=["Date", "ETF"])

    cutoff = pd.Timestamp.now(tz="America/Toronto").tz_localize(None) - pd.DateOffset(years=1)
    hist_1y = hist[hist["Date"] >= cutoff].copy()
    if hist_1y.empty:
        hist_1y = hist.copy()

    hist_1y = hist_1y.sort_values(["ETF", "Date"])

    data_sheet_name = "PE_History_Data"
    data_ws = workbook.add_worksheet(data_sheet_name)
    writer.sheets[data_sheet_name] = data_ws
    data_ws.hide()

    headers = ["Date", "ETF", "PE Ratio", "Forward PE", "PE coverage", "Forward PE coverage", "YTD", "Covered Weight"]
    for c, h in enumerate(headers):
        data_ws.write(0, c, h)

    date_fmt = workbook.add_format({"num_format": "yyyy-mm-dd"})
    num_fmt = workbook.add_format({"num_format": "0.00"})
    pct_fmt = workbook.add_format({"num_format": "0.00%"})

    for r, (_, row) in enumerate(hist_1y.iterrows(), start=1):
        data_ws.write_datetime(r, 0, pd.Timestamp(row["Date"]).to_pydatetime(), date_fmt)
        data_ws.write(r, 1, row.get("ETF", ""))
        _write_number_or_dash(data_ws, r, 2, row.get("PE Ratio"), num_fmt, num_fmt)
        _write_number_or_dash(data_ws, r, 3, row.get("Forward PE"), num_fmt, num_fmt)
        _write_number_or_dash(data_ws, r, 4, row.get("PE coverage"), pct_fmt, pct_fmt)
        _write_number_or_dash(data_ws, r, 5, row.get("Forward PE coverage"), pct_fmt, pct_fmt)
        _write_number_or_dash(data_ws, r, 6, row.get("YTD"), pct_fmt, pct_fmt)
        _write_number_or_dash(data_ws, r, 7, row.get("Covered Weight"), pct_fmt, pct_fmt)

    worksheet.write(chart_start_row, start_col, "PE history, last 1 year", workbook.add_format({"bold": True, "font_size": 12}))

    for i, etf in enumerate(hist_1y["ETF"].dropna().astype(str).unique()):
        chart_row = chart_start_row + 2 + (i // 2) * 16
        chart_col = start_col + (i % 2) * 8
        seq_positions = [j + 1 for j, (_, row) in enumerate(hist_1y.iterrows()) if str(row.get("ETF")) == etf]

        if not seq_positions:
            continue

        first_row = min(seq_positions)
        last_row = max(seq_positions)

        chart = workbook.add_chart({"type": "line"})
        chart.add_series({
            "name": f"{etf} Current PE",
            "categories": [data_sheet_name, first_row, 0, last_row, 0],
            "values": [data_sheet_name, first_row, 2, last_row, 2],
            "marker": {"type": "circle", "size": 4},
        })
        chart.add_series({
            "name": f"{etf} Forward PE",
            "categories": [data_sheet_name, first_row, 0, last_row, 0],
            "values": [data_sheet_name, first_row, 3, last_row, 3],
            "marker": {"type": "diamond", "size": 4},
        })
        chart.set_title({"name": f"{etf} PE vs Forward PE"})
        chart.set_x_axis({"name": "Date", "date_axis": True, "num_format": "mmm yyyy"})
        chart.set_y_axis({"name": "PE", "major_gridlines": {"visible": True}})
        chart.set_legend({"position": "bottom"})
        chart.set_size({"width": 520, "height": 300})
        worksheet.insert_chart(chart_row, chart_col, chart)


def _make_edge_formats(workbook):
    base = {"border": 1, "align": "center", "valign": "vcenter"}
    return {
        "top_left": workbook.add_format({**base, "top": 2, "left": 2}),
        "top": workbook.add_format({**base, "top": 2}),
        "top_right": workbook.add_format({**base, "top": 2, "right": 2}),
        "left": workbook.add_format({**base, "left": 2}),
        "right": workbook.add_format({**base, "right": 2}),
        "bottom_left": workbook.add_format({**base, "bottom": 2, "left": 2}),
        "bottom": workbook.add_format({**base, "bottom": 2}),
        "bottom_right": workbook.add_format({**base, "bottom": 2, "right": 2}),
    }


def _apply_thick_outer_block(ws, edge_formats, first_row, first_col, last_row, last_col):
    """Apply a thick outside border to a rectangular section without changing inner cell formats."""
    for r in range(first_row, last_row + 1):
        for c in range(first_col, last_col + 1):
            if r == first_row and c == first_col:
                fmt = edge_formats["top_left"]
            elif r == first_row and c == last_col:
                fmt = edge_formats["top_right"]
            elif r == last_row and c == first_col:
                fmt = edge_formats["bottom_left"]
            elif r == last_row and c == last_col:
                fmt = edge_formats["bottom_right"]
            elif r == first_row:
                fmt = edge_formats["top"]
            elif r == last_row:
                fmt = edge_formats["bottom"]
            elif c == first_col:
                fmt = edge_formats["left"]
            elif c == last_col:
                fmt = edge_formats["right"]
            else:
                continue

            ws.conditional_format(r, c, r, c, {"type": "no_blanks", "format": fmt})
            ws.conditional_format(r, c, r, c, {"type": "blanks", "format": fmt})






def _excel_quote_sheet_name(sheet_name):
    """Return a safely quoted Excel sheet reference name."""
    return str(sheet_name).replace("'", "''")



def _write_etf_overlap_sheet(workbook, writer, all_details, etf_sheet_map):
    """
    ETF Overlap tab, corrected version.

    What it does:
      1. Uses the same ticker + weight data written to each ETF tab by prepare_detail_sheet().
      2. Precomputes all ETF pair overlaps in Python.
      3. Uses minimum-weight overlap as the main overlap method:
           overlap = SUM(MIN(weight_in_ETF_1, weight_in_ETF_2))
         Example: ETF A NVDA 60%, META 40%; ETF B NVDA 40%, META 60% => overlap = 40% + 40% = 80%.
      4. Puts the ETF dropdown list on a separate hidden sheet so it cannot be overwritten.
      5. Visible ETF Overlap tab uses simple INDEX/MATCH formulas only.
    """

    try:
        workbook.set_calc_mode("auto")
    except Exception:
        pass

    # ---------------- Formats ----------------
    title_fmt = workbook.add_format({"bold": True, "font_size": 14, "align": "left", "valign": "vcenter"})
    label_fmt = workbook.add_format({"bold": True, "border": 1, "align": "left", "valign": "vcenter", "bg_color": "#E2F0D9"})
    input_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter", "bg_color": "#FFF2CC"})
    header_fmt = workbook.add_format({"bold": True, "border": 1, "align": "center", "valign": "vcenter", "bg_color": "#D9EAF7"})
    text_fmt = workbook.add_format({"border": 1, "align": "left", "valign": "vcenter"})
    center_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter"})
    number_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter", "num_format": "#,##0"})
    pct_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter", "num_format": "0.00%"})
    note_fmt = workbook.add_format({"italic": True, "font_color": "#666666"})

    # ---------------- Build temporary ETF holdings from each ETF tab's source data ----------------
    details_by_etf = {}
    for details in all_details:
        if details is None or details.empty or "ETF" not in details.columns:
            continue
        vals = details["ETF"].dropna().astype(str).unique()
        if len(vals) > 0:
            details_by_etf[str(vals[0])] = details

    etf_holdings = {}
    etf_names = []

    for item in etf_sheet_map:
        etf_label = str(item.get("etf", "")).strip()
        sheet_name = str(item.get("sheet_name", "")).strip()
        details = details_by_etf.get(etf_label)

        if not sheet_name or details is None or details.empty:
            continue

        # This is exactly what each ETF tab writes: column A = Ticker, column B = % of Portfolio.
        try:
            tab_df = prepare_detail_sheet(details)
        except Exception:
            continue

        if tab_df.empty or "Ticker" not in tab_df.columns or "% of Portfolio" not in tab_df.columns:
            continue

        ticker_map = {}
        for _, row in tab_df.iterrows():
            ticker = str(row.get("Ticker", "")).strip().upper()
            if not ticker or ticker.lower() in {"nan", "none", "-", "--"}:
                continue

            weight = safe_float_value(row.get("% of Portfolio"))
            if weight is None or pd.isna(weight) or weight <= 0:
                continue

            name = ticker
            if "Yahoo Ticker" in details.columns and "Name" in details.columns:
                try:
                    m = details[details["Yahoo Ticker"].astype(str).str.upper().str.strip() == ticker]
                    if not m.empty:
                        nv = m.iloc[0].get("Name", ticker)
                        if pd.notna(nv) and str(nv).strip():
                            name = str(nv).strip()
                except Exception:
                    pass

            if ticker not in ticker_map:
                ticker_map[ticker] = {"Ticker": ticker, "Name": name, "Weight": float(weight)}
            else:
                ticker_map[ticker]["Weight"] += float(weight)
                if ticker_map[ticker]["Name"] == ticker and name != ticker:
                    ticker_map[ticker]["Name"] = name

        rows = sorted(ticker_map.values(), key=lambda x: x["Weight"], reverse=True)
        if rows:
            etf_holdings[sheet_name] = rows
            etf_names.append(sheet_name)

    etf_names = list(dict.fromkeys(etf_names))

    # ---------------- Visible tab ----------------
    ws = workbook.add_worksheet("ETF Overlap")
    writer.sheets["ETF Overlap"] = ws
    ws.write(0, 0, "ETF Overlap", title_fmt)

    if len(etf_names) < 2:
        ws.write(2, 0, "Need at least two ETF sheets to calculate overlap.", text_fmt)
        return

    default_1 = "QQQ" if "QQQ" in etf_names else etf_names[0]
    default_2 = "XLK" if "XLK" in etf_names and "XLK" != default_1 else next((x for x in etf_names if x != default_1), etf_names[0])

    # ---------------- Hidden ETF dropdown list, separate sheet so it cannot be overwritten ----------------
    list_ws = workbook.add_worksheet("Overlap_Lists")
    writer.sheets["Overlap_Lists"] = list_ws
    list_ws.write(0, 0, "ETF List", header_fmt)
    for i, etf in enumerate(etf_names, start=1):
        list_ws.write(i, 0, etf)
    list_ws.hide()
    list_last_row = len(etf_names) + 1

    # ---------------- Hidden temporary holdings sheet ----------------
    temp_ws = workbook.add_worksheet("Overlap_Tickers")
    writer.sheets["Overlap_Tickers"] = temp_ws
    temp_headers = ["ETF", "Ticker", "Name", "Weight"]
    for c, h in enumerate(temp_headers):
        temp_ws.write(0, c, h, header_fmt)

    temp_row = 1
    for etf in etf_names:
        for r in etf_holdings[etf]:
            temp_ws.write(temp_row, 0, etf)
            temp_ws.write(temp_row, 1, r["Ticker"])
            temp_ws.write(temp_row, 2, r["Name"])
            temp_ws.write_number(temp_row, 3, r["Weight"], pct_fmt)
            temp_row += 1
    temp_ws.set_column("A:A", 14)
    temp_ws.set_column("B:B", 14)
    temp_ws.set_column("C:C", 36)
    temp_ws.set_column("D:D", 14)
    temp_ws.hide()

    # ---------------- Python precomputed pair overlap ----------------
    pair_rows = []
    summary_rows = []
    pair_summary = {}

    for etf1 in etf_names:
        h1 = {r["Ticker"]: r for r in etf_holdings[etf1]}
        for etf2 in etf_names:
            if etf1 == etf2:
                continue

            h2 = {r["Ticker"]: r for r in etf_holdings[etf2]}
            common = sorted(
                set(h1).intersection(h2),
                key=lambda t: min(h1[t]["Weight"], h2[t]["Weight"]),
                reverse=True,
            )

            overlap_weight = sum(min(h1[t]["Weight"], h2[t]["Weight"]) for t in common)
            raw_etf1_common_weight = sum(h1[t]["Weight"] for t in common)
            raw_etf2_common_weight = sum(h2[t]["Weight"] for t in common)

            summary = {
                "Pair Key": f"{etf1}|{etf2}",
                "ETF 1": etf1,
                "ETF 2": etf2,
                "Count": len(common),
                "Overlap Weight": overlap_weight,
                "Raw ETF 1 Common Weight": raw_etf1_common_weight,
                "Raw ETF 2 Common Weight": raw_etf2_common_weight,
            }
            pair_summary[(etf1, etf2)] = summary
            summary_rows.append(summary)

            for rank, ticker in enumerate(common, start=1):
                w1 = h1[ticker]["Weight"]
                w2 = h2[ticker]["Weight"]
                min_w = min(w1, w2)
                pair_rows.append({
                    "Pair Key": f"{etf1}|{etf2}",
                    "ETF 1": etf1,
                    "ETF 2": etf2,
                    "Rank": rank,
                    "Ticker": ticker,
                    "Name": h1[ticker].get("Name") or h2[ticker].get("Name") or ticker,
                    "ETF 1 Weight": w1,
                    "ETF 2 Weight": w2,
                    "Min Weight": min_w,
                    "Rank Key": f"{etf1}|{etf2}|{rank}",
                })

    # Hidden pair detail sheet.
    pair_ws = workbook.add_worksheet("Overlap_Pairs")
    writer.sheets["Overlap_Pairs"] = pair_ws
    pair_headers = ["Pair Key", "ETF 1", "ETF 2", "Rank", "Ticker", "Name", "ETF 1 Weight", "ETF 2 Weight", "Min Weight", "Rank Key"]
    for c, h in enumerate(pair_headers):
        pair_ws.write(0, c, h, header_fmt)

    for r, row in enumerate(pair_rows, start=1):
        pair_ws.write(r, 0, row["Pair Key"])
        pair_ws.write(r, 1, row["ETF 1"])
        pair_ws.write(r, 2, row["ETF 2"])
        pair_ws.write_number(r, 3, row["Rank"], number_fmt)
        pair_ws.write(r, 4, row["Ticker"])
        pair_ws.write(r, 5, row["Name"])
        pair_ws.write_number(r, 6, row["ETF 1 Weight"], pct_fmt)
        pair_ws.write_number(r, 7, row["ETF 2 Weight"], pct_fmt)
        pair_ws.write_number(r, 8, row["Min Weight"], pct_fmt)
        pair_ws.write(r, 9, row["Rank Key"])

    pair_ws.set_column("A:A", 24)
    pair_ws.set_column("B:C", 12)
    pair_ws.set_column("D:D", 8)
    pair_ws.set_column("E:E", 12)
    pair_ws.set_column("F:F", 36)
    pair_ws.set_column("G:I", 14)
    pair_ws.set_column("J:J", 28)
    pair_ws.hide()
    last_pair_row = max(len(pair_rows) + 1, 2)

    # Hidden summary lookup sheet.
    sum_ws = workbook.add_worksheet("Overlap_Summary")
    writer.sheets["Overlap_Summary"] = sum_ws
    sum_headers = ["Pair Key", "ETF 1", "ETF 2", "# Overlap", "Overlap Weight", "Raw ETF 1 Common Weight", "Raw ETF 2 Common Weight"]
    for c, h in enumerate(sum_headers):
        sum_ws.write(0, c, h, header_fmt)

    summary_rows = sorted(summary_rows, key=lambda x: (x["ETF 1"], x["ETF 2"]))
    for r, row in enumerate(summary_rows, start=1):
        sum_ws.write(r, 0, row["Pair Key"])
        sum_ws.write(r, 1, row["ETF 1"])
        sum_ws.write(r, 2, row["ETF 2"])
        sum_ws.write_number(r, 3, row["Count"], number_fmt)
        sum_ws.write_number(r, 4, row["Overlap Weight"], pct_fmt)
        sum_ws.write_number(r, 5, row["Raw ETF 1 Common Weight"], pct_fmt)
        sum_ws.write_number(r, 6, row["Raw ETF 2 Common Weight"], pct_fmt)
    sum_ws.hide()
    last_summary_row = max(len(summary_rows) + 1, 2)

    # ---------------- User input area ----------------
    ws.write(1, 0, "ETF 1", label_fmt)
    ws.write(1, 1, default_1, input_fmt)
    ws.write(2, 0, "ETF 2", label_fmt)
    ws.write(2, 1, default_2, input_fmt)
    ws.write(1, 3, "Select any two ETF tabs from this report.", note_fmt)
    ws.write(2, 3, "Main overlap uses SUM(MIN(weight in ETF 1, weight in ETF 2)).", note_fmt)

    validation_source = f"=Overlap_Lists!$A$2:$A${list_last_row}"
    ws.data_validation("B2", {"validate": "list", "source": validation_source})
    ws.data_validation("B3", {"validate": "list", "source": validation_source})

    # ---------------- Metrics ----------------
    default_summary = pair_summary.get((default_1, default_2), {
        "Count": 0,
        "Overlap Weight": 0,
        "Raw ETF 1 Common Weight": 0,
        "Raw ETF 2 Common Weight": 0,
    })

    pair_key_lookup = f"Overlap_Summary!$A$2:$A${last_summary_row}"
    count_lookup = f"Overlap_Summary!$D$2:$D${last_summary_row}"
    overlap_lookup = f"Overlap_Summary!$E$2:$E${last_summary_row}"
    raw1_lookup = f"Overlap_Summary!$F$2:$F${last_summary_row}"
    raw2_lookup = f"Overlap_Summary!$G$2:$G${last_summary_row}"

    ws.write(4, 0, "Metric", header_fmt)
    ws.write(4, 1, "Value", header_fmt)

    ws.write(5, 0, "Number of overlapping stocks", text_fmt)
    ws.write_formula(5, 1, f'=IFERROR(INDEX({count_lookup},MATCH($B$2&"|"&$B$3,{pair_key_lookup},0)),0)', number_fmt, default_summary["Count"])

    # The two ETF overlap percentages are equal by the user's requested min-weight method.
    ws.write(6, 0, "ETF 1 overlap weight, min method", text_fmt)
    ws.write_formula(6, 1, f'=IFERROR(INDEX({overlap_lookup},MATCH($B$2&"|"&$B$3,{pair_key_lookup},0)),0)', pct_fmt, default_summary["Overlap Weight"])

    ws.write(7, 0, "ETF 2 overlap weight, min method", text_fmt)
    ws.write_formula(7, 1, f'=IFERROR(INDEX({overlap_lookup},MATCH($B$2&"|"&$B$3,{pair_key_lookup},0)),0)', pct_fmt, default_summary["Overlap Weight"])

    ws.write(8, 0, "Overlap weight", text_fmt)
    ws.write_formula(8, 1, f'=IFERROR(INDEX({overlap_lookup},MATCH($B$2&"|"&$B$3,{pair_key_lookup},0)),0)', pct_fmt, default_summary["Overlap Weight"])

    ws.write(9, 0, "ETF 1 raw common-stock weight", text_fmt)
    ws.write_formula(9, 1, f'=IFERROR(INDEX({raw1_lookup},MATCH($B$2&"|"&$B$3,{pair_key_lookup},0)),0)', pct_fmt, default_summary["Raw ETF 1 Common Weight"])

    ws.write(10, 0, "ETF 2 raw common-stock weight", text_fmt)
    ws.write_formula(10, 1, f'=IFERROR(INDEX({raw2_lookup},MATCH($B$2&"|"&$B$3,{pair_key_lookup},0)),0)', pct_fmt, default_summary["Raw ETF 2 Common Weight"])

    # ---------------- Selected ticker-level table ----------------
    table_row = 12
    headers = ["Ticker", "Name", "ETF 1 Weight", "ETF 2 Weight", "Min Weight"]
    for c, h in enumerate(headers):
        ws.write(table_row, c, h, header_fmt)

    default_rows = [r for r in pair_rows if r["ETF 1"] == default_1 and r["ETF 2"] == default_2]
    rank_key_range = f"Overlap_Pairs!$J$2:$J${last_pair_row}"
    ticker_range = f"Overlap_Pairs!$E$2:$E${last_pair_row}"
    name_range = f"Overlap_Pairs!$F$2:$F${last_pair_row}"
    w1_range = f"Overlap_Pairs!$G$2:$G${last_pair_row}"
    w2_range = f"Overlap_Pairs!$H$2:$H${last_pair_row}"
    min_range = f"Overlap_Pairs!$I$2:$I${last_pair_row}"

    output_rows = 500
    for i in range(output_rows):
        r0 = table_row + 1 + i
        rank = i + 1
        rank_key_formula = f'$B$2&"|"&$B$3&"|"&{rank}'
        cached = default_rows[i] if i < len(default_rows) else None
        ws.write_formula(r0, 0, f'=IFERROR(INDEX({ticker_range},MATCH({rank_key_formula},{rank_key_range},0)),"")', text_fmt, cached["Ticker"] if cached else "")
        ws.write_formula(r0, 1, f'=IFERROR(INDEX({name_range},MATCH({rank_key_formula},{rank_key_range},0)),"")', text_fmt, cached["Name"] if cached else "")
        ws.write_formula(r0, 2, f'=IFERROR(INDEX({w1_range},MATCH({rank_key_formula},{rank_key_range},0)),"")', pct_fmt, cached["ETF 1 Weight"] if cached else "")
        ws.write_formula(r0, 3, f'=IFERROR(INDEX({w2_range},MATCH({rank_key_formula},{rank_key_range},0)),"")', pct_fmt, cached["ETF 2 Weight"] if cached else "")
        ws.write_formula(r0, 4, f'=IFERROR(INDEX({min_range},MATCH({rank_key_formula},{rank_key_range},0)),"")', pct_fmt, cached["Min Weight"] if cached else "")

    # ---------------- Static all-pair summary on the right, useful even if formulas are not recalculated ----------------
    summary_col = 6  # G
    summary_row = 4
    static_headers = ["ETF 1", "ETF 2", "# Overlap", "Overlap Weight", "Raw ETF 1 Common", "Raw ETF 2 Common"]
    for c, h in enumerate(static_headers):
        ws.write(summary_row, summary_col + c, h, header_fmt)

    for r, row in enumerate(summary_rows, start=summary_row + 1):
        ws.write(r, summary_col + 0, row["ETF 1"], text_fmt)
        ws.write(r, summary_col + 1, row["ETF 2"], text_fmt)
        ws.write_number(r, summary_col + 2, row["Count"], number_fmt)
        ws.write_number(r, summary_col + 3, row["Overlap Weight"], pct_fmt)
        ws.write_number(r, summary_col + 4, row["Raw ETF 1 Common Weight"], pct_fmt)
        ws.write_number(r, summary_col + 5, row["Raw ETF 2 Common Weight"], pct_fmt)

    if summary_rows:
        ws.autofilter(summary_row, summary_col, summary_row + len(summary_rows), summary_col + len(static_headers) - 1)

    ws.set_column("A:A", 30)
    ws.set_column("B:B", 38)
    ws.set_column("C:E", 16)
    ws.set_column("D:D", 16)
    ws.set_column("E:E", 16)
    ws.set_column("G:H", 14)
    ws.set_column("I:I", 12)
    ws.set_column("J:L", 18)
    ws.freeze_panes(table_row + 1, 0)

def export_excel_report(all_details, summaries, output_path=None, report_date=None, pe_history=None):
    output_path = Path(output_path) if output_path else REPORT_PATH

    if report_date is None:
        today = pd.Timestamp.now(tz="America/Toronto")
        report_date = f"{today.month}/{today.day}/{today.year}"

    last_year, current_year, next_year = get_report_years()

    summary_df = pd.DataFrame(summaries)
    summary_rows = build_excel_summary_rows(summary_df)
    fear_greed_summary, fear_greed_error = build_fear_greed_summary()
    vix_summary, vix_error = build_vix_summary()
    pe_history = load_pe_history() if pe_history is None else pe_history

    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        workbook = writer.book
        try:
            workbook.set_calc_mode("auto")
        except Exception:
            pass

        header_fmt = workbook.add_format({"bold": True, "align": "center", "valign": "vcenter", "border": 1, "bg_color": "#E2F0D9"})
        header_white_fmt = workbook.add_format({"bold": True, "align": "center", "valign": "vcenter", "border": 1, "bg_color": "#FFFFFF"})
        section_fmt = workbook.add_format({"bold": True, "font_size": 12, "align": "left", "valign": "vcenter"})
        group_header_fmt = workbook.add_format({"bold": True, "align": "center", "valign": "vcenter", "border": 1, "bg_color": "#FFFFFF"})
        blank_no_border_fmt = workbook.add_format({"align": "center", "valign": "vcenter"})

        normal_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter"})
        text_fmt = workbook.add_format({"border": 1, "align": "left", "valign": "vcenter"})
        pct_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter", "num_format": "0.00%"})
        pct_red_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter", "num_format": "0.00%", "font_color": "#FF0000"})
        pct_green_fill_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter", "num_format": "0.00%", "bg_color": "#C6E0B4"})
        money_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter", "num_format": "#,##0.00"})
        money_red_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter", "num_format": "#,##0.00", "font_color": "#FF0000"})
        number_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter", "num_format": "0.00"})
        int_fmt = workbook.add_format({"border": 1, "align": "center", "valign": "vcenter", "num_format": "#,##0"})
        total_pct_fmt = workbook.add_format({"bold": True, "border": 1, "align": "center", "valign": "vcenter", "num_format": "0.00%"})
        total_text_fmt = workbook.add_format({"bold": True, "border": 1, "align": "center", "valign": "vcenter"})
        edge_formats = _make_edge_formats(workbook)

        # ---------------- Summary sheet ----------------
        worksheet = workbook.add_worksheet("Summary")
        writer.sheets["Summary"] = worksheet

        # Summary sheet column layout is intentionally explicit.
        # Excel columns:
        # B ETF/date, C reliable, D YTD, E Average, F Median, G WORST, H BEST,
        # I Current pr, J worst price, K PE Ratio, L Forward PE,
        # M PE coverage, N Forward PE coverage.
        start_row = 3
        start_col = 1
        summary_columns = [
            ("B", report_date, None, header_fmt),
            ("C", "reliable", "reliable", pct_fmt),
            ("D", "YTD", "YTD", pct_fmt),
            ("E", "Average", "Average", pct_fmt),
            ("F", "Median", "Median", pct_fmt),
            ("G", "WORST", "WORST", pct_fmt),
            ("H", "BEST", "BEST", pct_fmt),
            ("I", "Current pr", "Current pr", money_fmt),
            ("J", "worst price", "worst price", money_red_fmt),
            ("K", "PE Ratio", "PE Ratio", number_fmt),
            ("L", "Forward PE", "Forward PE", number_fmt),
            ("M", "PE coverage", "PE coverage", pct_fmt),
            ("N", "Forward PE coverage", "Forward PE coverage", pct_fmt),
        ]

        worksheet.write(0, 1, "Summary order check: K=PE Ratio, L=Forward PE, M=PE coverage, N=Forward PE coverage", section_fmt)
        worksheet.write(start_row, start_col, report_date, header_fmt)
        for offset, (_excel_col, label, _key, _fmt) in enumerate(summary_columns[1:], start=1):
            worksheet.write(start_row, start_col + offset, label, header_white_fmt)

        # Hard override for Summary header cells. 0-indexed columns: K=10, L=11, M=12, N=13.
        worksheet.write(start_row, 10, "PE Ratio", header_white_fmt)
        worksheet.write(start_row, 11, "Forward PE", header_white_fmt)
        worksheet.write(start_row, 12, "PE coverage", header_white_fmt)
        worksheet.write(start_row, 13, "Forward PE coverage", header_white_fmt)

        for i, row in summary_rows.iterrows():
            r = start_row + 1 + i
            worksheet.write(r, start_col, row["ETF"], text_fmt)
            for offset, (_excel_col, _label, key, value_fmt) in enumerate(summary_columns[1:], start=1):
                _write_number_or_dash(worksheet, r, start_col + offset, row[key], value_fmt, normal_fmt)

            # Hard override for Summary value cells. This prevents any earlier layout code from keeping
            # Forward PE separated from PE Ratio.
            _write_number_or_dash(worksheet, r, 10, row["PE Ratio"], number_fmt, normal_fmt)
            _write_number_or_dash(worksheet, r, 11, row["Forward PE"], number_fmt, normal_fmt)
            _write_number_or_dash(worksheet, r, 12, row["PE coverage"], pct_fmt, normal_fmt)
            _write_number_or_dash(worksheet, r, 13, row["Forward PE coverage"], pct_fmt, normal_fmt)

        end_row = start_row + len(summary_rows)
        worksheet.conditional_format(f"D5:H{end_row + 1}", {"type": "cell", "criteria": "<", "value": 0, "format": pct_red_fmt})
        worksheet.conditional_format(f"D5:H{end_row + 1}", {"type": "cell", "criteria": ">=", "value": 0.3, "format": pct_green_fill_fmt})

        fg_start = end_row + 4
        worksheet.write(fg_start, start_col, "Fear & Greed Index", section_fmt)
        if fear_greed_error:
            worksheet.write(fg_start + 1, start_col, f"Fear & Greed data unavailable: {fear_greed_error}", text_fmt)
            vix_start = fg_start + 4
        else:
            for j, h in enumerate(fear_greed_summary.columns, start_col):
                worksheet.write(fg_start + 1, j, h, header_white_fmt)
            for i, row in fear_greed_summary.iterrows():
                rr = fg_start + 2 + i
                for j, h in enumerate(fear_greed_summary.columns, start_col):
                    val = row[h]
                    if h == "Score":
                        worksheet.write_number(rr, j, float(val), number_fmt)
                    else:
                        worksheet.write(rr, j, str(val), text_fmt if h in ["Metric", "Rating", "Source"] else normal_fmt)
            vix_start = fg_start + 5

        worksheet.write(vix_start, start_col, "VIX percentile analysis", section_fmt)
        if vix_error:
            worksheet.write(vix_start + 1, start_col, f"VIX data unavailable: {vix_error}", text_fmt)
            chart_start = vix_start + 4
        else:
            for j, h in enumerate(vix_summary.columns, start_col):
                worksheet.write(vix_start + 1, j, h, header_white_fmt)
            for i, row in vix_summary.iterrows():
                rr = vix_start + 2 + i
                for j, h in enumerate(vix_summary.columns, start_col):
                    val = row[h]
                    if h == "Period":
                        worksheet.write(rr, j, val, text_fmt)
                    elif h in ["Start Date", "End Date"]:
                        worksheet.write(rr, j, str(val), normal_fmt)
                    elif h == "Trading Days":
                        worksheet.write_number(rr, j, int(val), int_fmt)
                    elif h == "Percentile Rank":
                        worksheet.write_number(rr, j, val / 100.0, pct_fmt)
                    else:
                        worksheet.write_number(rr, j, float(val), number_fmt)
            chart_start = vix_start + 2 + len(vix_summary) + 3

        _write_summary_pe_history_data_and_charts(workbook, worksheet, writer, pe_history, chart_start, start_col=start_col)

        worksheet.set_column("B:B", 14)
        worksheet.set_column("C:H", 12)
        worksheet.set_column("I:N", 14)
        worksheet.freeze_panes(start_row + 1, 0)

        # ---------------- ETF detail sheets ----------------
        etf_sheet_map = []

        for details in all_details:
            if details.empty:
                continue

            etf_values = details["ETF"].dropna().astype(str).unique()
            etf = etf_values[0] if len(etf_values) > 0 else f"ETF_{len(writer.sheets)}"

            safe_sheet_base = (
                etf.replace(".", "_").replace("/", "_").replace("\\", "_")
                .replace("?", "_").replace("*", "_").replace("[", "_")
                .replace("]", "_").replace(":", "_")
            )[:31]

            safe_sheet = safe_sheet_base or f"ETF_{len(writer.sheets)}"
            counter = 1
            used_sheets_lower = {name.lower() for name in writer.sheets.keys()}
            while safe_sheet.lower() in used_sheets_lower:
                suffix = f"_{counter}"
                safe_sheet = safe_sheet_base[:31 - len(suffix)] + suffix
                counter += 1

            detail = prepare_detail_sheet(details)
            ws = workbook.add_worksheet(safe_sheet)
            writer.sheets[safe_sheet] = ws
            etf_sheet_map.append({"etf": etf, "sheet_name": safe_sheet})

            ws.write(0, 0, report_date, group_header_fmt)
            ws.write(0, 1, "Weight", group_header_fmt)
            ws.write(0, 2, "YTD", group_header_fmt)
            ws.merge_range(0, 3, 0, 6, "Analyst Target Return", group_header_fmt)
            ws.write(0, 7, "", blank_no_border_fmt)
            ws.write(0, 8, "Price", group_header_fmt)
            ws.merge_range(0, 9, 0, 10, "PE ratio", group_header_fmt)
            ws.merge_range(0, 11, 0, 13, "Growth", group_header_fmt)
            ws.write(0, 14, "", blank_no_border_fmt)
            ws.merge_range(0, 15, 0, 18, "12-month estimate price target", group_header_fmt)
            ws.merge_range(0, 19, 0, 21, "EPS (Est Avg)", group_header_fmt)

            headers2 = [
                etf, "% of Portfolio", "YTD",
                "Worst", "Average", "Median", "Best", "",
                "Current",
                "Current", "Forward",
                str(last_year), str(current_year), str(next_year), "",
                "Worst", "Average", "Median", "Best",
                str(last_year), str(current_year), str(next_year),
            ]

            for c, h in enumerate(headers2):
                ws.write(1, c, h, blank_no_border_fmt if c in [7, 14] else header_white_fmt)

            data_start = 2
            for i, row in detail.iterrows():
                r = data_start + i
                ws.write(r, 0, row["Ticker"], text_fmt)
                _write_number_or_dash(ws, r, 1, row["% of Portfolio"], pct_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 2, row["YTD"], pct_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 3, row["Worst"], pct_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 4, row["Average"], pct_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 5, row["Median"], pct_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 6, row["Best"], pct_fmt, normal_fmt)
                ws.write(r, 7, "", blank_no_border_fmt)
                _write_number_or_dash(ws, r, 8, row["Current"], money_red_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 9, row["Current PE"], number_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 10, row["Forward PE"], number_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 11, row["Growth Last Year"], pct_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 12, row["Growth This Year Est"], pct_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 13, row["Growth Next Year Est"], pct_fmt, normal_fmt)
                ws.write(r, 14, row["Target Ticker"], text_fmt)
                _write_number_or_dash(ws, r, 15, row["Worst Target"], money_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 16, row["Average Target"], money_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 17, row["Median Target"], money_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 18, row["Best Target"], money_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 19, row["EPS Last Year"], number_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 20, row["EPS This Year Est Avg"], number_fmt, normal_fmt)
                _write_number_or_dash(ws, r, 21, row["EPS Next Year Est Avg"], number_fmt, normal_fmt)

            total_row = data_start + len(detail) + 2
            first_excel_row = data_start + 1
            last_excel_row = data_start + len(detail)
            last_data_row = last_excel_row - 1

            ws.write(total_row, 0, "TOTAL", total_text_fmt)
            ws.write_formula(total_row, 1, f"=SUM(B{first_excel_row}:B{last_excel_row})", total_pct_fmt)
            ws.write_formula(total_row, 2, f"=SUMPRODUCT(B{first_excel_row}:B{last_excel_row},C{first_excel_row}:C{last_excel_row})", total_pct_fmt)
            ws.write_formula(total_row, 3, f"=SUMPRODUCT(B{first_excel_row}:B{last_excel_row},D{first_excel_row}:D{last_excel_row})", total_pct_fmt)
            ws.write_formula(total_row, 4, f"=SUMPRODUCT(B{first_excel_row}:B{last_excel_row},E{first_excel_row}:E{last_excel_row})", total_pct_fmt)
            ws.write_formula(total_row, 5, f"=SUMPRODUCT(B{first_excel_row}:B{last_excel_row},F{first_excel_row}:F{last_excel_row})", total_pct_fmt)
            ws.write_formula(total_row, 6, f"=SUMPRODUCT(B{first_excel_row}:B{last_excel_row},G{first_excel_row}:G{last_excel_row})", total_pct_fmt)

            ws.conditional_format(f"C3:G{last_excel_row}", {"type": "cell", "criteria": "<", "value": 0, "format": pct_red_fmt})
            ws.conditional_format(f"C3:G{last_excel_row}", {"type": "cell", "criteria": ">=", "value": 0.3, "format": pct_green_fill_fmt})
            ws.conditional_format(f"L3:N{last_excel_row}", {"type": "cell", "criteria": "<", "value": 0, "format": pct_red_fmt})
            ws.conditional_format(f"L3:N{last_excel_row}", {"type": "cell", "criteria": ">=", "value": 0.3, "format": pct_green_fill_fmt})

            # Thick outside blocks: title row + subheader row + all data rows.
            if len(detail) > 0:
                _apply_thick_outer_block(ws, edge_formats, 0, 3, last_data_row, 6)    # Analyst Target Return
                _apply_thick_outer_block(ws, edge_formats, 0, 8, last_data_row, 8)    # Price
                _apply_thick_outer_block(ws, edge_formats, 0, 9, last_data_row, 10)   # PE ratio
                _apply_thick_outer_block(ws, edge_formats, 0, 11, last_data_row, 13)  # Growth
                _apply_thick_outer_block(ws, edge_formats, 0, 15, last_data_row, 18)  # 12-month estimate price target
                _apply_thick_outer_block(ws, edge_formats, 0, 19, last_data_row, 21)  # EPS (Est Avg)

            ws.freeze_panes(2, 1)
            ws.set_column("A:A", 11)
            ws.set_column("B:B", 14)
            ws.set_column("C:G", 11)
            ws.set_column("H:H", 3)
            ws.set_column("I:I", 12)
            ws.set_column("J:K", 11)
            ws.set_column("L:N", 11)
            ws.set_column("O:O", 11)
            ws.set_column("P:S", 12)
            ws.set_column("T:V", 11)
            ws.set_default_row(18)

        # ---------------- ETF Overlap sheet ----------------
        _write_etf_overlap_sheet(workbook, writer, all_details, etf_sheet_map)

        print(f"Saved Excel report: {output_path}")
        return output_path


# ============================================================
# RUNNERS
# ============================================================

def run_one_etf(etf):
    if etf.upper() == "TECH":
        etf = "TECH.TO"
    if etf.upper() == "CHPS":
        etf = "CHPS.TO"

    holdings = pull_issuer_holdings(etf)
    enriched = calculate_returns(add_yahoo_targets(holdings))
    summary = summarize_etf(enriched, etf)
    print_summary(summary)

    output_cols = [
        "ETF", "Yahoo Ticker", "Raw Ticker", "Name", "Weight", "Weight Decimal",
        "Current Price", "Target Low", "Target Mean", "Target High", "Target Median", "Analyst Count",
        "Trailing PE", "Forward PE", "YTD Return",
        "EPS Last Year", "EPS This Year Est Avg", "EPS Next Year Est Avg",
        "Growth Last Year", "Growth This Year Est", "Growth Next Year Est",
        "Low Return", "Mean Return", "High Return", "Median Return",
        "Weighted Low Return", "Weighted Mean Return", "Weighted High Return", "Weighted Median Return",
        "Shares Held", "Identifier", "Source Note", "Yahoo Error",
    ]

    enriched = enriched[[c for c in output_cols if c in enriched.columns]]
    enriched = enriched.sort_values("Weight", ascending=False).reset_index(drop=True)
    return enriched, summary


def main_with_excel():
    print(f"ETF analyst report version: {REPORT_VERSION}")

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

    completed_etfs = {str(s.get("ETF", "")).strip() for s in summaries}
    required_etfs = set(ETFS)
    missing_required = sorted(required_etfs - completed_etfs)

    # Important: do not create/email a partial workbook.
    # Before this fix, if SOXX failed, the script still exported the workbook with
    # the successful ETFs only, so the email looked successful but SOXX was missing.
    if failures or missing_required:
        print("\nFailures:")
        for f in failures:
            print(f"{f['ETF']}: {f['Error']}")

        if missing_required:
            print("\nMissing required ETF(s): " + ", ".join(missing_required))

        raise RuntimeError(
            "Report incomplete. No Excel report was exported. "
            "Missing or failed ETF(s): " + ", ".join(missing_required or [f["ETF"] for f in failures])
        )

    pe_history = update_pe_history(summaries)
    export_excel_report(all_details, summaries, pe_history=pe_history)

    return all_details, summaries, failures



def main():
    return main_with_excel()


# ============================================================
# SCRIPT RUN
# ============================================================

if __name__ == "__main__":
    main_with_excel()
