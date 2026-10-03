"""Classify IB cash transactions into tax categories and convert them to AUD.

Categories: div (dividend), roc (return of capital paid as a "dividend"), pil (payment in lieu
of a dividend), wht (withholding tax), int_recv, int_paid, fee. Anything else (deposits,
withdrawals, adjustments) is returned separately so the report can list what was ignored.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .flex import fy_of
from .fx import RateTable

INCOME_COLS = ["fy", "date", "cat", "source", "fee_scope", "type", "symbol", "conid", "isin",
               "country", "cur", "amount", "fx", "aud", "aud_ib", "description", "tid"]


def categorise(ib_type: str, dividend_type: str = "") -> str | None:
    t = ib_type.lower()
    if "withholding" in t:
        return "wht"
    if "payment in lieu" in t:
        return "pil"
    if "dividend" in t:
        return "roc" if "return of capital" in dividend_type.lower() else "div"
    if "interest" in t and "received" in t:
        return "int_recv"
    if "interest" in t and "paid" in t:
        return "int_paid"
    if "fee" in t:
        return "fee"
    return None


def build_income(cash: pd.DataFrame, rates: RateTable, interest_source: str,
                 max_gap_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (income rows in AUD, ignored rows)."""
    if cash.empty:
        return pd.DataFrame(columns=INCOME_COLS), cash
    cats = [categorise(t, d) for t, d in zip(cash["type"], cash["divtype"], strict=True)]
    df = cash.assign(cat=cats)
    ignored = df[df["cat"].isna()].drop(columns="cat")
    df = df[df["cat"].notna()]
    if df["date"].isna().any():
        raise ValueError(f"{int(df['date'].isna().sum())} cash transactions have no parseable date")

    country = df["isin"].str[:2].str.upper()
    is_int = df["cat"].isin(["int_recv", "int_paid"])
    source = np.where(country == "AU", "au", "foreign")
    source = np.where(is_int, interest_source, source)
    source = np.where(df["cat"] == "fee", "n/a", source)
    scope = np.where(df["cat"] != "fee", "",
                     np.where(country == "", "unallocated",
                              np.where(country == "AU", "au", "foreign")))
    fx = rates.rates_for(df["cur"], df["date"], max_gap_days)
    out = df.assign(
        country=country, source=source, fee_scope=scope, fx=fx,
        aud=df["amount"] / fx, aud_ib=df["amount"] * df["fx_ib"],
        fy=fy_of(df["date"]),
    )
    return out[INCOME_COLS].sort_values(["fy", "date"]).reset_index(drop=True), ignored
