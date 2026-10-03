"""Read IB Flex Query CSV exports into named sections with fixed column schemas.

A Flex CSV holds several tables one after another. Each table starts with a header row; we
detect it when at least three cells are known Flex column names. Tables are classified by the
columns they carry, not by position, so a query with fewer sections still works.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

SECTIONS = ("nav", "funds", "positions", "taxes", "corp_actions", "transfers", "trades", "cash")

_KNOWN = [
    "ClientAccountID", "AccountId", "CurrencyPrimary", "Currency", "AssetClass", "Symbol",
    "Description", "Conid", "ISIN", "TradeID", "TransactionID", "TradeDate", "DateTime",
    "Quantity", "TradePrice", "Proceeds", "IBCommission", "Type", "Amount", "LevelOfDetail",
    "ReportDate", "SettleDate", "ActionID", "FromDate", "ToDate",
]
_MARKERS = {"BOF", "EOF", "BOA", "EOA", "BOS", "EOS", "MSG"}


class FlexFormatError(ValueError):
    """A Flex file contains a table we cannot classify."""


def norm(name: object) -> str:
    """Normalise a column name: lowercase, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


KNOWN_COLS = frozenset(norm(c) for c in _KNOWN)


def classify(columns: Iterable[str]) -> str:
    c = set(columns)
    rules = [
        ("nav", {"fromdate", "endingvalue"}),
        ("funds", {"activitycode"}),
        ("positions", {"costbasismoney"}),
        ("taxes", {"taxdescription"}),
        ("corp_actions", {"actiondescription"}),
        ("transfers", {"direction"}),
        ("trades", {"tradeprice", "quantity"}),
        ("cash", {"type", "amount"}),
    ]
    for name, needed in rules:
        if needed <= c:
            return name
    raise FlexFormatError(f"unrecognised Flex table with columns: {sorted(c)[:12]}...")


@dataclass(frozen=True)
class FlexFile:
    path: Path
    sections: dict[str, pd.DataFrame] = field(repr=False)
    from_date: date | None = None
    to_date: date | None = None


def _tables(path: Path) -> list[tuple[list[str], list[list[str]]]]:
    tables: list[tuple[list[str], list[list[str]]]] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if not row or not any(c.strip() for c in row):
                continue
            tag = row[0].strip().upper()
            if tag in _MARKERS:
                continue
            if tag in ("HEADER", "DATA"):
                row = row[2:]
            cells = [norm(c) for c in row]
            if tag != "DATA" and sum(c in KNOWN_COLS for c in cells) >= 3:
                tables.append((cells, []))
            elif tables:
                header = tables[-1][0]
                tables[-1][1].append([c.strip() for c in (row + [""] * len(header))[: len(header)]])
    return tables


