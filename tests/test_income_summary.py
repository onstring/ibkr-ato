from dataclasses import replace
from datetime import date

import pandas as pd
import pytest
from conftest import cash, write_flex, write_rba

from ibkr_ato import flex
from ibkr_ato.config import Settings
from ibkr_ato.fx import load_rba
from ibkr_ato.income import build_income, categorise
from ibkr_ato.summary import (
    build_summaries,
    cgt_for_fy,
    income_for_fy,
    lodged_delta,
    summary_frame,
)


@pytest.mark.parametrize("t,d,cat", [
    ("Dividends", "Ordinary Dividend", "div"),
    ("Dividends", "Return of Capital", "roc"),
    ("Withholding Tax", "", "wht"),
    ("Payment In Lieu Of Dividends", "", "pil"),
    ("Broker Interest Received", "", "int_recv"),
    ("Broker Interest Paid", "", "int_paid"),
    ("Other Fees", "", "fee"),
    ("Deposits/Withdrawals", "", None),
])
def test_categorise(t, d, cat):
    assert categorise(t, d) == cat


@pytest.fixture
def income(tmp_path):
    write_rba(tmp_path / "f.csv", {d.date(): {"USD": 0.5, "HKD": 4.0}
                                   for d in pd.bdate_range("2025-01-01", "2026-12-31")})
    rates = load_rba([tmp_path / "f.csv"])
    rows = [cash("Dividends", 100.0, date(2025, 9, 1)),
            cash("Withholding Tax", -15.0, date(2025, 9, 1)),
            cash("Dividends", 8.0, date(2025, 9, 2), symbol="RRR", isin="VG0000000001",
                 cur="HKD", divtype="Return of Capital"),
            cash("Dividends", 50.0, date(2025, 9, 3), symbol="ZZZ", isin="AU0000000001",
                 cur="AUD", fx_ib=1),
            cash("Broker Interest Paid", -10.0, date(2025, 10, 1), symbol="", isin=""),
            cash("Other Fees", -2.0, date(2025, 10, 2)),
            cash("Other Fees", -4.0, date(2025, 10, 3), symbol="", isin=""),
            cash("Deposits/Withdrawals", 1000.0, date(2025, 8, 1), symbol="", isin="",
                 cur="AUD")]
    f = flex.read_flex(write_flex(tmp_path / "c.csv", cash=rows))
    inc, ignored = build_income(flex.cash_rows([f]), rates, "foreign", 7)
    assert len(ignored) == 1
    return inc


def test_income_lines(income):
    v = income_for_fy(income, 2026, Settings(margin_foreign_share=0.75))
    assert v["foreign_dividends"] == pytest.approx(200)  # 100 USD at 0.5
    assert v["return_of_capital"] == pytest.approx(2)    # 8 HKD at 4.0
    assert v["20E"] == pytest.approx(202)                # ROC counted as dividend by default
    assert v["20M"] == pytest.approx(202)                # fees not netted by default
    assert v["20O"] == pytest.approx(30)
    assert v["au_dividends_cash"] == pytest.approx(50)
    assert v["D15_margin_interest_foreign"] == pytest.approx(15)
    assert v["D8_margin_interest_au"] == pytest.approx(5)
    assert v["fees_foreign"] == pytest.approx(4)
    assert v["fees_unallocated"] == pytest.approx(8)


def test_income_options(income):
    v = income_for_fy(income, 2026, Settings(deduct_foreign_fees=True,
                                             return_of_capital="cost_base"))
    assert v["20E"] == pytest.approx(200)
    assert v["20M"] == pytest.approx(196)


def parcels(rows):
    df = pd.DataFrame(rows, columns=["gain_aud", "discount_eligible", "fy"])
    return df.astype({"gain_aud": float, "discount_eligible": bool})


def test_losses_go_against_non_discount_gains_first():
    v = cgt_for_fy(parcels([(100, False, 2026), (100, True, 2026), (-150, False, 2026)]),
                   2026, 0.0)
    assert (v["18H"], v["18A"], v["18V"]) == (200, 25, 0)


def test_carry_forward_and_schedule_flag():
    p = parcels([(-12000, False, 2025), (5000, True, 2026)])
    s = build_summaries(p, pd.DataFrame(columns=["fy"]), Settings(), {})
    assert s[2025].values["18V"] == 12000
    assert s[2025].values["cgt_schedule_required"] == "YES"  # losses over $10,000
    assert s[2026].values["18A"] == 0 and s[2026].values["18V"] == 7000


def test_carry_in_from_lodged_snapshot():
    p = parcels([(1000, False, 2026)])
    s = build_summaries(p, pd.DataFrame(columns=["fy"]), Settings(), {2025: {"18V": 400.0}})
    assert s[2026].values["18A"] == 600
    assert s[2026].carry_in_source == "lodged FY2025"
    s2 = build_summaries(p, pd.DataFrame(columns=["fy"]),
                         replace(Settings(), carry_in_from_lodged=False), {2025: {"18V": 400.0}})
    assert s2[2026].values["18A"] == 1000


def test_lodged_delta_flags_whole_dollar_changes():
    rows = {r.key: r for r in lodged_delta({"18A": 100.4, "20E": 50.0},
                                           {"18A": 100.0, "20E": 51.0})}
    assert rows["18A"].flag == "ok"
    assert rows["20E"].flag == "review/amend"


def test_summary_frame_has_bilingual_labels():
    s = build_summaries(parcels([(1, False, 2026)]), pd.DataFrame(columns=["fy"]), Settings(), {})
    df = summary_frame(s)
    assert {"description_en", "description_zh"} <= set(df.columns)
    assert df.loc[df["key"] == "18H", "value"].iloc[0] == 1
