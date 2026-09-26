from pathlib import Path

ETFS = ["SPMO", "MAGS", "CHAT", "TECH.TO", "CHPS.TO", "SOXX", "SMH", "XLK", "QQQ"]
OUTPUT_DIR = Path("etf_analyst_target_outputs")

YAHOO_SLEEP_SECONDS = 0.25
YAHOO_MAX_WORKERS = 4
YAHOO_RETRY_MISSING_GROWTH = False
INCLUDE_CASH_FUTURES_SWAPS = False
AUTO_INSTALL_PLAYWRIGHT_IF_MISSING = False
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
        "issuer": "Roundhill live holdings",
        "url": "https://www.roundhillinvestments.com/etf/mags/",
        "factsheet_url": "https://www.roundhillinvestments.com/assets/pdfs/MAGS_Factsheet.pdf",
    },
    "CHAT": {
        "issuer": "Roundhill live holdings",
        "url": "https://www.roundhillinvestments.com/etf/chat/",
        "factsheet_url": "https://www.roundhillinvestments.com/assets/pdfs/CHAT_Factsheet.pdf",
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
