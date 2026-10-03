"""FIFO parcel ledger in AUD.

Every buy opens a lot; every sell (or cash takeover) consumes lots oldest first and produces
CGT parcels. Each lot keeps its cost per share in AUD (converted on the acquisition date) and in
the trade currency. Brokerage and transaction taxes on a buy are in the cost; on a sell they
reduce the proceeds (same gain as treating them as a cost-base element).

The book is a ``dict[str, deque[_Lot]]`` mutated in place. FIFO consumption is sequential by
nature; copying the book per trade would cost clarity for nothing. The mutation stays inside
this module: ``run_ledger`` returns new DataFrames and tuples only.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd

from .corporate_actions import (
    EVENT_ORDER,
    AddLots,
    CorpEvent,
    CostReduction,
    RemoveForCash,
    Rollover,
    Split,
)
from .flex import fy_of, get, norm, num, to_date
from .fx import RateTable

EPS = 1e-9
PARCEL_COLS = ["symbol", "isin", "currency", "acq_date", "disp_date", "quantity", "cost_fc",
               "proceeds_fc", "fx_acq", "fx_disp", "cost_aud", "proceeds_aud", "gain_aud",
               "discount_eligible", "acq_source", "disp_source", "conid", "fy"]
HOLDING_COLS = ["symbol", "isin", "currency", "acq_date", "quantity", "cost_fc", "cost_aud",
                "source", "conid"]


class Unresolved(NamedTuple):
    kind: str
    conid: str
    symbol: str
    date: str
    quantity: float
    message: str


@dataclass(frozen=True)
class LedgerResult:
    parcels: pd.DataFrame
    holdings: pd.DataFrame
    unresolved: tuple[Unresolved, ...]


@dataclass
class _Lot:
    acq: pd.Timestamp
    qty: float
    aud_ps: float
    fc_ps: float
    fx: float
    src: str


def discount_eligible(acq: pd.Timestamp, disp: pd.Timestamp) -> bool:
    """CGT discount needs at least 12 months' ownership, excluding acquisition and disposal days.

    So disposal must fall after the first anniversary: bought 2 Feb 2024, eligible from 3 Feb 2025.
    """
    return bool(pd.Timestamp(disp) > pd.Timestamp(acq) + pd.DateOffset(years=1))


# ------------------------------------------------------------------ inputs


def prepare_trades(exe: pd.DataFrame, rates: RateTable,
                   gap: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return (stock trades with AUD amounts, FX conversions, excluded asset rows)."""
    if exe["date"].isna().any():
        bad = exe[exe["date"].isna()]
        raise ValueError(f"{len(bad)} trades have no parseable date (set Flex dates to yyyyMMdd)")
    fx_trades = exe[exe["asset"] == "CASH"]
    excluded = exe[~exe["asset"].isin(["STK", "CASH", ""])]
    df = exe[exe["asset"].isin(["STK", ""])]
    key = df["conid"].where(df["conid"] != "", df["symbol"])
    commcur = df["commcur"].where(df["commcur"] != "", df["cur"])
    zero = (df["proceeds"] == 0) & (df["qty"] != 0)
    proceeds = df["proceeds"].where(~zero, -df["qty"] * df["price"] * df["mult"])
    fx = rates.rates_for(df["cur"], df["date"], gap)
    fx_comm = rates.rates_for(commcur, df["date"], gap)
    comm_fc = np.where(commcur == df["cur"], df["comm"], df["comm"] / fx_comm * fx)
    out = df.assign(
        key=key, commcur=commcur, proceeds=proceeds, fx=fx, fx_comm=fx_comm, comm_fc=comm_fc,
        net_aud=(proceeds + df["taxes"]) / fx + df["comm"] / fx_comm,
        net_fc=proceeds + df["taxes"] + comm_fc,
    )
    return out.reset_index(drop=True), fx_trades.reset_index(drop=True), excluded


