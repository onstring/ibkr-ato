from datetime import date

import pytest
from conftest import cash, closed_lot, corp_action, nav, position, trade, write_flex

from ibkr_ato import flex


def test_reads_and_classifies_sections(tmp_path):
    p = write_flex(
        tmp_path / "a.csv",
        nav=[nav(date(2025, 7, 1), date(2026, 6, 30), Dividends=10)],
        positions=[position("AAA", "1", date(2026, 6, 30), 10, 100)],
        trades=[trade("AAA", "1", date(2025, 8, 1), 10, 10.0)],
        corp_actions=[corp_action("FS", "AAA SPLIT 2 FOR 1", 10, date(2025, 9, 1), "AAA", "1")],
        cash=[cash("Dividends", 5.0, date(2025, 9, 1))],
    )
    f = flex.read_flex(p)
    assert set(f.sections) == {"nav", "positions", "trades", "corp_actions", "cash"}
    assert (f.from_date, f.to_date) == (date(2025, 7, 1), date(2026, 6, 30))
    ca = flex.corp_action_rows([f])
    assert ca.loc[0, "date"].date() == date(2025, 9, 1)  # "Date/Time" normalised


def test_unknown_table_raises(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("ClientAccountID,Symbol,Conid,Weird\nU0,AAA,1,z\n")
    with pytest.raises(flex.FlexFormatError):
        flex.read_flex(p)


def test_overlapping_exports_dedupe_on_ids(tmp_path):
    t = trade("AAA", "1", date(2025, 8, 1), 10, 10.0, tid="T1")
    c = cash("Dividends", 5.0, date(2025, 9, 1), tid="C1")
    a = flex.read_flex(write_flex(tmp_path / "a.csv", trades=[t], cash=[c]))
    b = flex.read_flex(write_flex(tmp_path / "b.csv", trades=[t], cash=[c]))
    assert len(flex.executions([a, b])) == 1
    assert len(flex.cash_rows([a, b])) == 1


def test_executions_exclude_closed_lots_and_closed_lots_parse_open_date(tmp_path):
    rows = [trade("AAA", "1", date(2025, 8, 1), -10, 12.0),
            closed_lot("AAA", "1", date(2025, 8, 1), date(2024, 1, 5), 10, 100, 19, term="LT")]
    f = flex.read_flex(write_flex(tmp_path / "a.csv", trades=rows))
    assert len(flex.executions([f])) == 1
    cl = flex.closed_lots([f])
    assert cl.loc[0, "acq_date"].date() == date(2024, 1, 5)
    assert cl.loc[0, "ib_term"] == "LT"


def test_fy_of():
    import pandas as pd
    s = pd.to_datetime(pd.Series(["2025-06-30", "2025-07-01"]))
    assert list(flex.fy_of(s)) == [2025, 2026]


def test_conflicting_reexport_is_an_error(tmp_path):
    a = flex.read_flex(write_flex(tmp_path / "a.csv",
                                  trades=[trade("AAA", "1", date(2025, 8, 1), 10, 10.0, comm=-1,
                                                tid="T1")]))
    b = flex.read_flex(write_flex(tmp_path / "b.csv",
                                  trades=[trade("AAA", "1", date(2025, 8, 1), 10, 10.0, comm=-5,
                                                tid="T1")]))
    with pytest.raises(ValueError, match="disagree"):
        flex.executions([a, b])
