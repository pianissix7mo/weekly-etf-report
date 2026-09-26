import math

import pandas as pd
import pytest

from etf_report.analytics import (
    calculate_returns,
    weighted_forward_pe,
    weighted_harmonic_from_column,
    weighted_harmonic_pe,
)


def test_calculate_returns_preserves_existing_formula_contract():
    df = pd.DataFrame(
        {
            "Weight": [60.0, 40.0],
            "Current Price": [100.0, 50.0],
            "Target Low": [90.0, 45.0],
            "Target Mean": [120.0, 55.0],
            "Target High": [140.0, 70.0],
            "Target Median": [110.0, 52.5],
        }
    )
    out = calculate_returns(df)
    assert out["Weight Decimal"].tolist() == pytest.approx([0.6, 0.4])
    assert out["Mean Return"].tolist() == pytest.approx([0.2, 0.1])
    assert out["Weighted Mean Return"].sum() == pytest.approx(0.16)
    assert out["Weighted Low Return"].sum() == pytest.approx(-0.10)


def test_weighted_harmonic_pe_matches_original_formula():
    df = pd.DataFrame({"Weight Decimal": [0.6, 0.4], "PE": [20.0, 40.0]})
    pe, coverage = weighted_harmonic_from_column(df, "PE")
    expected = 1.0 / (0.6 / 20.0 + 0.4 / 40.0)
    assert pe == pytest.approx(expected)
    assert coverage == pytest.approx(1.0)


def test_harmonic_pe_uses_forward_when_trailing_missing():
    df = pd.DataFrame(
        {
            "Weight Decimal": [0.5, 0.5],
            "Trailing PE": [20.0, math.nan],
            "Forward PE": [18.0, 30.0],
        }
    )
    pe, coverage = weighted_harmonic_pe(df)
    expected = 1.0 / (0.5 / 20.0 + 0.5 / 30.0)
    assert pe == pytest.approx(expected)
    assert coverage == pytest.approx(1.0)


def test_non_positive_pe_is_excluded_from_coverage():
    df = pd.DataFrame({"Weight Decimal": [0.7, 0.3], "Forward PE": [25.0, -5.0]})
    pe, coverage = weighted_forward_pe(df)
    assert pe == pytest.approx(25.0)
    assert coverage == pytest.approx(0.7)


def test_missing_pe_column_returns_no_value_and_zero_coverage():
    df = pd.DataFrame({"Weight Decimal": [1.0]})
    pe, coverage = weighted_harmonic_from_column(df, "PE")
    assert pe is None
    assert coverage == 0.0