def load_opening_lots(path: Path | None, rates: RateTable, gap: int,
                      sym2key: dict[str, str]) -> pd.DataFrame:
    """Lots acquired outside the IB history: acq_date,symbol,conid,quantity,cost_total,currency."""
    cols = ["date", "key", "symbol", "qty", "cost", "cur", "fx", "cost_aud"]
    if path is None or not Path(path).exists():
        return pd.DataFrame(columns=cols)
    o = pd.read_csv(path, dtype=str, keep_default_na=False)
    o.columns = [norm(c) for c in o.columns]
    d = to_date(get(o, "acqdate"))
    if d.isna().any():
        raise ValueError(f"{path}: every row needs a valid acq_date")
    sym, conid = get(o, "symbol"), get(o, "conid")
    cur = get(o, "currency").str.upper()
    qty, cost = num(get(o, "quantity")), num(get(o, "costtotal"))
    if (qty <= 0).any():
        raise ValueError(f"{path}: quantity must be greater than 0")
    fx = rates.rates_for(cur, d, gap)
    keys = [c or sym2key.get(s, s) for c, s in zip(conid, sym, strict=True)]
    return pd.DataFrame({"date": d, "key": keys, "symbol": sym, "qty": qty, "cost": cost,
                         "cur": cur, "fx": fx, "cost_aud": cost / fx})


# ------------------------------------------------------------------ the book


class _Book:
    def __init__(self) -> None:
        self.lots: dict[str, deque[_Lot]] = defaultdict(deque)
        self.info: dict[str, tuple[str, str, str]] = {}
        self.parcels: list[dict] = []
        self.unresolved: list[Unresolved] = []

    def held(self, key: str) -> float:
        return sum(lot.qty for lot in self.lots[key])

    def flag(self, kind: str, key: str, when: pd.Timestamp, qty: float, msg: str) -> None:
        sym = self.info.get(key, (key, "", ""))[0]
        self.unresolved.append(Unresolved(kind, key, sym, str(when.date()), qty, msg))

    def dispose(self, key: str, qty: float, when: pd.Timestamp, proc_aud: float,
                proc_fc: float, fx: float, source: str) -> None:
        sym, isin, cur = self.info.get(key, (key, "", ""))
        remain, p_aud_ps, p_fc_ps = qty, proc_aud / qty, proc_fc / qty
        while remain > EPS and self.lots[key]:
            lot = self.lots[key][0]
            take = min(remain, lot.qty)
            cost_aud, proc = take * lot.aud_ps, take * p_aud_ps
            self.parcels.append({
                "symbol": sym, "isin": isin, "currency": cur, "acq_date": lot.acq.date(),
                "disp_date": when.date(), "quantity": take, "cost_fc": take * lot.fc_ps,
                "proceeds_fc": take * p_fc_ps, "fx_acq": lot.fx, "fx_disp": fx,
                "cost_aud": cost_aud, "proceeds_aud": proc, "gain_aud": proc - cost_aud,
                "discount_eligible": discount_eligible(lot.acq, when),
                "acq_source": lot.src, "disp_source": source, "conid": key,
            })
            lot.qty -= take
            remain -= take
            if lot.qty <= EPS:
                self.lots[key].popleft()
        if remain > EPS:
            self.flag("unmatched_sell", key, when, remain,
                      "disposal exceeds known holdings (missing history, transfer-in or short "
                      "sale); add the lots to opening_lots.csv")

    # -- events

    def trade(self, r) -> None:
        self.info[r.key] = (r.symbol, r.isin, r.cur)
        if r.qty > EPS:
            self.lots[r.key].append(_Lot(r.date, r.qty, -r.net_aud / r.qty, -r.net_fc / r.qty,
                                         r.fx, f"trade {r.tid}"))
        elif r.qty < -EPS:
            self.dispose(r.key, -r.qty, r.date, r.net_aud, r.net_fc, r.fx, r.tid)

    def opening(self, r) -> None:
        self.lots[r.key].append(_Lot(r.date, r.qty, r.cost_aud / r.qty, r.cost / r.qty, r.fx,
                                     "opening_lots"))
        self.info.setdefault(r.key, (r.symbol, "", r.cur))

    def event(self, e: CorpEvent, when: pd.Timestamp) -> None:
        if isinstance(e, Split):
            for lot in self.lots[e.conid]:
                lot.qty, lot.aud_ps, lot.fc_ps = lot.qty * e.ratio, lot.aud_ps / e.ratio, \
                    lot.fc_ps / e.ratio
        elif isinstance(e, AddLots):
            acq = pd.Timestamp(e.acq_date)
            self.lots[e.conid].append(_Lot(acq, e.quantity, e.cost_fc / e.fx / e.quantity,
                                           e.cost_fc / e.quantity, e.fx, e.action_id))
            self.info.setdefault(e.conid, (e.symbol, "", e.currency))
        elif isinstance(e, RemoveForCash):
            qty = e.quantity if e.quantity is not None else self.held(e.conid)
            if qty <= EPS:
                self.flag("event_no_holding", e.conid, when, 0.0, f"{e.action_id}: nothing held")
                return
            self.dispose(e.conid, qty, when, e.proceeds_fc / e.fx, e.proceeds_fc, e.fx,
                         e.action_id)
        elif isinstance(e, Rollover):
            self.rollover(e, when)
        elif isinstance(e, CostReduction):
            self.reduce_cost(e, when)

    def rollover(self, e: Rollover, when: pd.Timestamp) -> None:
        src, dst = self.lots[e.conid], self.lots[e.to_conid]
        remain = e.quantity if e.quantity is not None else self.held(e.conid)
        _, _, cur = self.info.get(e.conid, (e.symbol, "", ""))
        self.info.setdefault(e.to_conid, (e.to_symbol or e.to_conid, "", cur))
        while remain > EPS and src:
            lot = src[0]
            take = min(remain, lot.qty)
            dst.append(_Lot(lot.acq, take * e.ratio, lot.aud_ps / e.ratio, lot.fc_ps / e.ratio,
                            lot.fx, f"{lot.src} > {e.action_id}"))
            lot.qty -= take
            remain -= take
            if lot.qty <= EPS:
                src.popleft()
        if remain > EPS:
            self.flag("event_exceeds_holding", e.conid, when, remain,
                      f"{e.action_id}: rollover quantity exceeds holding")

    def reduce_cost(self, e: CostReduction, when: pd.Timestamp) -> None:
        total = self.held(e.conid)
        if total <= EPS:
            self.flag("event_no_holding", e.conid, when, 0.0,
                      f"{e.action_id}: return of capital with no shares held")
            return
        for lot in self.lots[e.conid]:
            lot.aud_ps -= e.amount_aud / total
            lot.fc_ps -= e.amount_fc / total
            if lot.aud_ps < -EPS:
                self.flag("roc_exceeds_cost", e.conid, when, lot.qty,
                          f"{e.action_id}: return of capital exceeds cost base; the excess is a "
                          "capital gain (CGT event G1) to add manually")
                lot.aud_ps = lot.fc_ps = 0.0