def _frame(header: list[str], rows: list[list[str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=header, dtype=object)
    return df.loc[:, ~pd.Index(df.columns).duplicated()]


def read_flex(path: Path) -> FlexFile:
    sections: dict[str, list[pd.DataFrame]] = {}
    for header, rows in _tables(Path(path)):
        sections.setdefault(classify(header), []).append(_frame(header, rows))
    merged = {k: pd.concat(v, ignore_index=True) for k, v in sections.items()}
    from_d = to_d = None
    nav = merged.get("nav")
    if nav is not None and not nav.empty:
        from_d = _one_date(nav["fromdate"].iloc[0])
        to_d = _one_date(nav["todate"].iloc[0])
    return FlexFile(Path(path), merged, from_d, to_d)


def load_flex_dir(flex_dir: Path) -> tuple[FlexFile, ...]:
    paths = sorted(Path(flex_dir).glob("*.csv"))
    if not paths:
        raise FileNotFoundError(f"no Flex CSV files in {flex_dir}")
    return tuple(read_flex(p) for p in paths)


# ------------------------------------------------------------------ column helpers


def section(files: Sequence[FlexFile], name: str) -> pd.DataFrame:
    """All rows of one section across files, missing cells as empty strings."""
    frames = [f.sections[name] for f in files if name in f.sections]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).fillna("")


def get(df: pd.DataFrame, *names: str) -> pd.Series:
    """First present column among names, as stripped strings."""
    for n in names:
        k = norm(n)
        if k in df.columns:
            return df[k].fillna("").astype(str).str.strip()
    return pd.Series([""] * len(df), index=df.index, dtype=object)


def num(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


def to_date(s: pd.Series) -> pd.Series:
    ext = s.astype(str).str.extract(r"(\d{4})-?(\d{2})-?(\d{2})")
    return pd.to_datetime(ext[0] + ext[1] + ext[2], format="%Y%m%d", errors="coerce")


def _one_date(value: str) -> date | None:
    d = to_date(pd.Series([value])).iloc[0]
    return None if pd.isna(d) else d.date()


def fy_of(d: pd.Series) -> pd.Series:
    """Australian financial year: FY2026 runs 1 Jul 2025 to 30 Jun 2026."""
    return (d.dt.year + (d.dt.month >= 7).astype(int)).astype("Int64")


def fy_of_date(d: date) -> int:
    return d.year + (1 if d.month >= 7 else 0)


def dedupe(df: pd.DataFrame, id_col: str) -> pd.DataFrame:
    """Drop rows repeated by overlapping exports.

    Identical rows collapse to one. The same ID with different values (IB re-issued a corrected
    row) is an error: picking either silently could give a wrong figure.
    """
    df = df.drop_duplicates()
    has = df[id_col] != ""
    clash = df[has & df[id_col].duplicated(keep=False)]
    if not clash.empty:
        ids = ", ".join(sorted(clash[id_col].unique())[:10])
        raise ValueError(f"overlapping Flex exports disagree for {id_col} {ids}; keep only the "
                         "latest export covering those dates in flex/")
    return df


# ------------------------------------------------------------------ normalised sections


def executions(files: Sequence[FlexFile]) -> pd.DataFrame:
    """Trade executions (STK and CASH), deduplicated on TradeID."""
    tr = section(files, "trades")
    if tr.empty:
        return pd.DataFrame(columns=EXEC_COLS)
    raw_dt, td = get(tr, "DateTime"), get(tr, "TradeDate")
    df = pd.DataFrame({
        "tid": get(tr, "TradeID"),
        "level": get(tr, "LevelOfDetail").str.upper(),
        "asset": get(tr, "AssetClass").str.upper(),
        "symbol": get(tr, "Symbol"),
        "conid": get(tr, "Conid"),
        "isin": get(tr, "ISIN"),
        "cur": get(tr, "CurrencyPrimary", "Currency").str.upper(),
        "commcur": get(tr, "IBCommissionCurrency").str.upper(),
        "date": to_date(td.where(td != "", raw_dt)),
        "sortkey": raw_dt.where(raw_dt != "", td),
        "qty": num(get(tr, "Quantity")),
        "price": num(get(tr, "TradePrice")),
        "mult": num(get(tr, "Multiplier")).replace(0, 1),
        "proceeds": num(get(tr, "Proceeds")),
        "comm": num(get(tr, "IBCommission")),
        "taxes": num(get(tr, "Taxes")),
        "fx_ib": num(get(tr, "FXRateToBase")),
    })
    df = df[df["level"].isin(["EXECUTION", ""])]
    return dedupe(df, "tid")[EXEC_COLS].reset_index(drop=True)


EXEC_COLS = ["tid", "level", "asset", "symbol", "conid", "isin", "cur", "commcur", "date",
             "sortkey", "qty", "price", "mult", "proceeds", "comm", "taxes", "fx_ib"]


def closed_lots(files: Sequence[FlexFile]) -> pd.DataFrame:
    """IB's own FIFO closed lots: one row per (disposal, opening lot)."""
    tr = section(files, "trades")
    cols = ["conid", "symbol", "cur", "disp_date", "acq_date", "qty", "cost_fc", "pnl_fc",
            "ib_term"]
    if tr.empty:
        return pd.DataFrame(columns=cols)
    tr = tr[get(tr, "LevelOfDetail").str.upper() == "CLOSED_LOT"]
    df = pd.DataFrame({
        "conid": get(tr, "Conid"),
        "symbol": get(tr, "Symbol"),
        "cur": get(tr, "CurrencyPrimary").str.upper(),
        "disp_date": to_date(get(tr, "TradeDate")),
        "acq_date": to_date(get(tr, "OpenDateTime")),
        "qty": num(get(tr, "Quantity")),
        "cost_fc": num(get(tr, "CostBasis")),
        "pnl_fc": num(get(tr, "FifoPnlRealized")),
        "ib_term": get(tr, "Notes/Codes").str.extract(r"\b(LT|ST)\b", expand=False).fillna(""),
    })
    return df.drop_duplicates().reset_index(drop=True)[cols]


def open_positions(files: Sequence[FlexFile]) -> pd.DataFrame:
    """IB positions at each export's end date (stocks only)."""
    rows = []
    for f in files:
        pos = f.sections.get("positions")
        if pos is None or pos.empty:
            continue
        pos = pos.fillna("")
        pos = pos[get(pos, "AssetClass").str.upper() == "STK"]
        as_of = to_date(get(pos, "ReportDate"))
        rows.append(pd.DataFrame({
            "as_of": as_of,
            "conid": get(pos, "Conid"),
            "symbol": get(pos, "Symbol"),
            "cur": get(pos, "CurrencyPrimary").str.upper(),
            "qty": num(get(pos, "Quantity")),
            "cost_fc": num(get(pos, "CostBasisMoney")),
        }))
    if not rows:
        return pd.DataFrame(columns=["as_of", "conid", "symbol", "cur", "qty", "cost_fc"])
    return pd.concat(rows, ignore_index=True).drop_duplicates()


NAV_FIELDS = ["Dividends", "WithholdingTax", "Interest", "OtherFees", "Commissions",
              "ForexCommissions", "TransactionTax", "CorporateActionProceeds"]


def nav_totals(files: Sequence[FlexFile]) -> pd.DataFrame:
    """Change in NAV per export (base currency), keyed by FY of the end date."""
    rows = []
    for f in files:
        nav = f.sections.get("nav")
        if nav is None or nav.empty or f.to_date is None:
            continue
        nav = nav.fillna("")
        row = {"fy": fy_of_date(f.to_date), "from_date": f.from_date, "to_date": f.to_date}
        for name in NAV_FIELDS:
            row[norm(name)] = float(num(get(nav, name)).sum())
        rows.append(row)
    return pd.DataFrame(rows)


def cash_rows(files: Sequence[FlexFile]) -> pd.DataFrame:
    """Cash transactions (dividends, withholding, interest, fees...), deduplicated."""
    ct = section(files, "cash")
    cols = ["tid", "type", "divtype", "cur", "symbol", "conid", "isin", "description",
            "amount", "fx_ib", "date"]
    if ct.empty:
        return pd.DataFrame(columns=cols)
    dt, sd, rd = get(ct, "DateTime"), get(ct, "SettleDate"), get(ct, "ReportDate")
    df = pd.DataFrame({
        "tid": get(ct, "TransactionID"),
        "type": get(ct, "Type"),
        "divtype": get(ct, "DividendType"),
        "cur": get(ct, "CurrencyPrimary", "Currency").str.upper(),
        "symbol": get(ct, "Symbol"),
        "conid": get(ct, "Conid"),
        "isin": get(ct, "ISIN"),
        "description": get(ct, "Description"),
        "amount": num(get(ct, "Amount")),
        "fx_ib": num(get(ct, "FXRateToBase")),
        "date": to_date(dt.where(dt != "", sd.where(sd != "", rd))),
    })
    return dedupe(df, "tid")[cols].reset_index(drop=True)


def corp_action_rows(files: Sequence[FlexFile]) -> pd.DataFrame:
    ca = section(files, "corp_actions")
    cols = ["action_id", "tid", "date", "type", "symbol", "conid", "isin", "cur", "qty",
            "proceeds", "pnl", "description"]
    if ca.empty:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame({
        "action_id": get(ca, "ActionID"),
        "tid": get(ca, "TransactionID"),
        "date": to_date(get(ca, "DateTime", "ReportDate")),
        "type": get(ca, "Type").str.upper(),
        "symbol": get(ca, "Symbol"),
        "conid": get(ca, "Conid"),
        "isin": get(ca, "ISIN"),
        "cur": get(ca, "CurrencyPrimary").str.upper(),
        "qty": num(get(ca, "Quantity")),
        "proceeds": num(get(ca, "Proceeds")),
        "pnl": num(get(ca, "FifoPnlRealized")),
        "description": get(ca, "ActionDescription", "Description"),
    })
    return dedupe(df, "tid")[cols].reset_index(drop=True)
