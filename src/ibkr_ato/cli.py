"""Command-line entry point. The only module that prints."""

from __future__ import annotations

import argparse
import sys
import urllib.error
from collections.abc import Sequence

from . import flex
from .config import DataDir, Settings, load_settings
from .corporate_actions import CorpActionInvalid, draft_corporate_actions, read_csv
from .fx import RBA_URLS, FxRateMissing, fetch_rba, load_rba_dir
from .pipeline import PipelineResult, run_pipeline, write_csvs
from .report import report_status, write_report
from .summary import LABELS, summary_values_from_csv, write_lodged

EXIT_OK, EXIT_ERROR, EXIT_DRAFT = 0, 1, 2
# Keys renamed since the original ib_ato.py script (for importing its summaries).
LEGACY_KEYS = {"margin_interest_deducted_20": "D15_margin_interest_foreign",
               "margin_interest_remaining": "D8_margin_interest_au"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ibkr-ato", description=(
        "Interactive Brokers Flex Query exports to ATO individual tax-return figures. "
        "Not tax advice."))
    p.add_argument("--data", help="data directory (default: $IBKR_ATO_DATA)")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="compute figures; write report.html and CSVs per FY")
    g = run.add_mutually_exclusive_group()
    g.add_argument("--fy", type=int, help="financial year, e.g. 2026 (default: latest)")
    g.add_argument("--all-fy", action="store_true", help="write outputs for every FY")
    run.add_argument("--prior-losses", type=float, help="net capital losses before the history")
    run.add_argument("--margin-foreign-share", type=float,
                     help="share of margin interest for foreign shares, 0 to 1")
    run.add_argument("--interest-source", choices=["foreign", "au"])
    run.add_argument("--fx-max-gap-days", type=int)
    run.add_argument("--deduct-foreign-fees", action="store_true", default=None)
    run.add_argument("--return-of-capital", choices=["dividend", "cost_base"])
    run.add_argument("--no-lodged-carry", action="store_true",
                     help="recompute carried-forward losses instead of using lodged 18V")

    fetch = sub.add_parser("fetch-rba", help="download RBA F11 exchange rates into rba/")
    fetch.add_argument("--force", action="store_true", help="download even if files exist")

    ca = sub.add_parser("corp-actions", help="draft corporate_actions.csv from IB's records")
    ca.add_argument("--dry-run", action="store_true", help="print drafts, write nothing")

    lodge = sub.add_parser("lodge", help="snapshot an FY's figures as lodged with the ATO")
    lodge.add_argument("--fy", type=int, required=True)
    lodge.add_argument("--from", dest="from_csv",
                       help="take values from a summary CSV (e.g. the legacy ato_summary.csv)")
    lodge.add_argument("--force", action="store_true", help="overwrite, or lodge a DRAFT")
    lodge.add_argument("--note", default="")
    return p


def _settings(args: argparse.Namespace, data: DataDir) -> Settings:
    overrides = {
        "prior_losses": getattr(args, "prior_losses", None),
        "margin_foreign_share": getattr(args, "margin_foreign_share", None),
        "interest_source": getattr(args, "interest_source", None),
        "fx_max_gap_days": getattr(args, "fx_max_gap_days", None),
        "deduct_foreign_fees": getattr(args, "deduct_foreign_fees", None),
        "return_of_capital": getattr(args, "return_of_capital", None),
        "carry_in_from_lodged": False if getattr(args, "no_lodged_carry", False) else None,
    }
    return load_settings(data.settings, overrides)


def print_summary(result: PipelineResult, fy: int) -> None:
    values = result.summaries[fy].values
    print(f"\n=== FY{fy}  (1 Jul {fy - 1} to 30 Jun {fy}) ===")
    for lb in LABELS:
        v = values.get(lb.key, 0.0)
        tag = f"{lb.item}{lb.label}" if lb.label else (lb.item or "-")
        val = f"{v:>14,.2f}" if isinstance(v, float) else f"{v:>14}"
        print(f"  {tag:<5} {val}  {lb.en} / {lb.zh}")


