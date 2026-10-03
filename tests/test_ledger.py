from datetime import date

import pandas as pd
import pytest
from conftest import trade, write_flex, write_rba

from ibkr_ato import flex
from ibkr_ato.corporate_actions import AddLots, CostReduction, RemoveForCash, Rollover, Split
from ibkr_ato.fx import load_rba
from ibkr_ato.ledger import discount_eligible, prepare_trades, run_ledger


@pytest.mark.parametrize("acq,disp,expected", [
    ("2025-03-01", "2026-03-01", False),  # exact anniversary: not 12 months excluding both days
    ("2025-03-01", "2026-03-02", True),
    ("2024-02-02", "2025-02-02", False),  # ATO example: bought 2 Feb 2024 ...
    ("2024-02-02", "2025-02-03", True),   # ... eligible on or after 3 Feb 2025
    ("2024-02-29", "2025-02-28", False),
    ("2024-02-29", "2025-03-01", True),
])
def test_discount_boundary(acq, disp, expected):
    assert discount_eligible(pd.Timestamp(acq), pd.Timestamp(disp)) is expected


def build(tmp_path, rows, usd=0.5, hkd=4.0):
    rates_path = write_rba(tmp_path / "f.csv", {
        d.date(): {"USD": usd, "HKD": hkd}
        for d in pd.bdate_range("2022-01-01", "2026-12-31")})
    rates = load_rba([rates_path])
    f = flex.read_flex(write_flex(tmp_path / "t.csv", trades=rows))
    trades, _, _ = prepare_trades(flex.executions([f]), rates, 7)
    return trades


def test_fifo_consumes_oldest_lot_first_and_splits_partial_lots(tmp_path):
    trades = build(tmp_path, [
        trade("AAA", "1", date(2024, 1, 2), 10, 10.0, comm=0),
        trade("AAA", "1", date(2024, 6, 3), 10, 20.0, comm=0),
        trade("AAA", "1", date(2025, 3, 3), -15, 30.0, comm=0),
    ])
    r = run_ledger(trades)
    p = r.parcels
    assert list(p["quantity"]) == [10, 5]
    assert list(p["discount_eligible"]) == [True, False]
    # USD 0.5 per AUD: cost 10 x 10 USD = 200 AUD; proceeds 10 x 30 USD = 600 AUD
    assert p.loc[0, "cost_aud"] == pytest.approx(200)
    assert p.loc[0, "gain_aud"] == pytest.approx(400)
    assert r.holdings["quantity"].sum() == pytest.approx(5)
    assert r.holdings.loc[0, "cost_aud"] == pytest.approx(5 * 20 * 2)


def test_commission_and_taxes_in_cost_and_proceeds(tmp_path):
    trades = build(tmp_path, [
        trade("AAA", "1", date(2024, 1, 2), 10, 10.0, comm=-2, taxes=-1),
        trade("AAA", "1", date(2024, 2, 1), -10, 10.0, comm=-2, taxes=-1),
    ])
    p = run_ledger(trades).parcels
    assert p.loc[0, "cost_fc"] == pytest.approx(103)
    assert p.loc[0, "proceeds_fc"] == pytest.approx(97)
    assert p.loc[0, "gain_aud"] == pytest.approx(-12)


def test_commission_in_other_currency_gives_defined_fc_amounts(tmp_path):
    t = trade("AAA", "1", date(2024, 1, 2), 10, 10.0, cur="HKD", comm=-1)
    t["IBCommissionCurrency"] = "USD"
    trades = build(tmp_path, [t])
    # 1 USD = 8 HKD at these rates
    assert trades.loc[0, "net_fc"] == pytest.approx(-108)


def test_unmatched_sell_is_unresolved_but_matched_part_stays(tmp_path):
    trades = build(tmp_path, [trade("AAA", "1", date(2024, 1, 2), 5, 10.0, comm=0),
                              trade("AAA", "1", date(2024, 2, 1), -8, 10.0, comm=0)])
    r = run_ledger(trades)
    assert r.parcels["quantity"].sum() == pytest.approx(5)
    assert len(r.unresolved) == 1 and r.unresolved[0].quantity == pytest.approx(3)


def test_split_keeps_total_cost_and_acquisition_date(tmp_path):
    trades = build(tmp_path, [trade("AAA", "1", date(2024, 1, 2), 10, 10.0, comm=0)])
    r = run_ledger(trades, (Split("s", date(2024, 5, 1), "1", "AAA", 2.0),))
    h = r.holdings.iloc[0]
    assert (h["quantity"], h["cost_aud"], h["acq_date"]) == (20, pytest.approx(200),
                                                             date(2024, 1, 2))


def test_takeover_for_cash_creates_parcels(tmp_path):
    trades = build(tmp_path, [trade("BBB", "2", date(2024, 1, 2), 100, 4.0, cur="HKD", comm=0)])
    e = RemoveForCash("tc", date(2025, 3, 12), "2", "BBB", None, 600.0, "HKD", 4.0)
    p = run_ledger(trades, (e,)).parcels
    assert p.loc[0, "disp_source"] == "tc"
    assert p.loc[0, "gain_aud"] == pytest.approx((600 - 400) / 4)


def test_add_lots_and_rollover_carry_cost_and_date(tmp_path):
    add = AddLots("so", date(2025, 3, 11), "3", "CCC.SPO", 10.5, 63.0, "HKD", 4.0,
                  date(2025, 3, 11))
    roll = Rollover("tc", date(2025, 3, 18), "3", "CCC.SPO", "4", "DDD", 1.0, 10.0)
    r = run_ledger(build(tmp_path, []), (add, roll))
    h = r.holdings.set_index("conid")
    assert h.loc["4", "quantity"] == pytest.approx(10)
    assert h.loc["4", "acq_date"] == date(2025, 3, 11)
    assert h.loc["4", "cost_fc"] == pytest.approx(60)
    assert h.loc["3", "quantity"] == pytest.approx(0.5)  # fraction left behind


def test_rollover_beyond_holding_is_unresolved(tmp_path):
    roll = Rollover("tc", date(2025, 3, 18), "3", "X", "4", "Y", 1.0, 10.0)
    assert run_ledger(build(tmp_path, []), (roll,)).unresolved[0].kind == "event_exceeds_holding"


def test_cost_reduction_lowers_cost_base(tmp_path):
    trades = build(tmp_path, [trade("AAA", "1", date(2024, 1, 2), 10, 10.0, comm=0)])
    r = run_ledger(trades, (CostReduction("roc", date(2024, 6, 3), "1", "AAA", 5.0, 10.0),))
    assert r.holdings.loc[0, "cost_aud"] == pytest.approx(190)


def test_until_returns_holdings_at_a_date(tmp_path):
    trades = build(tmp_path, [trade("AAA", "1", date(2024, 1, 2), 10, 10.0, comm=0),
                              trade("AAA", "1", date(2024, 8, 1), -10, 10.0, comm=0)])
    r = run_ledger(trades, until=pd.Timestamp("2024-06-30"))
    assert r.holdings["quantity"].sum() == 10 and r.parcels.empty
