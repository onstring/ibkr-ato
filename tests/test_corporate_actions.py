from datetime import date

import pandas as pd
import pytest
from conftest import corp_action, write_flex, write_rba

from ibkr_ato import flex
from ibkr_ato.corporate_actions import (
    CSV_COLS,
    CorpActionInvalid,
    Rollover,
    Split,
    draft_corporate_actions,
    load_corporate_actions,
    parse_action,
)
from ibkr_ato.fx import load_rba


@pytest.fixture
def rates(tmp_path):
    p = write_rba(tmp_path / "f.csv", {d.date(): {"USD": 0.5, "HKD": 4.0}
                                       for d in pd.bdate_range("2025-01-01", "2026-12-31")})
    return load_rba([p])


def write_ca(tmp_path, *rows):
    df = pd.DataFrame([{**dict.fromkeys(CSV_COLS, ""), **r} for r in rows], columns=CSV_COLS)
    p = tmp_path / "corporate_actions.csv"
    df.to_csv(p, index=False)
    return p


@pytest.mark.parametrize("ratio", ["", "0", "-2", "abc", "inf"])
def test_bad_split_ratio_stops_the_run(tmp_path, rates, ratio):
    p = write_ca(tmp_path, {"action_id": "x", "status": "confirmed", "type": "split",
                            "date": "2025-05-01", "conid": "1", "ratio": ratio})
    with pytest.raises(CorpActionInvalid):
        load_corporate_actions(p, rates, 7)


@pytest.mark.parametrize("row", [
    {"type": "merge"},
    {"type": "split", "status": "maybe"},
    {"type": "add_lots", "quantity": "10", "currency": "HKD"},  # amount missing
    {"type": "rollover"},                                       # to_conid missing
    {"type": "split", "ratio": "2", "conid": ""},
])
def test_invalid_rows(tmp_path, rates, row):
    base = {"action_id": "x", "status": "confirmed", "date": "2025-05-01", "conid": "1"}
    with pytest.raises(CorpActionInvalid):
        load_corporate_actions(write_ca(tmp_path, {**base, **row}), rates, 7)


def test_applied_and_pending(tmp_path, rates):
    p = write_ca(tmp_path,
                 {"action_id": "a", "status": "auto", "type": "split", "date": "2025-05-01",
                  "conid": "1", "ratio": "2"},
                 {"action_id": "b", "status": "needs_manual", "type": "add_lots",
                  "date": "2025-05-01", "conid": "2"},
                 {"action_id": "c", "status": "confirmed", "type": "rollover",
                  "date": "2025-05-02", "conid": "2", "to_conid": "3", "quantity": "5"},
                 {"action_id": "d", "status": "ignored", "type": "", "date": "", "conid": ""})
    ca = load_corporate_actions(p, rates, 7)
    assert [type(e) for e in ca.events] == [Split, Rollover]
    assert list(ca.pending["action_id"]) == ["b"]


@pytest.mark.parametrize("desc,kind,field,value", [
    ("AAA(US0000000001) SPLIT 4 FOR 1 (AAA, ...)", "split", "ratio", 4.0),
    ("489(CNE100000312) SPINOFF  3552608 FOR 10000000 (489.SPO, ...)", "spinoff", "ratio",
     0.3552608),
    ("489(CNE100000312) MERGED(Acquisition) FOR HKD 6.68 PER SHARE (489, ...)", "cash",
     "per_share", 6.68),
    ("489.SPO(CNE10312SPO8) MERGED(Mandatory Offer Allocation) WITH CNE100007FB7 1 FOR 1 (...)",
     "scrip", "to_isin", "CNE100007FB7"),
])
def test_parse_action(desc, kind, field, value):
    k, f = parse_action("", desc)
    assert k == kind
    assert f[field] == pytest.approx(value) if isinstance(value, float) else f[field] == value


def test_draft_pairs_scrip_rows_and_keeps_existing(tmp_path):
    d = date(2026, 3, 18)
    scrip = "X.SPO(CNE10312SPO8) MERGED(Mandatory Offer Allocation) WITH CNE100007FB7 1 FOR 1"
    # Real IB rows share the ActionID; the trailing "(symbol, name, isin)" differs.
    rows = [corp_action("TC", scrip + " (X.SPO, X SPINOFF, CNE10312SPO8)", -10, d, "X.SPO", "3",
                        action_id="7"),
            corp_action("TC", scrip + " (Y, Y CO, CNE100007FB7)", 10, d, "Y", "4", action_id="7"),
            corp_action("FS", "Z SPLIT 2 FOR 1", 5, d, "Z", "5", action_id="8")]
    f = flex.read_flex(write_flex(tmp_path / "a.csv", corp_actions=rows))
    ib = flex.corp_action_rows([f])
    first = draft_corporate_actions(ib, pd.DataFrame(columns=CSV_COLS))
    assert list(first["type"]) == ["rollover", "split"]
    roll = first.iloc[0]
    assert (roll["conid"], roll["to_conid"], roll["status"]) == ("3", "4", "needs_manual")
    assert first.iloc[1]["status"] == "auto"

    edited = first.assign(status=["confirmed", "auto"], note=["mine", "auto"])
    again = draft_corporate_actions(ib, edited)
    assert again.equals(edited)  # existing rows untouched, nothing new
