"""RBA F11 daily exchange rates: load, look up, download.

RBA F11 quotes foreign currency units per AUD 1, so AUD = foreign amount / rate. For a date
without a published rate (weekend, public holiday) we use the latest earlier published rate,
the same practice the ATO follows in its own rate tables, up to ``max_gap_days`` back.
A missing rate is an error: a figure without its rate cannot be right.
"""

from __future__ import annotations

import csv
import ssl
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

RBA_SERIES = {
    "USD": "FXRUSD", "HKD": "FXRHKD", "EUR": "FXREUR", "GBP": "FXRUKPS", "JPY": "FXRJY",
    "CNY": "FXRCR", "SGD": "FXRSD", "NZD": "FXRNZD", "CAD": "FXRCD", "CHF": "FXRSF",
}
RBA_URLS = {
    "f11.1-data.csv": "https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv",
    "f11-2018-2022.xls": "https://www.rba.gov.au/statistics/tables/xls-hist/2018-2022.xls",
}
DEFAULT_MAX_GAP_DAYS = 7


class FxRateMissing(ValueError):
    """One or more (currency, date) pairs have no usable RBA rate."""

    def __init__(self, misses: Sequence[tuple[str, date]], max_gap_days: int):
        self.misses = tuple(misses)
        shown = ", ".join(f"{c} {d}" for c, d in self.misses[:10])
        more = f" (+{len(self.misses) - 10} more)" if len(self.misses) > 10 else ""
        super().__init__(
            f"no RBA rate within {max_gap_days} days for: {shown}{more}. "
            "Run 'ibkr-ato fetch-rba' or add the RBA F11 file covering these dates."
        )


@dataclass(frozen=True)
class RateTable:
    rates: pd.DataFrame  # DatetimeIndex; columns are RBA series IDs; FC per AUD 1
    sources: tuple[Path, ...] = ()

    @property
    def date_range(self) -> tuple[date, date]:
        return self.rates.index.min().date(), self.rates.index.max().date()

    def rates_for(self, currency: pd.Series, dates: pd.Series, max_gap_days: int) -> pd.Series:
        """Rate per row; raises FxRateMissing listing every row without a rate."""
        currency = currency.fillna("")  # a NaN currency must fail below, not be skipped
        out = pd.Series(np.nan, index=currency.index, dtype=float)
        misses: list[tuple[str, date]] = []
        for cur in currency.dropna().unique():
            m = currency == cur
            if cur == "AUD":
                out.loc[m] = 1.0
                continue
            vals = self._lookup(cur, dates[m], max_gap_days)
            out.loc[m] = vals
            bad = dates[m][np.isnan(vals)]
            misses += [(cur, d.date() if pd.notna(d) else None) for d in bad]
        if misses:
            raise FxRateMissing(sorted(set(misses), key=str), max_gap_days)
        return out

    def _lookup(self, cur: str, dates: pd.Series, max_gap_days: int) -> np.ndarray:
        sid = RBA_SERIES.get(cur)
        if sid is None or sid not in self.rates.columns:
            return np.full(len(dates), np.nan)
        s = self.rates[sid].dropna()
        d = dates.values.astype("datetime64[ns]")
        idx = s.index.values.searchsorted(d, side="right") - 1
        vals = np.full(len(d), np.nan)
        ok = (idx >= 0) & ~np.isnat(d)
        if ok.any():
            gap = (d[ok] - s.index.values[idx[ok]]) / np.timedelta64(1, "D")
            vals[ok] = np.where(gap <= max_gap_days, s.values[idx[ok]], np.nan)
        return vals

    def to_aud(self, amount: pd.Series, currency: pd.Series, dates: pd.Series,
               max_gap_days: int) -> pd.Series:
        return amount / self.rates_for(currency, dates, max_gap_days)


def _read_rows(path: Path) -> list[list[str]]:
    if path.suffix.lower() in (".xls", ".xlsx"):
        raw = pd.read_excel(path, header=None, dtype=object)
        return [["" if pd.isna(v) else str(v) for v in row] for row in raw.itertuples(index=False)]
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        return list(csv.reader(f))


def _load_one(path: Path) -> pd.DataFrame:
    rows = _read_rows(path)
    hdr = next((i for i, r in enumerate(rows) if r and r[0].strip() == "Series ID"), None)
    if hdr is None:
        raise ValueError(f"{path}: no 'Series ID' row; is this an RBA F11 file?")
    cols = [c.strip() for c in rows[hdr]]
    data = [(r + [""] * len(cols))[: len(cols)] for r in rows[hdr + 1 :] if r]
    df = pd.DataFrame(data, columns=cols)
    first = df[cols[0]].astype(str).str.strip()
    d = pd.to_datetime(first, format="%d-%b-%Y", errors="coerce")
    d = d.fillna(pd.to_datetime(first.str[:10], format="%Y-%m-%d", errors="coerce"))
    keep = d.notna()
    sids = [c for c in cols if c.startswith("FXR")]
    out = df.loc[keep, sids].apply(pd.to_numeric, errors="coerce")
    out.index = pd.DatetimeIndex(d[keep])
    return out


def load_rba(paths: Sequence[Path]) -> RateTable:
    paths = tuple(Path(p) for p in paths)
    if not paths:
        raise FileNotFoundError("no RBA F11 files; run 'ibkr-ato fetch-rba'")
    rates = pd.concat([_load_one(p) for p in paths])
    rates = rates[~rates.index.duplicated(keep="last")].sort_index()
    rates.index.name = "date"
    return RateTable(rates, paths)


def load_rba_dir(rba_dir: Path) -> RateTable:
    exts = (".csv", ".xls", ".xlsx")
    paths = sorted(p for p in Path(rba_dir).glob("*") if p.suffix.lower() in exts)
    return load_rba(paths)


def _os_trust_context() -> ssl.SSLContext:
    """TLS context backed by the operating system's certificate store.

    Python ships its own CA bundle; using the OS store means a certificate your machine trusts
    (for example a corporate proxy's) also works here. Verification stays on.
    """
    import truststore

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout, context=_os_trust_context())


def fetch_rba(dest: Path, *, force: bool = False,
              urlopen: Callable = _urlopen) -> tuple[Path, ...]:
    """Download the RBA F11 current CSV and 2018-2022 history into dest."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for name, url in RBA_URLS.items():
        target = dest / name
        if target.exists() and not force:
            continue
        req = urllib.request.Request(url, headers={"User-Agent": "ibkr-ato"})
        with urlopen(req, timeout=60) as resp:
            body = resp.read()
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(body)
        tmp.replace(target)
        written.append(target)
    return tuple(written)
