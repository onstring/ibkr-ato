"""Run every stage over a data directory and write the per-FY outputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from . import flex
from .config import DataDir, Settings
from .corporate_actions import CorpActionsFile, CostReduction, load_corporate_actions
from .fx import RateTable, load_rba_dir
from .income import build_income
from .ledger import LedgerResult, load_opening_lots, prepare_trades, run_ledger
from .reconcile import (
    Reconciliation,
    reconcile_closed_lots,
    reconcile_nav,
    reconcile_positions,
)
from .summary import FySummary, Value, build_summaries, load_lodged_dir, summary_frame


class Issue(NamedTuple):
    kind: str
    message: str


@dataclass(frozen=True)
class PipelineResult:
    data: DataDir
    settings: Settings
    files: tuple[flex.FlexFile, ...]
    rates: RateTable
    trades: pd.DataFrame
    fx_trades: pd.DataFrame
    income: pd.DataFrame
    ignored_cash: pd.DataFrame
    corp_actions: CorpActionsFile
    events: tuple
    opening: pd.DataFrame
    ledger: LedgerResult
    summaries: dict[int, FySummary]
    lodged: dict[int, dict[str, Value]]
    reconciliations: dict[int, Reconciliation]
    issues: tuple[Issue, ...]

    @property
    def fys(self) -> list[int]:
        return sorted(self.summaries)


def roc_events(income: pd.DataFrame) -> tuple[CostReduction, ...]:
    roc = income[income["cat"] == "roc"]
    return tuple(CostReduction(f"roc:{r.tid}", r.date.date(), r.conid, r.symbol,
                               float(r.amount), float(r.aud)) for r in roc.itertuples())


def _issues(excluded: pd.DataFrame, files, ignored: pd.DataFrame) -> list[Issue]:
    out = []
    for asset, g in excluded.groupby("asset"):
        out.append(Issue("asset_not_handled", f"{len(g)} {asset} trades excluded from CGT "
                         f"({', '.join(sorted(g['symbol'].unique())[:10])}); handle manually"))
    if not flex.section(files, "transfers").empty:
        out.append(Issue("transfers", "position transfers present: add transferred-in lots to "
                         "opening_lots.csv"))
    for t, g in ignored.groupby("type"):
        out.append(Issue("cash_ignored", f"{len(g)} '{t}' cash rows are not tax items (ignored)"))
    return out


def run_pipeline(data: DataDir, settings: Settings) -> PipelineResult:
    gap = settings.fx_max_gap_days
    files = flex.load_flex_dir(data.flex)
    rates = load_rba_dir(data.rba)
    trades, fx_trades, excluded = prepare_trades(flex.executions(files), rates, gap)
    sym2key = dict(zip(trades["symbol"], trades["key"], strict=True))
    opening = load_opening_lots(data.opening_lots, rates, gap, sym2key)
    income, ignored = build_income(flex.cash_rows(files), rates, settings.interest_source, gap)
    ca = load_corporate_actions(data.corporate_actions, rates, gap)
    events = ca.events + (roc_events(income) if settings.return_of_capital == "cost_base" else ())
    ledger = run_ledger(trades, events, opening)
    lodged = load_lodged_dir(data.lodged) if data.lodged.exists() else {}
    summaries = build_summaries(ledger.parcels, income, settings, lodged)
    recon = reconcile_all(files, trades, fx_trades, income, events, opening, ledger, summaries)
    return PipelineResult(data, settings, files, rates, trades, fx_trades, income, ignored, ca,
                          events, opening, ledger, summaries, lodged, recon,
                          tuple(_issues(excluded, files, ignored)))


def reconcile_all(files, trades, fx_trades, income, events, opening, ledger,
                  summaries) -> dict[int, Reconciliation]:
    event_ids = {e.action_id for e in events}
    event_conids = {c for e in events for c in (e.conid, getattr(e, "to_conid", ""))
                    if not isinstance(e, CostReduction)}
    roc_conids = set(income.loc[income["cat"] == "roc", "conid"]) if not income.empty else set()
    closed, positions = flex.closed_lots(files), flex.open_positions(files)
    nav = flex.nav_totals(files)

    def holdings_at(as_of: pd.Timestamp) -> pd.DataFrame:
        return run_ledger(trades, events, opening, until=as_of).holdings

    out = {}
    for fy in summaries:
        rows = (reconcile_closed_lots(ledger.parcels, closed, fy, event_ids)
                + reconcile_positions(holdings_at, positions, fy, event_conids, roc_conids)
                + reconcile_nav(income, trades, fx_trades, nav, fy))
        out[fy] = Reconciliation(fy, tuple(rows))
    return out


def _fy_rows(df: pd.DataFrame, fy: int, col: str = "fy") -> pd.DataFrame:
    return df[df[col] == fy] if not df.empty else df


def write_csvs(result: PipelineResult, fy: int, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = {
        "summary.csv": summary_frame({fy: result.summaries[fy]}),
        "cgt_parcels.csv": _fy_rows(result.ledger.parcels, fy).sort_values(
            ["disp_date", "symbol"]),
        "income_detail.csv": _fy_rows(result.income, fy),
        "fx_conversions.csv": result.fx_trades[flex.fy_of(result.fx_trades["date"]) == fy]
        if not result.fx_trades.empty else result.fx_trades,
        "unresolved.csv": pd.DataFrame(result.ledger.unresolved),
        "reconciliation.csv": result.reconciliations[fy].frame(),
        "issues.csv": pd.DataFrame(result.issues, columns=["kind", "message"]),
    }
    if fy == max(result.fys):
        frames["open_lots.csv"] = result.ledger.holdings.sort_values(["symbol", "acq_date"])
    written = []
    for name, df in frames.items():
        path = out_dir / name
        df.to_csv(path, index=False)
        written.append(path)
    return written


def write_all_fy_csvs(result: PipelineResult, out_dir: Path) -> list[Path]:
    """Whole-history files, used by the golden test and for audit."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "summary_all.csv": summary_frame(result.summaries),
        "cgt_parcels_all.csv": result.ledger.parcels.sort_values(["fy", "disp_date", "symbol"]),
        "income_all.csv": result.income,
        "open_lots_end.csv": result.ledger.holdings.sort_values(["symbol", "acq_date"]),
    }
    for name, df in files.items():
        df.to_csv(out_dir / name, index=False)
    return [out_dir / n for n in files]
