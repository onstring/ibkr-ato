"""Corporate actions as typed ledger events.

IB's Corporate Actions section says what happened, not how the ATO treats it. ``corp-actions``
drafts ``corporate_actions.csv`` from IB's rows: plain splits are filled in and applied
(status ``auto``); everything with a tax choice (spin-off, takeover, share-for-share) is
drafted as ``needs_manual`` with a note. You edit the row, set ``confirmed``, and the ledger
applies it. Any ``needs_manual`` row keeps the report in DRAFT.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from .flex import to_date
from .fx import RateTable

CSV_COLS = ["action_id", "status", "type", "date", "conid", "symbol", "to_conid", "to_symbol",
            "quantity", "ratio", "amount", "currency", "acq_date", "note", "ib_description"]
TYPES = ("split", "remove_for_cash", "add_lots", "rollover")
APPLIED = frozenset({"auto", "confirmed"})
STATUSES = APPLIED | {"needs_manual", "ignored"}


class CorpActionInvalid(ValueError):
    """A row in corporate_actions.csv cannot be applied."""


@dataclass(frozen=True)
class Split:
    action_id: str
    date: date
    conid: str
    symbol: str
    ratio: float


@dataclass(frozen=True)
class RemoveForCash:
    action_id: str
    date: date
    conid: str
    symbol: str
    quantity: float | None  # None = whole holding
    proceeds_fc: float
    currency: str
    fx: float


@dataclass(frozen=True)
class AddLots:
    action_id: str
    date: date
    conid: str
    symbol: str
    quantity: float
    cost_fc: float
    currency: str
    fx: float
    acq_date: date


@dataclass(frozen=True)
class Rollover:
    action_id: str
    date: date
    conid: str
    symbol: str
    to_conid: str
    to_symbol: str
    ratio: float
    quantity: float | None  # None = whole holding


@dataclass(frozen=True)
class CostReduction:
    """Return of capital applied to the cost base of shares held (CGT event G1)."""

    action_id: str
    date: date
    conid: str
    symbol: str
    amount_fc: float
    amount_aud: float


CorpEvent = Split | RemoveForCash | AddLots | Rollover | CostReduction
EVENT_ORDER = {Split: 0, AddLots: 1, Rollover: 2, CostReduction: 3, RemoveForCash: 4}


@dataclass(frozen=True)
class CorpActionsFile:
    events: tuple[CorpEvent, ...]
    pending: pd.DataFrame
    rows: pd.DataFrame


# ------------------------------------------------------------------ loading


def read_csv(path: Path | None) -> pd.DataFrame:
    if path is None or not Path(path).exists():
        return pd.DataFrame(columns=CSV_COLS)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower() for c in df.columns]
    missing = set(CSV_COLS) - set(df.columns)
    if missing:
        raise CorpActionInvalid(f"{path}: missing columns {sorted(missing)}")
    return df[CSV_COLS].apply(lambda s: s.str.strip())


def _float(row: pd.Series, col: str, *, required: bool, positive: bool = True) -> float | None:
    raw = row[col]
    if raw == "":
        if required:
            raise CorpActionInvalid(f"{row['action_id']}: '{col}' is required")
        return None
    try:
        v = float(raw.replace(",", ""))
    except ValueError as e:
        raise CorpActionInvalid(f"{row['action_id']}: '{col}' is not a number: {raw!r}") from e
    if not math.isfinite(v) or (positive and v <= 0):
        raise CorpActionInvalid(f"{row['action_id']}: '{col}' must be greater than 0: {raw!r}")
    return v


def _date(row: pd.Series, col: str) -> date:
    d = to_date(pd.Series([row[col]])).iloc[0]
    if pd.isna(d):
        raise CorpActionInvalid(f"{row['action_id']}: '{col}' is not a date: {row[col]!r}")
    return d.date()


def _fx(rates: RateTable, cur: str, d: date, gap: int) -> float:
    return float(rates.rates_for(pd.Series([cur]), pd.Series([pd.Timestamp(d)]), gap).iloc[0])


def to_event(row: pd.Series, rates: RateTable, gap: int) -> CorpEvent:
    kind, aid = row["type"], row["action_id"]
    if not row["conid"]:
        raise CorpActionInvalid(f"{aid}: 'conid' is required")
    d = _date(row, "date")
    common = {"action_id": aid, "date": d, "conid": row["conid"], "symbol": row["symbol"]}
    if kind == "split":
        return Split(**common, ratio=_float(row, "ratio", required=True))
    if kind == "rollover":
        if not row["to_conid"]:
            raise CorpActionInvalid(f"{aid}: 'to_conid' is required for rollover")
        ratio = _float(row, "ratio", required=False) or 1.0
        return Rollover(**common, to_conid=row["to_conid"], to_symbol=row["to_symbol"],
                        ratio=ratio, quantity=_float(row, "quantity", required=False))
    cur = row["currency"].upper()
    if not cur:
        raise CorpActionInvalid(f"{aid}: 'currency' is required for {kind}")
    amount = _float(row, "amount", required=True, positive=False)
    if amount < 0:
        raise CorpActionInvalid(f"{aid}: 'amount' must be 0 or more")
    fx = _fx(rates, cur, d, gap)
    if kind == "remove_for_cash":
        return RemoveForCash(**common, quantity=_float(row, "quantity", required=False),
                             proceeds_fc=amount, currency=cur, fx=fx)
    if kind == "add_lots":
        acq = _date(row, "acq_date") if row["acq_date"] else d
        return AddLots(**common, quantity=_float(row, "quantity", required=True),
                       cost_fc=amount, currency=cur, fx=fx, acq_date=acq)
    raise CorpActionInvalid(f"{aid}: unknown type {kind!r}; expected one of {TYPES}")


def load_corporate_actions(path: Path | None, rates: RateTable, gap: int) -> CorpActionsFile:
    rows = read_csv(path)
    bad = sorted(set(rows["status"]) - STATUSES)
    if bad:
        raise CorpActionInvalid(f"unknown status {bad}; expected one of {sorted(STATUSES)}")
    applied = rows[rows["status"].isin(APPLIED)]
    events = tuple(to_event(r, rates, gap) for _, r in applied.iterrows())
    return CorpActionsFile(events, rows[rows["status"] == "needs_manual"], rows)


# ------------------------------------------------------------------ drafting from IB rows

_SPLIT = re.compile(r"SPLIT\s+(\d+(?:\.\d+)?)\s+FOR\s+(\d+(?:\.\d+)?)", re.I)
_SPINOFF = re.compile(r"SPINOFF\s+(\d+(?:\.\d+)?)\s+FOR\s+(\d+(?:\.\d+)?)", re.I)
_CASH = re.compile(r"MERGED\(Acquisition\)\s+FOR\s+([A-Z]{3})\s+([\d.]+)\s+PER SHARE", re.I)
_SCRIP = re.compile(r"MERGED\((.+?)\)\s+WITH\s+([A-Z0-9]{12})\s+(\d+(?:\.\d+)?)\s+FOR\s+"
                    r"(\d+(?:\.\d+)?)", re.I)

NOTE_SPINOFF = ("Decide: (a) assessable dividend in specie at market value (item 20); "
                "(b) consideration for the parent's disposal: amount = quantity x market value, "
                "and add it to the parent's remove_for_cash amount; (c) demerger rollover "
                "(not automated: see docs/corporate-actions.md).")
NOTE_CASH = ("IB proceeds are cash only. Capital proceeds = money + market value of any "
             "property received. Set amount accordingly, then confirmed.")
NOTE_SCRIP = ("Scrip-for-scrip rollover if eligible and chosen (keeps cost base and acquisition "
              "date). Otherwise replace with remove_for_cash + add_lots at market value.")
NOTE_SPLIT = "Share split/consolidation: no CGT event; cost base spread over the new quantity."


def parse_action(type_code: str, description: str) -> tuple[str, dict[str, object]]:
    """Return (draft type, fields) for one IB corporate-action description."""
    if m := _SPLIT.search(description):
        return "split", {"ratio": float(m[1]) / float(m[2])}
    if m := _SPINOFF.search(description):
        return "spinoff", {"ratio": float(m[1]) / float(m[2])}
    if m := _CASH.search(description):
        return "cash", {"currency": m[1].upper(), "per_share": float(m[2])}
    if m := _SCRIP.search(description):
        return "scrip", {"to_isin": m[2], "ratio": float(m[3]) / float(m[4])}
    return "unknown", {}


def _row(ib: pd.Series, **fields: object) -> dict[str, object]:
    base = dict.fromkeys(CSV_COLS, "")
    key = ib["action_id"] or ib["tid"]
    base.update(action_id=f"ib:{key}:{ib['conid']}", date=ib["date"].date().isoformat(),
                conid=ib["conid"], symbol=ib["symbol"], currency=ib["cur"],
                ib_description=ib["description"], status="needs_manual")
    base.update(fields)
    return base


def _fmt(v: float) -> str:
    return f"{v:.10g}"


def draft_rows(ib_rows: pd.DataFrame) -> list[dict[str, object]]:
    drafts, used = [], set()
    for i, ib in ib_rows.iterrows():
        if i in used:
            continue
        kind, f = parse_action(ib["type"], ib["description"])
        if kind == "split":
            drafts.append(_row(ib, type="split", ratio=_fmt(f["ratio"]), status="auto",
                               note=NOTE_SPLIT))
        elif kind == "spinoff":
            drafts.append(_row(ib, type="add_lots", quantity=_fmt(abs(ib["qty"])),
                               note=NOTE_SPINOFF))
        elif kind == "cash":
            drafts.append(_row(ib, type="remove_for_cash", quantity=_fmt(abs(ib["qty"])),
                               amount=_fmt(abs(ib["proceeds"])), currency=f["currency"],
                               note=NOTE_CASH))
        elif kind == "scrip":
            # IB writes one row per side with the same ActionID; descriptions differ only in
            # the trailing "(symbol, name, isin)".
            same = (ib_rows["action_id"] == ib["action_id"]) if ib["action_id"] else (
                ib_rows["description"].str.rsplit(" (", n=1).str[0]
                == ib["description"].rsplit(" (", 1)[0])
            mates = ib_rows[same & (ib_rows["date"] == ib["date"]) & (ib_rows.index != i)]
            used.update(mates.index)
            pair = pd.concat([ib.to_frame().T, mates])
            src = pair[pair["qty"].astype(float) < 0]
            dst = pair[pair["qty"].astype(float) > 0]
            if src.empty or dst.empty:
                drafts.append(_row(ib, note="Unpaired share-for-share row; decide treatment."))
                continue
            s, t = src.iloc[0], dst.iloc[0]
            drafts.append(_row(s, type="rollover", to_conid=t["conid"], to_symbol=t["symbol"],
                               ratio=_fmt(f["ratio"]), quantity=_fmt(abs(float(s["qty"]))),
                               note=NOTE_SCRIP))
        else:
            drafts.append(_row(ib, note="Unrecognised corporate action; decide treatment."))
    return drafts


def draft_corporate_actions(ib_rows: pd.DataFrame, existing: pd.DataFrame) -> pd.DataFrame:
    """Existing rows unchanged, plus drafts for IB actions not yet in the file."""
    known = set(existing["action_id"]) if not existing.empty else set()
    new = [r for r in draft_rows(ib_rows) if r["action_id"] not in known]
    frames = [f for f in (existing, pd.DataFrame(new, columns=CSV_COLS)) if not f.empty]
    if not frames:
        return pd.DataFrame(columns=CSV_COLS)
    return pd.concat(frames, ignore_index=True)[CSV_COLS]

