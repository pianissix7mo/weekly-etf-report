"""Conservative analyst-target recovery and explicitly labeled share-class proxies."""
import math

CORE_TARGET_TICKERS = frozenset({
    "NVDA", "AAPL", "MSFT", "AMZN", "META", "AMD", "MU", "AVGO",
    "TSLA", "GOOGL", "GOOG",
})
TARGET_TRIO = ("Target Low", "Target Mean", "Target High")
TARGET_FIELDS = TARGET_TRIO + ("Target Median",)
QUOTE_SUMMARY_KEYS = {
    "Target Low": "targetLowPrice",
    "Target Mean": "targetMeanPrice",
    "Target High": "targetHighPrice",
    "Target Median": "targetMedianPrice",
}


def positive_float(value):
    """Exclude empty, zero, nonfinite and malformed prices, never invent targets."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def complete_target_trio(row):
    low, mean, high = (positive_float(row.get(key)) for key in TARGET_TRIO)
    return all(x is not None for x in (low, mean, high)) and low <= mean <= high


def fill_from_quote_summary(row, info):
    """Fallback to the SAME ticker's quoteSummary fields (not a guessed target)."""
    if not isinstance(info, dict):
        return row
    used = False
    for column, info_key in QUOTE_SUMMARY_KEYS.items():
        value = positive_float(info.get(info_key))
        if positive_float(row.get(column)) is None and value is not None:
            row[column] = value
            used = True
    if row.get("Analyst Count") is None:
        count = positive_float(info.get("numberOfAnalystOpinions"))
        if count is not None and count.is_integer():
            row["Analyst Count"] = int(count)
    if used:
        prior = row.get("Target Source")
        row["Target Source"] = (
            "Yahoo analyst endpoint + quote summary" if prior
            else "Yahoo quote summary (undated analyst targets)"
        )
    return row


def clean_target_fields(row):
    for column in TARGET_FIELDS:
        row[column] = positive_float(row.get(column))
    # Reject logically contradictory 12-month target bands rather than using
    # them in return calculations, even when Yahoo supplies all three values.
    if all(row[key] is not None for key in TARGET_TRIO) and not complete_target_trio(row):
        for column in TARGET_TRIO:
            row[column] = None
        row["Yahoo Error"] = (
            (str(row.get("Yahoo Error")) + " | ") if row.get("Yahoo Error") else ""
        ) + "inconsistent analyst target band"
    return row


def fill_alphabet_share_class_proxy(row, peer, peer_symbol):
    """Use independently fetched GOOG/GOOGL targets only with equal dated closes.

    It is an *estimate*, not this share class's published consensus. Never use
    stale, missing, chained-proxy or unusually divergent share-class quotes.
    """
    symbol = str(row.get("Yahoo Ticker") or "")
    if {symbol, peer_symbol} != {"GOOGL", "GOOG"}:
        return False
    if complete_target_trio(row) or not complete_target_trio(peer):
        return False
    if str(peer.get("Target Source") or "").startswith("ALPHABET PROXY:"):
        return False
    day = str(row.get("Price Date") or "")
    if not day or day != str(peer.get("Price Date") or ""):
        return False
    this_price = positive_float(row.get("Current Price"))
    peer_price = positive_float(peer.get("Current Price"))
    if this_price is None or peer_price is None:
        return False
    ratio = this_price / peer_price
    if not 0.90 <= ratio <= 1.10:
        return False
    for column in TARGET_FIELDS:
        target = positive_float(peer.get(column))
        row[column] = target * ratio if target is not None else None
    row["Analyst Count"] = None  # Peer-class analyst count is not direct coverage.
    row["Target Source"] = (
        f"ALPHABET PROXY: {peer_symbol} targets x {symbol}/{peer_symbol} "
        f"same-session close ratio ({day}); NOT direct {symbol} consensus"
    )
    return True
