"""Check our figures against IB's own figures in the same export.

A. IB closed lots (FIFO) vs our CGT parcels, in the trade currency.
B. IB open positions at 30 June vs our holdings at that date.
C. IB Change in NAV totals vs our income and trade costs, at IB's own exchange rate
   (completeness), then our RBA-based AUD vs IB's AUD (exchange-rate source).

A ``mismatch`` keeps the report in DRAFT. ``info`` rows are expected differences to read.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from .flex import fy_of

QTY_TOL = 1e-6


def money_tol(v: float, floor: float = 0.05) -> float:
    return max(floor, abs(v) * 0.001)


@dataclass(frozen=True)
class ReconRow:
    section: str
    check: str
    scope: str
    ours: float | None
    ib: float | None
    status: str  # ok | mismatch | info | n/a
    note: str = ""

    @property
    def diff(self) -> float | None:
        return None if self.ours is None or self.ib is None else self.ours - self.ib


def _cmp(section, check, scope, ours, ib, tol, note="", soft=False) -> ReconRow:
    ok = abs(ours - ib) <= tol
    return ReconRow(section, check, scope, ours, ib,
                    "ok" if ok else ("info" if soft else "mismatch"), "" if ok else note)


# ------------------------------------------------------------------ A. closed lots


def _sum(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    return df.groupby(keys, as_index=False)[["qty", "cost_fc", "gain_fc"]].sum()


def _frames(ours: pd.DataFrame, ib: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    o = pd.DataFrame({"conid": ours["conid"], "symbol": ours["symbol"],
                      "cur": ours["currency"], "disp": pd.to_datetime(ours["disp_date"]),
                      "acq": pd.to_datetime(ours["acq_date"]), "qty": ours["quantity"],
                      "cost_fc": ours["cost_fc"],
                      "gain_fc": ours["proceeds_fc"] - ours["cost_fc"]})
    i = pd.DataFrame({"conid": ib["conid"], "symbol": ib["symbol"], "cur": ib["cur"],
                      "disp": ib["disp_date"], "acq": ib["acq_date"], "qty": ib["qty"],
                      "cost_fc": ib["cost_fc"], "gain_fc": ib["pnl_fc"]})
    return o, i


def _disposal_rows(o: pd.DataFrame, i: pd.DataFrame, names: dict) -> list[ReconRow]:
    rows = []
    keys = ["conid", "disp"]
    m = _sum(o, keys).merge(_sum(i, keys), on=keys, how="outer", suffixes=("", "_ib"))
    for r in m.itertuples():
        scope = f"{names.get(r.conid, r.conid)} sold {r.disp.date()}"
        if pd.isna(r.qty) or pd.isna(r.qty_ib):
            side = "IB only" if pd.isna(r.qty) else "ours only"
            rows.append(ReconRow("A closed lots", "disposal", scope,
                                 None if pd.isna(r.gain_fc) else r.gain_fc,
                                 None if pd.isna(r.gain_fc_ib) else r.gain_fc_ib, "mismatch",
                                 f"{side}: disposal missing on one side"))
            continue
        rows.append(_cmp("A closed lots", "quantity", scope, r.qty, r.qty_ib, QTY_TOL,
                         "quantity differs"))
        rows.append(_cmp("A closed lots", "cost (FC)", scope, r.cost_fc, r.cost_fc_ib,
                         money_tol(r.cost_fc_ib), "cost base differs"))
        rows.append(_cmp("A closed lots", "gain (FC)", scope, r.gain_fc, r.gain_fc_ib,
                         money_tol(r.gain_fc_ib), "realised gain differs"))
        lots_o = sorted(zip(o.loc[(o.conid == r.conid) & (o.disp == r.disp), "acq"].dt.date,
                            strict=True))
        lots_i = sorted(zip(i.loc[(i.conid == r.conid) & (i.disp == r.disp), "acq"].dt.date,
                            strict=True))
        if set(lots_o) != set(lots_i):
            rows.append(ReconRow("A closed lots", "lot dates", scope, None, None, "info",
                                 f"ours {sorted({d for d, in lots_o})} vs IB "
                                 f"{sorted({d for d, in lots_i})}: IB open times can fall on "
                                 "another calendar day (time zone) or IB used another lot "
                                 "method. Check the discount test near 12 months."))
    return rows


def reconcile_closed_lots(parcels: pd.DataFrame, closed: pd.DataFrame, fy: int,
                          event_ids: set[str]) -> list[ReconRow]:
    rows: list[ReconRow] = []
    p = parcels[parcels["fy"] == fy] if not parcels.empty else parcels
    is_event = p["disp_source"].isin(event_ids) if not p.empty else pd.Series(dtype=bool)
    for r in p[is_event].itertuples():
        rows.append(ReconRow("A closed lots", f"{r.symbol} sold {r.disp_date}", r.disp_source,
                             None, None, "n/a", "disposal by corporate action: IB reports it "
                             "under Corporate Actions, not as a closed lot"))
    ours = p[~is_event] if not p.empty else p
    ib = closed[fy_of(closed["disp_date"]) == fy] if not closed.empty else closed
    if ours.empty and ib.empty:
        return rows
    o, i = _frames(ours, ib)
    names = dict(zip(pd.concat([o, i])["conid"], pd.concat([o, i])["symbol"], strict=True))
    rows += _disposal_rows(o, i, names)
    for cur in sorted(set(o["cur"]) | set(i["cur"])):
        g_o, g_i = float(o.loc[o.cur == cur, "gain_fc"].sum()), float(
            i.loc[i.cur == cur, "gain_fc"].sum())
        rows.append(_cmp("A closed lots", "total realised gain (FC)", cur, g_o, g_i,
                         money_tol(g_i, 0.5), "FY total differs"))
    return rows


# ------------------------------------------------------------------ B. positions


def reconcile_positions(holdings_at: Callable[[pd.Timestamp], pd.DataFrame],
                        positions: pd.DataFrame, fy: int, event_conids: set[str],
                        roc_conids: set[str] = frozenset()) -> list[ReconRow]:
    if positions.empty:
        return []
    pos = positions[fy_of(positions["as_of"]) == fy]
    rows: list[ReconRow] = []
    for as_of, ib in pos.groupby("as_of"):
        h = holdings_at(as_of)
        ours = (h.groupby("conid", as_index=False)
                .agg(symbol=("symbol", "first"), qty=("quantity", "sum"),
                     cost_fc=("cost_fc", "sum")) if not h.empty
                else pd.DataFrame(columns=["conid", "symbol", "qty", "cost_fc"]))
        m = ours.merge(ib, on="conid", how="outer", suffixes=("", "_ib"))
        for r in m.itertuples():
            sym = r.symbol if isinstance(r.symbol, str) else r.symbol_ib
            scope = f"{sym} at {as_of.date()}"
            q, qi = (0.0 if pd.isna(r.qty) else r.qty), (0.0 if pd.isna(r.qty_ib) else r.qty_ib)
            rows.append(_cmp("B positions", "quantity", scope, q, qi, QTY_TOL,
                             "holding differs (missing trade, transfer or corporate action)"))
            if pd.isna(r.qty) or pd.isna(r.qty_ib):
                continue
            if r.conid in event_conids:
                note, soft = "cost set by a corporate action; IB allocates cost its own way", True
            elif r.conid in roc_conids:
                note, soft = ("IB reduces cost base by return of capital; with "
                              "return_of_capital = 'dividend' we do not"), True
            else:
                note, soft = "cost base differs", False
            rows.append(_cmp("B positions", "cost (FC)", scope, r.cost_fc, r.cost_fc_ib,
                             money_tol(r.cost_fc_ib), note, soft=soft))
    return rows


# ------------------------------------------------------------------ C. Change in NAV


def _base(amount: pd.Series, fx_ib: pd.Series) -> float:
    return float((amount * fx_ib).sum())


def reconcile_nav(income: pd.DataFrame, trades: pd.DataFrame, fx_trades: pd.DataFrame,
                  nav: pd.DataFrame, fy: int) -> list[ReconRow]:
    n = nav[nav["fy"] == fy] if not nav.empty else nav
    if n.empty:
        return [ReconRow("C NAV", "Change in NAV", f"FY{fy}", None, None, "n/a",
                         "no Change in NAV section for this FY")]
    n = n.sum(numeric_only=True)
    inc = income[income["fy"] == fy] if not income.empty else income
    stk = trades[fy_of(trades["date"]) == fy] if not trades.empty else trades
    fxt = fx_trades[fy_of(fx_trades["date"]) == fy] if not fx_trades.empty else fx_trades

    def cat(*cats: str, col: str = "aud_ib") -> float:
        return float(inc.loc[inc["cat"].isin(cats), col].sum()) if not inc.empty else 0.0

    def comm(df: pd.DataFrame) -> float:
        if df.empty:
            return 0.0
        rate = df["fx_ib"].where(df["commcur"] != "AUD", 1.0)
        return _base(df["comm"], rate)

    checks = [("dividends", ("div", "roc", "pil"), n["dividends"]),
              ("withholding tax", ("wht",), n["withholdingtax"]),
              ("interest", ("int_recv", "int_paid"), n["interest"]),
              ("other fees", ("fee",), n["otherfees"])]
    rows = [_cmp("C NAV", f"{name} (IB rate)", f"FY{fy}", cat(*cats), float(ib), 0.5,
                 "rows missing or duplicated") for name, cats, ib in checks]
    # IB's NAV "Commissions" includes commissions on currency conversions.
    rows.append(_cmp("C NAV", "commissions, stock + FX (IB rate)", f"FY{fy}",
                     comm(stk) + comm(fxt),
                     float(n["commissions"]) + float(n["forexcommissions"]), 0.5,
                     "commission rows differ"))
    taxes = _base(stk["taxes"], stk["fx_ib"]) if not stk.empty else 0.0
    rows.append(_cmp("C NAV", "transaction taxes (IB rate)", f"FY{fy}", taxes,
                     float(n["transactiontax"]), 0.5, "transaction tax rows differ"))
    for name, cats, _ in checks:
        ours, ib = cat(*cats, col="aud"), cat(*cats)
        if ib == 0 and ours == 0:
            continue
        big = abs(ours - ib) > 0.05 * abs(ib)
        rows.append(ReconRow("C NAV", f"{name}: RBA vs IB rate", f"FY{fy}", ours, ib,
                             "mismatch" if big else "info",
                             "over 5%: check exchange rates" if big else
                             "AUD at RBA rate (ours) vs IB's rate"))
    return rows


@dataclass(frozen=True)
class Reconciliation:
    fy: int
    rows: tuple[ReconRow, ...]

    @property
    def mismatches(self) -> tuple[ReconRow, ...]:
        return tuple(r for r in self.rows if r.status == "mismatch")

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame([{**r.__dict__, "diff": r.diff} for r in self.rows])
