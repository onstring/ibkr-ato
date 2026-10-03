"""Regression tests against the private data set in $IBKR_ATO_DATA.

Two baselines live in $IBKR_ATO_DATA/golden (never in this repository):

- ``baseline/``: output of the original ib_ato.py script. ``test_legacy_parity`` proves the new
  pipeline reproduces it, with renamed keys mapped explicitly.
- ``current/``: output of this package. ``test_current`` catches any change. After an intended
  change, refresh it with ``IBKR_ATO_UPDATE_GOLDEN=1 uv run pytest -m golden`` and record why in
  the commit message.

Failures print private amounts, so run these only on your own machine.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from ibkr_ato.config import DataDir, Settings
from ibkr_ato.pipeline import run_pipeline, write_all_fy_csvs

pytestmark = pytest.mark.golden
DATA = os.environ.get("IBKR_ATO_DATA")
UPDATE = os.environ.get("IBKR_ATO_UPDATE_GOLDEN") == "1"
TOL = 0.005

# Legacy summary key -> new key. Values must match after mapping.
LEGACY_KEY_MAP = {
    "margin_interest_deducted_20": "D15_margin_interest_foreign",
    "margin_interest_remaining": "D8_margin_interest_au",
}
LEGACY_SUM_KEYS = {
    "fees_other": ("fees_foreign", "fees_unallocated", "fees_au"),
    # Legacy counted IB "Return of Capital" dividends as ordinary dividends.
    "foreign_dividends": ("foreign_dividends", "return_of_capital"),
}


def _require_data() -> DataDir:
    if not DATA:
        pytest.skip("IBKR_ATO_DATA not set")
    return DataDir.at(DATA)


@pytest.fixture(scope="module")
def result():
    data = _require_data()
    return run_pipeline(data, Settings(carry_in_from_lodged=False))


def _close(a: pd.Series, b: pd.Series) -> pd.Series:
    an, bn = pd.to_numeric(a, errors="coerce"), pd.to_numeric(b, errors="coerce")
    both_num = an.notna() & bn.notna()
    return (both_num & ((an - bn).abs() <= TOL)) | (~both_num & (a.astype(str) == b.astype(str)))


def assert_frames_match(ours: pd.DataFrame, base: pd.DataFrame, keys: list[str], what: str):
    cols = [c for c in base.columns if c in ours.columns]
    o = ours[cols].astype(str).sort_values(keys).reset_index(drop=True)
    b = base[cols].astype(str).sort_values(keys).reset_index(drop=True)
    assert len(o) == len(b), f"{what}: {len(o)} rows vs baseline {len(b)}"
    bad = {c: int((~_close(o[c], b[c])).sum()) for c in cols}
    bad = {c: n for c, n in bad.items() if n}
    if bad:
        c = next(iter(bad))
        diff = pd.concat([o[keys + [c]], b[[c]].add_suffix("_baseline")], axis=1)
        pytest.fail(f"{what}: mismatching columns {bad}\n{diff[~_close(o[c], b[c])].head(10)}")


def _legacy_summary(result) -> pd.DataFrame:
    rows = []
    for fy, s in result.summaries.items():
        v = dict(s.values)
        for old, new in LEGACY_KEY_MAP.items():
            v[old] = v[new]
        for old, parts in LEGACY_SUM_KEYS.items():
            v[old] = sum(float(v[p]) for p in parts)
        rows += [{"fy": f"FY{fy}", "key": k, "value": round(x, 2) + 0.0 if isinstance(x, float)
                  else x} for k, x in v.items()]
    return pd.DataFrame(rows)


def test_legacy_parity(result):
    base_dir = Path(DATA) / "golden" / "baseline"
    if not base_dir.exists():
        pytest.skip("no legacy baseline")
    base = pd.read_csv(base_dir / "ato_summary.csv", dtype=str, keep_default_na=False)
    ours = _legacy_summary(result)
    merged = base.merge(ours, on=["fy", "key"], how="left", suffixes=("_base", ""))
    assert merged["value"].notna().all(), merged[merged["value"].isna()][["fy", "key"]]
    ok = _close(merged["value"].astype(str), merged["value_base"])
    assert ok.all(), merged[~ok][["fy", "key", "value_base", "value"]]

    parcels = result.ledger.parcels.rename(columns={"disp_source": "disp_trade"})
    base_p = pd.read_csv(base_dir / "cgt_parcels.csv", dtype=str, keep_default_na=False)
    assert_frames_match(parcels, base_p, ["fy", "disp_date", "symbol", "acq_date", "quantity"],
                        "cgt_parcels")

    base_l = pd.read_csv(base_dir / "open_lots_end.csv", dtype=str, keep_default_na=False)
    assert_frames_match(result.ledger.holdings, base_l, ["symbol", "acq_date", "quantity"],
                        "open_lots_end")

    income = result.income.assign(cat=result.income["cat"].replace({"roc": "div"}))
    base_i = pd.read_csv(base_dir / "income_detail.csv", dtype=str, keep_default_na=False)
    assert_frames_match(income, base_i, ["fy", "date", "cat", "tid", "symbol", "amount"],
                        "income_detail")


def test_current(result, tmp_path):
    current = Path(DATA) / "golden" / "current"
    if UPDATE:
        write_all_fy_csvs(result, current)
        pytest.skip(f"golden baseline refreshed at {current}")
    if not current.exists():
        pytest.skip("no current baseline; run with IBKR_ATO_UPDATE_GOLDEN=1 to create it")
    fresh = write_all_fy_csvs(result, tmp_path)
    keys = {"summary_all.csv": ["fy", "key"],
            "cgt_parcels_all.csv": ["fy", "disp_date", "conid", "acq_date", "quantity"],
            "income_all.csv": ["fy", "date", "cat", "tid", "amount"],
            "open_lots_end.csv": ["conid", "acq_date", "quantity"]}
    for path in fresh:
        ours = pd.read_csv(path, dtype=str, keep_default_na=False)
        base = pd.read_csv(current / path.name, dtype=str, keep_default_na=False)
        assert list(ours.columns) == list(base.columns), f"{path.name}: columns changed"
        assert_frames_match(ours, base, keys[path.name], path.name)
