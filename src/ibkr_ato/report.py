"""Build the per-FY HTML report: one self-contained page, tables only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import jinja2
import pandas as pd

from . import __version__
from .ledger import run_ledger
from .pipeline import PipelineResult
from .summary import LABELS, lodged_delta

KNOWN_GAPS = [
    "Foreign exchange gains or losses on USD/HKD cash held at IB (forex realisation events) "
    "are not computed. Currency conversions are listed in fx_conversions.csv; seek advice.",
    "Payments in lieu of dividends are counted as foreign income; the ATO treatment for a "
    "lender was not confirmed.",
    "Franking credits on Australian shares are not read from IB; use the company statements.",
    "Options, futures and bonds are not handled (listed under issues if present).",
    "Commissions on currency conversions are not included in any figure.",
]


@dataclass(frozen=True)
class ReportStatus:
    final: bool
    reasons: tuple[str, ...]


def report_status(result: PipelineResult, fy: int) -> ReportStatus:
    reasons = []
    pending = result.corp_actions.pending
    if not pending.empty:
        reasons.append(f"{len(pending)} corporate action(s) need a decision "
                       "(status needs_manual in corporate_actions.csv)")
    unresolved = [u for u in result.ledger.unresolved if _fy(u.date) <= fy]
    if unresolved:
        reasons.append(f"{len(unresolved)} unresolved ledger item(s), e.g. sells without lots")
    mism = result.reconciliations[fy].mismatches
    if mism:
        reasons.append(f"{len(mism)} reconciliation mismatch(es) against IB")
    return ReportStatus(not reasons, tuple(reasons))


def _fy(iso_date: str) -> int:
    d = pd.Timestamp(iso_date)
    return d.year + (1 if d.month >= 7 else 0)


def _records(df: pd.DataFrame) -> list[dict]:
    return df.to_dict("records") if df is not None and not df.empty else []


def _cgt_groups(parcels: pd.DataFrame) -> list[dict]:
    groups = []
    for sym, g in parcels.sort_values(["symbol", "disp_date", "acq_date"]).groupby("symbol"):
        groups.append({"symbol": sym, "rows": _records(g),
                       "total": {c: float(g[c].sum()) for c in
                                 ("quantity", "cost_aud", "proceeds_aud", "gain_aud")}})
    return groups


def _income_by_symbol(income: pd.DataFrame) -> list[dict]:
    inc = income[income["cat"].isin(["div", "roc", "pil", "wht"])]
    if inc.empty:
        return []
    pv = inc.pivot_table(index=["symbol", "country"], columns="cat", values="aud",
                         aggfunc="sum", fill_value=0.0).reset_index()
    for c in ("div", "roc", "pil", "wht"):
        if c not in pv:
            pv[c] = 0.0
    return _records(pv)


def _summary_rows(result: PipelineResult, fy: int) -> tuple[list[dict], bool]:
    values = result.summaries[fy].values
    lodged = result.lodged.get(fy)
    deltas = {d.key: d for d in lodged_delta(values, lodged)} if lodged else {}
    rows = []
    for lb in LABELS:
        d = deltas.get(lb.key)
        rows.append({"tag": f"{lb.item}{lb.label}" if lb.label else lb.item, "en": lb.en,
                     "zh": lb.zh, "value": values.get(lb.key, 0.0),
                     "lodged": d.lodged if d else None, "delta": d.delta if d else None,
                     "flag": d.flag if d else ""})
    return rows, bool(lodged)


def _assumptions(result: PipelineResult, fy: int) -> list[str]:
    s, rates = result.settings, result.rates
    lo, hi = rates.date_range
    return [
        f"Exchange rates: RBA F11 daily ({lo} to {hi}); a date without a rate uses the latest "
        f"earlier published rate, up to {s.fx_max_gap_days} days back.",
        "Parcels matched first in, first out (FIFO) per security.",
        "50% CGT discount when the disposal date is after the first anniversary of the "
        "acquisition date (at least 12 months excluding both days).",
        "Capital losses applied to non-discount gains first, then to discount gains.",
        f"Net capital losses brought forward: {result.summaries[fy].carry_in_source}.",
        f"IB interest treated as {s.interest_source} income. Margin interest split "
        f"{s.margin_foreign_share:.0%} foreign shares (D15) / "
        f"{1 - s.margin_foreign_share:.0%} Australian shares (D8).",
        f"Fees on foreign shares {'are' if s.deduct_foreign_fees else 'are NOT'} deducted at "
        "item 20 (setting deduct_foreign_fees).",
        f"Return of capital treated as {'a dividend (in 20E)' if s.return_of_capital == 'dividend' else 'a cost base reduction (CGT event G1), not income'} "  # noqa: E501
        "(setting return_of_capital).",
        "Amounts are rounded to cents here; the return takes whole dollars.",
        "This is not tax advice. Check the figures against your records and the ATO "
        "instructions for the year.",
    ]


def build_context(result: PipelineResult, fy: int) -> dict:
    parcels = result.ledger.parcels
    p = parcels[parcels["fy"] == fy] if not parcels.empty else parcels
    inc = result.income[result.income["fy"] == fy] if not result.income.empty else result.income
    fy_end = pd.Timestamp(year=fy, month=6, day=30)
    holdings = run_ledger(result.trades, result.events, result.opening, until=fy_end).holdings
    summary_rows, has_lodged = _summary_rows(result, fy)
    s = result.summaries[fy]
    v = s.values
    events = [e for e in result.events if _fy(str(e.date)) == fy]
    recon = result.reconciliations[fy].rows
    order = {"mismatch": 0, "info": 1, "n/a": 2, "ok": 3}
    return {
        "fy": fy, "period": f"1 July {fy - 1} to 30 June {fy}", "version": __version__,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": report_status(result, fy),
        "flex_files": [{"name": f.path.name, "from": f.from_date, "to": f.to_date}
                       for f in result.files],
        "rba_files": [x.name for x in result.rates.sources], "rba_range": result.rates.date_range,
        "summary_rows": summary_rows, "has_lodged": has_lodged,
        "cgt_groups": _cgt_groups(p),
        "cgt": {"carry_in": v["cgt_carry_in"], "losses": v["cgt_losses_current"],
                "other": v["cgt_gains_other"], "disc": v["cgt_gains_discountable"],
                "use_o": s.applied_to_other, "use_d": s.applied_to_discount,
                "discount": (v["cgt_gains_discountable"] - s.applied_to_discount) * 0.5,
                "net": v["18A"], "carry_out": v["18V"], "carry_source": s.carry_in_source},
        "holdings": _records(holdings.sort_values(["symbol", "acq_date"])),
        "income_by_symbol": _income_by_symbol(inc),
        "income_rows": _records(inc[inc["cat"] != "fee"]),
        "fee_rows": _records(inc[inc["cat"] == "fee"]),
        "values": v,
        "recon": sorted(recon, key=lambda r: (r.section, order[r.status])),
        "recon_counts": pd.Series([r.status for r in recon]).value_counts().to_dict(),
        "unresolved": [u._asdict() for u in result.ledger.unresolved if _fy(u.date) == fy],
        "pending": _records(result.corp_actions.pending),
        "issues": [i._asdict() for i in result.issues],
        "events": [e.__dict__ | {"kind": type(e).__name__} for e in events],
        "assumptions": _assumptions(result, fy), "known_gaps": KNOWN_GAPS,
    }


def _money(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, (int, float)):
        return f"{round(v, 2) + 0.0:,.2f}"  # + 0.0 turns -0.0 into 0.0
    return str(v)


def _qty(v) -> str:
    return "" if v is None or pd.isna(v) else f"{v:,.6g}"


def render_report(context: dict) -> str:
    env = jinja2.Environment(loader=jinja2.PackageLoader("ibkr_ato", "templates"),
                             autoescape=jinja2.select_autoescape(["html", "j2"]),
                             undefined=jinja2.StrictUndefined, trim_blocks=True,
                             lstrip_blocks=True)
    env.filters["money"] = _money
    env.filters["qty"] = _qty
    return env.get_template("report.html.j2").render(**context)


def write_report(result: PipelineResult, fy: int, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.html"
    path.write_text(render_report(build_context(result, fy)), encoding="utf-8")
    return path
