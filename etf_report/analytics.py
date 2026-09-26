import numpy as np


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
    if pe_col not in df.columns:
        return None, 0.0
    x = df.dropna(subset=[pe_col, "Weight Decimal"]).copy()
    x = x[(x[pe_col] > 0) & (x["Weight Decimal"] > 0)]
    if x.empty:
        return None, 0.0
    covered_weight = x["Weight Decimal"].sum()
    denom = (x["Weight Decimal"] / x[pe_col]).sum()
    return (None, covered_weight) if denom <= 0 else (covered_weight / denom, covered_weight)


def weighted_harmonic_pe(df):
    x = df.copy()
    if "Trailing PE" not in x.columns:
        x["Trailing PE"] = np.nan
    if "Forward PE" not in x.columns:
        x["Forward PE"] = np.nan
    x["PE Used"] = x["Trailing PE"]
    x.loc[x["PE Used"].isna(), "PE Used"] = x.loc[x["PE Used"].isna(), "Forward PE"]
    return weighted_harmonic_from_column(x, "PE Used")


def weighted_forward_pe(df):
    return weighted_harmonic_from_column(df, "Forward PE")
