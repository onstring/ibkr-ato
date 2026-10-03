"""Per-FY tax-return figures, and snapshots of what was lodged.

Capital gains (item 18): gross gains at 18H; capital losses (current year, then carried
forward) go against gains not eligible for the discount first, then discount gains; the 50%
discount applies to what remains of discount gains; 18A is the result; unused losses are 18V.
The ATO lets you choose the order; this one gives the smallest net capital gain.

Foreign income (item 20): 20E is gross foreign income (withholding tax included); 20O is the
foreign tax paid. Interest on money borrowed to buy shares is a debt deduction: it is not
netted at item 20 but claimed at D15 (foreign shares) or D8 (Australian shares).
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from .config import Settings

Value = float | str


class Label(NamedTuple):
    key: str
    item: str
    label: str
    en: str
    zh: str


LABELS: tuple[Label, ...] = tuple(Label(*x) for x in [
    ("18H", "18", "H", "Total current year capital gains",
     "当年资本利得总额（未抵亏损、未折扣）"),
    ("18A", "18", "A", "Net capital gain", "净资本利得"),
    ("18V", "18", "V", "Net capital losses carried forward to later income years",
     "结转以后年度的净资本亏损"),
    ("cgt_losses_current", "18", "", "Current year capital losses (info)", "当年资本亏损（参考）"),
    ("cgt_carry_in", "18", "", "Net capital losses brought forward (info)",
     "以前年度结转的净资本亏损（参考）"),
    ("cgt_gains_discountable", "18", "", "Gains held 12 months or more, before discount (info)",
     "持有满12个月的盈利，折扣前（参考）"),
    ("cgt_gains_other", "18", "", "Gains held under 12 months (info)",
     "持有不满12个月的盈利（参考）"),
    ("cgt_schedule_required", "18", "",
     "CGT schedule required in myTax (gains or losses over $10,000)", "是否需提交 CGT schedule"),
    ("20E", "20", "E", "Assessable foreign source income", "应税境外来源收入"),
    ("20M", "20", "M", "Other net foreign source income", "其他境外来源净收入"),
    ("20O", "20", "O", "Foreign income tax offset", "境外税收抵免"),
    ("fito_over_1000", "20", "", "FITO over $1,000: compute the FITO limit",
     "FITO 超过 $1,000，需计算上限"),
    ("foreign_dividends", "20", "", "Foreign dividends, gross (info)", "境外股息税前金额（参考）"),
    ("return_of_capital", "20", "", "Return of capital paid as dividend (check treatment)",
     "以股息形式支付的资本返还（需确认处理方式）"),
    ("payment_in_lieu", "20", "", "Payment in lieu of dividends (check treatment)",
     "替代股息（需确认处理方式）"),
    ("interest_received", "", "", "IB credit interest received", "IB 利息收入"),
    ("fees_foreign", "20", "", "Fees on foreign shares, e.g. ADR fees (check deductibility)",
     "境外股票相关费用，如 ADR 费（需确认能否扣除）"),
    ("fees_unallocated", "", "", "Other IB fees, e.g. market data (check deductibility)",
     "其他 IB 费用，如行情费（需确认能否扣除）"),
    ("fees_au", "D8", "", "Fees on Australian shares (check deductibility)",
     "澳股相关费用（需确认能否扣除）"),
    ("margin_interest_paid", "", "", "Margin interest paid (total)", "融资利息支出（总额）"),
    ("D15_margin_interest_foreign", "D15", "",
     "Margin interest for foreign shares (myTax Other deductions)",
     "用于境外股票的融资利息（myTax 其他扣除）"),
    ("D8_margin_interest_au", "D8", "", "Margin interest for Australian shares",
     "用于澳股的融资利息"),
    ("au_dividends_cash", "11", "", "Australian dividends cash (franking from company statements)",
     "澳洲股息现金额（franking 以公司股息单为准）"),
    ("au_interest", "10", "L", "Gross interest (if IB interest treated as Australian)",
     "利息总额（若 IB 利息按澳洲来源）"),
])
LABEL_BY_KEY = {lb.key: lb for lb in LABELS}


@dataclass(frozen=True)
class FySummary:
    fy: int
    values: Mapping[str, Value]
    applied_to_other: float
    applied_to_discount: float
    carry_in_source: str  # "lodged FY2025", "computed", "prior_losses"


def cgt_for_fy(parcels: pd.DataFrame, fy: int, carry_in: float) -> dict[str, float]:
    p = parcels[parcels["fy"] == fy] if not parcels.empty else parcels
    if p.empty:
        disc = other = losses = 0.0
    else:
        g, eligible = p["gain_aud"].astype(float), p["discount_eligible"].astype(bool)
        disc = float(g[(g > 0) & eligible].sum())
        other = float(g[(g > 0) & ~eligible].sum())
        losses = float(-g[g < 0].sum())
    avail = losses + carry_in
    use_o = min(avail, other)
    avail -= use_o
    use_d = min(avail, disc)
    avail -= use_d
    return {
        "18H": disc + other, "18A": (other - use_o) + (disc - use_d) * 0.5, "18V": avail,
        "cgt_losses_current": losses, "cgt_carry_in": carry_in,
        "cgt_gains_discountable": disc, "cgt_gains_other": other,
        "cgt_schedule_required": "YES" if max(disc + other, losses) > 10000 else "no",
        "_use_o": use_o, "_use_d": use_d,
    }


def income_for_fy(income: pd.DataFrame, fy: int, s: Settings) -> dict[str, Value]:
    c = income[income["fy"] == fy] if not income.empty else income

    def total(cat: str, source: str | None = None, scope: str | None = None) -> float:
        if c.empty:
            return 0.0
        m = c["cat"] == cat
        if source:
            m &= c["source"] == source
        if scope:
            m &= c["fee_scope"] == scope
        return float(c.loc[m, "aud"].sum())

    div_f, pil_f, roc_f = total("div", "foreign"), total("pil", "foreign"), total("roc", "foreign")
    int_r, int_p = total("int_recv"), -total("int_paid")
    wht_f = -total("wht", "foreign")
    fees_f = -total("fee", scope="foreign")
    e20 = div_f + pil_f + (roc_f if s.return_of_capital == "dividend" else 0.0)
    e20 += int_r if s.interest_source == "foreign" else 0.0
    return {
        "20E": e20, "20M": e20 - (fees_f if s.deduct_foreign_fees else 0.0), "20O": wht_f,
        "fito_over_1000": "YES" if wht_f > 1000 else "no",
        "foreign_dividends": div_f, "return_of_capital": roc_f + total("roc", "au"),
        "payment_in_lieu": pil_f, "interest_received": int_r,
        "fees_foreign": fees_f, "fees_unallocated": -total("fee", scope="unallocated"),
        "fees_au": -total("fee", scope="au"),
        "margin_interest_paid": int_p,
        "D15_margin_interest_foreign": int_p * s.margin_foreign_share,
        "D8_margin_interest_au": int_p * (1 - s.margin_foreign_share),
        "au_dividends_cash": total("div", "au"),
        "au_interest": int_r if s.interest_source == "au" else 0.0,
    }


def fys_in(parcels: pd.DataFrame, income: pd.DataFrame) -> list[int]:
    fys: set[int] = set()
    for df in (parcels, income):
        if not df.empty:
            fys |= set(df["fy"].dropna().astype(int))
    return sorted(fys)


def build_summaries(parcels: pd.DataFrame, income: pd.DataFrame, s: Settings,
                    lodged: Mapping[int, Mapping[str, Value]]) -> dict[int, FySummary]:
    out: dict[int, FySummary] = {}
    carry, source = float(s.prior_losses), "prior_losses"
    for fy in fys_in(parcels, income):
        prev = lodged.get(fy - 1, {})
        if s.carry_in_from_lodged and isinstance(prev.get("18V"), float):
            carry, source = float(prev["18V"]), f"lodged FY{fy - 1}"
        cgt = cgt_for_fy(parcels, fy, carry)
        values = {k: v for k, v in cgt.items() if not k.startswith("_")}
        values.update(income_for_fy(income, fy, s))
        out[fy] = FySummary(fy, values, cgt["_use_o"], cgt["_use_d"], source)
        carry, source = float(cgt["18V"]), "computed"
    return out


def _round(v: Value) -> Value:
    return round(v, 2) + 0.0 if isinstance(v, float) else v


def summary_frame(summaries: Mapping[int, FySummary]) -> pd.DataFrame:
    rows = [{"fy": f"FY{fy}", "item": lb.item, "label": lb.label, "key": lb.key,
             "description_en": lb.en, "description_zh": lb.zh,
             "value": _round(s.values.get(lb.key, 0.0))}
            for fy, s in summaries.items() for lb in LABELS]
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ lodged snapshots


def _parse(v: str) -> Value:
    try:
        return float(v)
    except ValueError:
        return v


def load_lodged(path: Path) -> dict[str, Value]:
    with open(path, newline="", encoding="utf-8") as f:
        return {r["key"]: _parse(r["value"]) for r in csv.DictReader(f)}


def load_lodged_dir(lodged_dir: Path) -> dict[int, dict[str, Value]]:
    out = {}
    for p in sorted(Path(lodged_dir).glob("FY*.csv")):
        try:
            out[int(p.stem[2:])] = load_lodged(p)
        except ValueError as e:
            raise ValueError(f"{p}: not a lodged snapshot ({e})") from e
    return out


def write_lodged(values: Mapping[str, Value], path: Path, source: str, note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["key", "item", "label", "description_en", "value", "lodged_on", "source",
                    "note"])
        for key, v in values.items():
            lb = LABEL_BY_KEY.get(key, Label(key, "", "", key, ""))
            w.writerow([key, lb.item, lb.label, lb.en, _round(v), date.today().isoformat(),
                        source, note])


def summary_values_from_csv(path: Path, fy: int) -> dict[str, Value]:
    """Read one FY from a summary CSV (ours or the legacy ato_summary.csv)."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = df[df["fy"] == f"FY{fy}"]
    if df.empty:
        raise ValueError(f"{path}: no rows for FY{fy}")
    return {k: _parse(v) for k, v in zip(df["key"], df["value"], strict=True)}


class DeltaRow(NamedTuple):
    key: str
    en: str
    computed: Value
    lodged: Value
    delta: float | None
    flag: str


DELTA_THRESHOLD = 1.0  # the return is in whole dollars


def lodged_delta(values: Mapping[str, Value], lodged: Mapping[str, Value]) -> tuple[DeltaRow, ...]:
    rows = []
    for key in lodged:
        if key not in LABEL_BY_KEY:
            continue
        c, lo = values.get(key, 0.0), lodged[key]
        if isinstance(c, float) and isinstance(lo, float):
            d = c - lo
            flag = "review/amend" if abs(d) >= DELTA_THRESHOLD else "ok"
        else:
            d, flag = None, "ok" if str(c) == str(lo) else "review/amend"
        rows.append(DeltaRow(key, LABEL_BY_KEY[key].en, c, lo, d, flag))
    return tuple(rows)
