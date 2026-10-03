"""Pipeline, reconciliation, report and CLI on a small synthetic data set.

AAA (USD): bought 10 @ 10 on 2 Jan 2024, sold 10 @ 15 on 3 Mar 2025 (discount eligible).
BBB (HKD): bought 100 @ 4 on 1 Aug 2025, still held at 30 Jun 2026.
Rates: 1 USD = 2 AUD, 1 HKD = 0.25 AUD. IB's own figures agree with ours.
"""

from datetime import date

import pytest
from conftest import cash, closed_lot, nav, position, trade, write_flex

from ibkr_ato.cli import main
from ibkr_ato.config import Settings
from ibkr_ato.pipeline import run_pipeline
from ibkr_ato.report import build_context, render_report, report_status


def make_data(d, *, drop_closed_lot=False, position_qty=100, extra_cash=()):
    fy25 = [trade("AAA", "1", date(2024, 1, 2), 10, 10.0, comm=0, fx_ib=2),
            trade("AAA", "1", date(2025, 3, 3), -10, 15.0, comm=0, fx_ib=2)]
    if not drop_closed_lot:
        fy25.append(closed_lot("AAA", "1", date(2025, 3, 3), date(2024, 1, 2), 10, 100, 50,
                               term="LT"))
    write_flex(d.flex / "fy2025.csv", nav=[nav(date(2024, 7, 1), date(2025, 6, 30))],
               trades=fy25)
    rows = [cash("Dividends", 10.0, date(2025, 9, 1), symbol="BBB", conid="2",
                 isin="HK0000000001", cur="HKD", fx_ib=0.25),
            cash("Withholding Tax", -1.0, date(2025, 9, 1), symbol="BBB", conid="2",
                 isin="HK0000000001", cur="HKD", fx_ib=0.25), *extra_cash]
    write_flex(d.flex / "fy2026.csv",
               nav=[nav(date(2025, 7, 1), date(2026, 6, 30), Dividends=2.5,
                        WithholdingTax=-0.25)],
               positions=[position("BBB", "2", date(2026, 6, 30), position_qty, 400, cur="HKD")],
               trades=[trade("BBB", "2", date(2025, 8, 1), 100, 4.0, cur="HKD", comm=0,
                             fx_ib=0.25, isin="HK0000000001")],
               cash=rows)
    return d


def test_figures_and_clean_reconciliation(data_dir):
    r = run_pipeline(make_data(data_dir), Settings())
    v25, v26 = r.summaries[2025].values, r.summaries[2026].values
    assert v25["18H"] == pytest.approx(100)  # (150 - 100) USD x 2
    assert v25["18A"] == pytest.approx(50)
    assert v26["20E"] == pytest.approx(2.5) and v26["20O"] == pytest.approx(0.25)
    for fy in (2025, 2026):
        assert not r.reconciliations[fy].mismatches, r.reconciliations[fy].frame()
        assert report_status(r, fy).final


def test_missing_closed_lot_is_a_mismatch(data_dir):
    r = run_pipeline(make_data(data_dir, drop_closed_lot=True), Settings())
    assert any(x.check == "disposal" for x in r.reconciliations[2025].mismatches)


def test_position_difference_is_a_mismatch(data_dir):
    r = run_pipeline(make_data(data_dir, position_qty=90), Settings())
    assert any(x.section == "B positions" for x in r.reconciliations[2026].mismatches)


def test_missing_cash_row_is_a_nav_mismatch(data_dir):
    extra = [cash("Dividends", 4.0, date(2025, 10, 1), symbol="BBB", conid="2",
                  isin="HK0000000001", cur="HKD", fx_ib=0.25)]
    r = run_pipeline(make_data(data_dir, extra_cash=extra), Settings())
    assert any(x.section == "C NAV" for x in r.reconciliations[2026].mismatches)


def test_report_renders_bilingual_and_escapes(data_dir):
    extra = [cash("Dividends", 8.0, date(2025, 10, 1), symbol="BBB", conid="2",
                  isin="HK0000000001", cur="HKD", fx_ib=0.25, description="<script>x</script>")]
    r = run_pipeline(make_data(data_dir, extra_cash=extra), Settings())
    html = render_report(build_context(r, 2026))
    assert "Assessable foreign source income" in html and "应税境外来源收入" in html
    assert "<script>x" not in html and "&lt;script&gt;" in html
    assert "DRAFT" in html  # the extra dividend is not in IB's NAV


def test_cli_run_lodge_and_errors(data_dir, monkeypatch, capsys):
    make_data(data_dir)
    root = str(data_dir.root)
    assert main(["--data", root, "run", "--all-fy"]) == 0
    assert (data_dir.out(2026) / "report.html").exists()
    assert main(["--data", root, "lodge", "--fy", "2025"]) == 0
    assert main(["--data", root, "lodge", "--fy", "2025"]) == 1  # exists
    assert data_dir.lodged_file(2025).exists()
    assert main(["--data", root, "run", "--fy", "2025"]) == 0
    assert "Lodged" in (data_dir.out(2025) / "report.html").read_text()
    monkeypatch.delenv("IBKR_ATO_DATA", raising=False)
    assert main(["run"]) == 1
    assert "IBKR_ATO_DATA" in capsys.readouterr().err


def test_cli_corp_actions_dry_run_writes_nothing(data_dir):
    make_data(data_dir)
    assert main(["--data", str(data_dir.root), "corp-actions", "--dry-run"]) == 0
    assert not data_dir.corporate_actions.exists()
