import io
from datetime import date

import pandas as pd
import pytest
from conftest import write_rba

from ibkr_ato.fx import FxRateMissing, fetch_rba, load_rba


@pytest.fixture
def rates(tmp_path):
    # Fri 3 Jan 2025, then a 9-day gap to Mon 13 Jan.
    write_rba(tmp_path / "f.csv", {date(2025, 1, 2): {"USD": 0.6},
                                   date(2025, 1, 3): {"USD": 0.62, "HKD": 4.8},
                                   date(2025, 1, 13): {"USD": 0.64, "HKD": 5.0}})
    return load_rba([tmp_path / "f.csv"])


def ts(*days):
    return pd.Series(pd.to_datetime(list(days)))


def test_aud_is_one(rates):
    assert rates.rates_for(pd.Series(["AUD"]), ts("2030-01-01"), 7).iloc[0] == 1.0


def test_weekend_uses_previous_published_day(rates):
    assert rates.rates_for(pd.Series(["USD"]), ts("2025-01-05"), 7).iloc[0] == 0.62


def test_gap_beyond_limit_raises_and_lists_every_miss(rates):
    with pytest.raises(FxRateMissing) as e:
        rates.rates_for(pd.Series(["USD", "HKD"]), ts("2025-01-11", "2025-01-12"), 7)
    assert {c for c, _ in e.value.misses} == {"USD", "HKD"}


def test_unknown_currency_raises(rates):
    with pytest.raises(FxRateMissing):
        rates.rates_for(pd.Series(["XYZ"]), ts("2025-01-03"), 7)


def test_to_aud_divides_by_rate(rates):
    v = rates.to_aud(pd.Series([62.0]), pd.Series(["USD"]), ts("2025-01-03"), 7)
    assert v.iloc[0] == pytest.approx(100.0)


def test_later_file_wins_on_overlap(tmp_path):
    a = write_rba(tmp_path / "a.csv", {date(2025, 1, 3): {"USD": 0.6}})
    b = write_rba(tmp_path / "b.csv", {date(2025, 1, 3): {"USD": 0.7}})
    r = load_rba([a, b])
    assert r.rates_for(pd.Series(["USD"]), ts("2025-01-03"), 0).iloc[0] == 0.7


def test_not_an_f11_file(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="Series ID"):
        load_rba([p])


def test_xls_path_uses_read_excel(tmp_path, monkeypatch):
    grid = pd.DataFrame([["Title", "A$1=USD"], ["Series ID", "FXRUSD"],
                         [pd.Timestamp("2025-01-03"), 0.62]])
    monkeypatch.setattr(pd, "read_excel", lambda *a, **k: grid)
    p = tmp_path / "h.xls"
    p.write_bytes(b"")
    r = load_rba([p])
    assert r.rates_for(pd.Series(["USD"]), ts("2025-01-03"), 0).iloc[0] == 0.62


def test_fetch_rba_writes_files_and_respects_force(tmp_path):
    calls = []

    def fake(req, timeout):
        calls.append(req.full_url)
        return io.BytesIO(b"data")

    first = fetch_rba(tmp_path, urlopen=fake)
    assert len(first) == 2 and all(p.read_bytes() == b"data" for p in first)
    assert fetch_rba(tmp_path, urlopen=fake) == ()
    assert len(fetch_rba(tmp_path, force=True, urlopen=fake)) == 2
    assert len(calls) == 4