def cmd_run(args: argparse.Namespace, data: DataDir) -> int:
    result = run_pipeline(data, _settings(args, data))
    if not result.fys:
        print("no trades or income found", file=sys.stderr)
        return EXIT_ERROR
    if args.fy and args.fy not in result.summaries:
        print(f"no activity in FY{args.fy}; years with data: {result.fys}", file=sys.stderr)
        return EXIT_ERROR
    fys = result.fys if args.all_fy else [args.fy or max(result.fys)]
    code = EXIT_OK
    for fy in fys:
        out = data.out(fy)
        write_csvs(result, fy, out)
        report = write_report(result, fy, out)
        status = report_status(result, fy)
        print_summary(result, fy)
        if status.final:
            print("  status: FINAL")
        else:
            code = EXIT_DRAFT
            print("  status: DRAFT")
            for r in status.reasons:
                print(f"    - {r}")
        print(f"  report: {report}")
    return code


def cmd_fetch(args: argparse.Namespace, data: DataDir) -> int:
    try:
        written = fetch_rba(data.rba, force=args.force)
    except (urllib.error.URLError, TimeoutError) as e:
        urls = "\n  ".join(RBA_URLS.values())
        print(f"download failed: {e}\nDownload manually into {data.rba}:\n  {urls}",
              file=sys.stderr)
        return EXIT_ERROR
    for p in written:
        print(f"downloaded {p}")
    lo, hi = load_rba_dir(data.rba).date_range
    print(f"RBA rates available {lo} to {hi}")
    return EXIT_OK


def cmd_corp_actions(args: argparse.Namespace, data: DataDir) -> int:
    ib = flex.corp_action_rows(flex.load_flex_dir(data.flex))
    existing = read_csv(data.corporate_actions)
    merged = draft_corporate_actions(ib, existing)
    added = len(merged) - len(existing)
    for status, g in merged.groupby("status"):
        print(f"\n[{status}] {len(g)}")
        for r in g.itertuples():
            print(f"  {r.date} {r.type or '?':<16} {r.symbol:<10} {r.action_id}")
            if status == "needs_manual":
                print(f"      {r.note}")
    if args.dry_run:
        print(f"\n{added} new row(s); dry run, nothing written")
    elif added:
        merged.to_csv(data.corporate_actions, index=False)
        print(f"\n{added} new row(s) written to {data.corporate_actions}")
    else:
        print("\nno new corporate actions")
    return EXIT_OK


def cmd_lodge(args: argparse.Namespace, data: DataDir) -> int:
    target = data.lodged_file(args.fy)
    if target.exists() and not args.force:
        print(f"{target} exists; use --force to overwrite", file=sys.stderr)
        return EXIT_ERROR
    if args.from_csv:
        raw = summary_values_from_csv(args.from_csv, args.fy)
        values = {LEGACY_KEYS.get(k, k): v for k, v in raw.items()}
        source = f"imported from {args.from_csv}"
    else:
        result = run_pipeline(data, _settings(args, data))
        if args.fy not in result.summaries:
            print(f"no activity in FY{args.fy}", file=sys.stderr)
            return EXIT_ERROR
        status = report_status(result, args.fy)
        if not status.final and not args.force:
            print("FY is DRAFT; resolve open items or use --force:\n  - "
                  + "\n  - ".join(status.reasons), file=sys.stderr)
            return EXIT_ERROR
        values, source = dict(result.summaries[args.fy].values), "ibkr-ato run"
    write_lodged(values, target, source, args.note)
    print(f"lodged snapshot written to {target}")
    return EXIT_OK


COMMANDS = {"run": cmd_run, "fetch-rba": cmd_fetch, "corp-actions": cmd_corp_actions,
            "lodge": cmd_lodge}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data = DataDir.from_arg_or_env(args.data)
        return COMMANDS[args.command](args, data)
    except (ValueError, FileNotFoundError, FxRateMissing, CorpActionInvalid) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