# ------------------------------------------------------------------ driver


def _events(trades: pd.DataFrame, events: tuple[CorpEvent, ...], opening: pd.DataFrame):
    rows = [((r.date, 0, 0, "", 0), "open", r) for r in opening.itertuples()]
    rows += [((pd.Timestamp(e.date), 1, EVENT_ORDER[type(e)], "", 0), "event", e)
             for e in events]
    rows += [((r.date, 2, 0, r.sortkey, 0 if r.qty > 0 else 1), "trade", r)
             for r in trades.itertuples()]
    return sorted(rows, key=lambda x: x[0])


def run_ledger(trades: pd.DataFrame, events: tuple[CorpEvent, ...] = (),
               opening: pd.DataFrame | None = None,
               until: pd.Timestamp | None = None) -> LedgerResult:
    book = _Book()
    opening = opening if opening is not None else pd.DataFrame(columns=["date"])
    for (when, *_), kind, item in _events(trades, events, opening):
        if until is not None and when > until:
            break
        if kind == "trade":
            book.trade(item)
        elif kind == "open":
            book.opening(item)
        else:
            book.event(item, when)
    return LedgerResult(_parcels_frame(book), _holdings_frame(book), tuple(book.unresolved))


def _parcels_frame(book: _Book) -> pd.DataFrame:
    p = pd.DataFrame(book.parcels, columns=PARCEL_COLS[:-1])
    p["fy"] = fy_of(pd.to_datetime(p["disp_date"])) if not p.empty else pd.Series(dtype="Int64")
    return p


def _holdings_frame(book: _Book) -> pd.DataFrame:
    rows = []
    for key, lots in book.lots.items():
        sym, isin, cur = book.info.get(key, (key, "", ""))
        rows += [{"symbol": sym, "isin": isin, "currency": cur, "acq_date": lot.acq.date(),
                  "quantity": lot.qty, "cost_fc": lot.qty * lot.fc_ps,
                  "cost_aud": lot.qty * lot.aud_ps, "source": lot.src, "conid": key}
                 for lot in lots if lot.qty > EPS]
    return pd.DataFrame(rows, columns=HOLDING_COLS)
