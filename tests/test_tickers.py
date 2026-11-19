import pytest

from etf_report.tickers import is_plausible_yahoo_symbol, looks_like_bad_row, map_to_yahoo_symbol


@pytest.mark.parametrize(
    ("raw", "name", "identifier", "expected"),
    [
        ("UNKNOWN", "Apple Inc.", "037833100", "AAPL"),
        ("", "SK Hynix Inc.", "", "000660.KS"),
        ("AAPL US", "", "", "AAPL"),
        ("SHOP CN", "", "", "SHOP.TO"),
        ("2330 TT", "", "", "2330.TW"),
        ("2408.TT", "", "", "2408.TW"),
        ("285A.JP", "", "", "285A.T"),
        ("005930.KS", "", "", "005930.KS"),
        ("603986.C1", "GigaDevice Semiconductor", "", "603986.SS"),
        ("603986 C1", "GigaDevice Semiconductor", "", "603986.SS"),
        ("CXMT", "CXMT Corporation", "", "688825.SS"),
        ("BTMTQT8", "CXMT Corporation", "", "688825.SS"),
        ("7203 JT", "", "", "7203.T"),
        ("BRK.B US", "", "", "BRK-B"),
        ("KRX:5930", "", "", "005930.KS"),
        ("TSX:SHOP", "", "", "SHOP.TO"),
        ("AAPL.O", "", "", "AAPL"),
        ("BRK B", "Berkshire Hathaway Inc Class B", "", "BRK-B"),
    ],
)
def test_map_to_yahoo_symbol_regressions(raw, name, identifier, expected):
    assert map_to_yahoo_symbol(raw, name, identifier) == expected


def test_cash_like_rows_are_rejected():
    assert looks_like_bad_row("USD Cash and equivalents")
    assert looks_like_bad_row("US Treasury bill")
    assert map_to_yahoo_symbol("USD", "Cash", "") is None


def test_empty_ticker_returns_none():
    assert map_to_yahoo_symbol("", "", "") is None


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("AAPL", True),
        ("BRK-B", True),
        ("005930.KS", True),
        ("2330.TW", True),
        ("603986.SS", True),
        ("688825.SS", True),
        ("2408-TT", False),
        ("SHOP.TO", True),
        ("ARM$CBRS", False),
        ("IXTZ6", True),
        ("285A", True),
        ("AGPXX", False),
        ("FGXXX", False),
        ("", False),
    ],
)
def test_yahoo_symbol_preflight(symbol, expected):
    assert is_plausible_yahoo_symbol(symbol) is expected


@pytest.mark.parametrize(
    ("raw", "name", "expected"),
    [
        ("DOX", "Amdocs Limited", "DOX"),
        ("KLAR", "Klarna Group", "KLAR"),
        ("NTLA", "Intellia Therapeutics", "NTLA"),
        ("CADE", "Cadence Bank", "CADE"),
        ("PEGY", "Pineapple Energy", "PEGY"),
    ],
)
def test_valid_raw_ticker_wins_over_ambiguous_company_name(raw, name, expected):
    assert map_to_yahoo_symbol(raw, name, "") == expected


@pytest.mark.parametrize(
    "name",
    ["Amdocs Limited", "Klarna Group", "Intellia Therapeutics", "Cadence Bank", "Pineapple Energy"],
)
def test_name_fallback_avoids_substring_false_positives(name):
    assert map_to_yahoo_symbol("", name, "") is None


def test_totalenergies_is_not_filtered_as_summary_total():
    assert not looks_like_bad_row("TotalEnergies SE")
    assert looks_like_bad_row("Total")
    assert looks_like_bad_row("Portfolio Total")
