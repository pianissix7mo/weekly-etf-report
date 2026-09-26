import pytest

from etf_report.tickers import looks_like_bad_row, map_to_yahoo_symbol


@pytest.mark.parametrize(
    ("raw", "name", "identifier", "expected"),
    [
        ("UNKNOWN", "Apple Inc.", "037833100", "AAPL"),
        ("", "SK Hynix Inc.", "", "000660.KS"),
        ("AAPL US", "", "", "AAPL"),
        ("SHOP CN", "", "", "SHOP.TO"),
        ("2330 TT", "", "", "2330.TW"),
        ("7203 JT", "", "", "7203.T"),
        ("BRK.B US", "", "", "BRK-B"),
        ("KRX:5930", "", "", "005930.KS"),
        ("TSX:SHOP", "", "", "SHOP.TO"),
        ("AAPL.O", "", "", "AAPL"),
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
