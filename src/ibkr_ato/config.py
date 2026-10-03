"""Data directory layout and run settings.

Settings come from built-in defaults, then ``[defaults]`` in ``ibkr-ato.toml`` inside the data
directory, then command-line flags. Keeping them in a file next to the data makes every run of
the same data reproducible.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Literal

ENV_DATA = "IBKR_ATO_DATA"


@dataclass(frozen=True)
class DataDir:
    root: Path

    @classmethod
    def at(cls, root: Path | str) -> DataDir:
        return cls(Path(root).expanduser())

    @classmethod
    def from_arg_or_env(cls, arg: str | None) -> DataDir:
        value = arg or os.environ.get(ENV_DATA)
        if not value:
            raise ValueError(f"no data directory: pass --data DIR or set {ENV_DATA}")
        d = cls.at(value)
        if not d.root.is_dir():
            raise ValueError(f"data directory not found: {d.root}")
        return d

    flex = property(lambda self: self.root / "flex")
    rba = property(lambda self: self.root / "rba")
    corporate_actions = property(lambda self: self.root / "corporate_actions.csv")
    opening_lots = property(lambda self: self.root / "opening_lots.csv")
    lodged = property(lambda self: self.root / "lodged")
    settings = property(lambda self: self.root / "ibkr-ato.toml")

    def out(self, fy: int) -> Path:
        return self.root / "out" / f"FY{fy}"

    def lodged_file(self, fy: int) -> Path:
        return self.lodged / f"FY{fy}.csv"


@dataclass(frozen=True)
class Settings:
    """Tax-treatment choices. Each one is shown in the report's Assumptions section."""

    prior_losses: float = 0.0
    margin_foreign_share: float = 1.0
    interest_source: Literal["foreign", "au"] = "foreign"
    fx_max_gap_days: int = 7
    carry_in_from_lodged: bool = True
    deduct_foreign_fees: bool = False
    return_of_capital: Literal["dividend", "cost_base"] = "dividend"

    def validate(self) -> Settings:
        if not 0.0 <= self.margin_foreign_share <= 1.0:
            raise ValueError("margin_foreign_share must be between 0 and 1")
        if self.interest_source not in ("foreign", "au"):
            raise ValueError("interest_source must be 'foreign' or 'au'")
        if self.return_of_capital not in ("dividend", "cost_base"):
            raise ValueError("return_of_capital must be 'dividend' or 'cost_base'")
        if self.fx_max_gap_days < 0:
            raise ValueError("fx_max_gap_days must be 0 or more")
        if self.prior_losses < 0:
            raise ValueError("prior_losses must be 0 or more")
        return self


def load_settings(path: Path | None, overrides: Mapping[str, object] | None = None) -> Settings:
    base = Settings()
    known = {f.name for f in fields(Settings)}
    values: dict[str, object] = {}
    if path is not None and path.exists():
        with open(path, "rb") as fh:
            doc = tomllib.load(fh)
        table = doc.get("defaults", {})
        unknown = set(table) - known
        if unknown:
            raise ValueError(f"{path}: unknown settings {sorted(unknown)}")
        values.update(table)
    values.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return replace(base, **values).validate()
