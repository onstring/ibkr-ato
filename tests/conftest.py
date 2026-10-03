"""Builders for tiny synthetic Flex and RBA files. No personal data."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from pathlib import Path

import pytest

from ibkr_ato.config import DataDir

COMMON = ["ClientAccountID", "CurrencyPrimary", "FXRateToBase", "AssetClass", "Symbol",
          "Description", "Conid", "ISIN", "LevelOfDetail"]
HEADERS = {
    "nav": ["ClientAccountID", "CurrencyPrimary", "FromDate", "ToDate", "StartingValue",
            "Dividends", "WithholdingTax", "Interest", "OtherFees", "Commissions",
            "ForexCommissions", "TransactionTax", "CorporateActionProceeds", "EndingValue"],
    "positions": COMMON + ["ReportDate", "Quantity", "CostBasisMoney", "OpenDateTime"],
    "trades": COMMON + ["TradeID", "DateTime", "TradeDate", "Quantity", "TradePrice",
                        "Proceeds", "Taxes", "IBCommission", "IBCommissionCurrency",
                        "CostBasis", "FifoPnlRealized", "Notes/Codes", "OpenDateTime",
                        "Multiplier"],
    "corp_actions": COMMON + ["Report Date", "Date/Time", "ActionDescription", "Amount",
                              "Proceeds", "Quantity", "FifoPnlRealized", "Type",
                              "TransactionID", "ActionID"],
    "cash": COMMON + ["Date/Time", "SettleDate", "Amount", "Type", "DividendType",
                      "TransactionID", "ReportDate"],
}
FY_START, FY_END = date(2024, 7, 1), date(2026, 6, 30)


def write_flex(path: Path, **sections: Iterable[Mapping[str, object]]) -> Path:
    """Write a multi-section Flex CSV. Unspecified columns are blank."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        for name, rows in sections.items():
            header = HEADERS[name]
            w.writerow(header)
            for r in rows:
                unknown = set(r) - set(header)
                assert not unknown, f"{name}: unknown columns {unknown}"
                w.writerow([r.get(c, "") for c in header])
    return path


def trade(symbol, conid, day, qty, price, cur="USD", comm=-1.0, tid=None, isin="US0000000001",
          taxes=0.0, level="EXECUTION", fx_ib=1.5, **extra):
    d = day.strftime("%Y%m%d")
    proceeds = -qty * price if level == "EXECUTION" else ""
    return {"ClientAccountID": "U0000000", "CurrencyPrimary": cur, "FXRateToBase": fx_ib,
            "AssetClass": "STK", "Symbol": symbol, "Conid": conid, "ISIN": isin,
            "LevelOfDetail": level, "TradeID": tid or f"T{symbol}{d}{qty}",
            "DateTime": f"{d};100000", "TradeDate": d, "Quantity": qty, "TradePrice": price,
            "Proceeds": proceeds, "Taxes": taxes, "IBCommission": comm if level == "EXECUTION"
            else "", "IBCommissionCurrency": cur, "Multiplier": 1, **extra}


def closed_lot(symbol, conid, disp, acq, qty, cost, pnl, cur="USD", term="ST"):
    return {"ClientAccountID": "U0000000", "CurrencyPrimary": cur, "AssetClass": "STK",
            "Symbol": symbol, "Conid": conid, "LevelOfDetail": "CLOSED_LOT",
            "TradeDate": disp.strftime("%Y%m%d"), "DateTime": disp.strftime("%Y%m%d;100000"),
            "OpenDateTime": acq.strftime("%Y%m%d;093000"), "Quantity": qty, "CostBasis": cost,
            "FifoPnlRealized": pnl, "Notes/Codes": term, "TradePrice": 0}


def cash(ib_type, amount, day, symbol="AAA", conid="1", isin="US0000000001", cur="USD",
         divtype="", tid=None, fx_ib=1.5, description=""):
    return {"ClientAccountID": "U0000000", "CurrencyPrimary": cur, "FXRateToBase": fx_ib,
            "AssetClass": "STK" if symbol else "", "Symbol": symbol, "Conid": conid,
            "ISIN": isin, "LevelOfDetail": "DETAIL", "Date/Time": day.strftime("%Y%m%d"),
            "Amount": amount, "Type": ib_type, "DividendType": divtype,
            "TransactionID": tid or f"C{ib_type[:3]}{day:%Y%m%d}{amount}",
            "Description": description or f"{symbol} {ib_type}"}


def position(symbol, conid, as_of, qty, cost, cur="USD"):
    return {"ClientAccountID": "U0000000", "CurrencyPrimary": cur, "AssetClass": "STK",
            "Symbol": symbol, "Conid": conid, "LevelOfDetail": "SUMMARY",
            "ReportDate": as_of.strftime("%Y%m%d"), "Quantity": qty, "CostBasisMoney": cost}


def nav(from_d, to_d, **values):
    row = {"ClientAccountID": "U0000000", "CurrencyPrimary": "AUD",
           "FromDate": from_d.strftime("%Y%m%d"), "ToDate": to_d.strftime("%Y%m%d"),
           "EndingValue": 0}
    row.update(values)
    return row


def corp_action(type_code, description, qty, day, symbol, conid, cur="HKD", proceeds=0.0,
                action_id="A1", tid=None):
    return {"ClientAccountID": "U0000000", "CurrencyPrimary": cur, "AssetClass": "STK",
            "Symbol": symbol, "Conid": conid, "LevelOfDetail": "DETAIL",
            "Report Date": day.strftime("%Y%m%d"), "Date/Time": day.strftime("%Y%m%d;112500"),
            "ActionDescription": description, "Quantity": qty, "Proceeds": proceeds,
            "Type": type_code, "ActionID": action_id, "TransactionID": tid or f"{action_id}{conid}"}


def write_rba(path: Path, rates: Mapping[date, Mapping[str, float]]) -> Path:
    """RBA F11 layout: BOM, metadata rows, Series ID row, DD-Mon-YYYY dates."""
    series = {"USD": "FXRUSD", "HKD": "FXRHKD"}
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["F11.1  EXCHANGE RATES"])
        w.writerow(["Title", "A$1=USD", "A$1=HKD"])
        for label in ("Description", "Frequency", "Type", "Units", "", "", "Source",
                      "Publication date"):
            w.writerow([label, "", ""])
        w.writerow(["Series ID", *series.values()])
        for d in sorted(rates):
            w.writerow([d.strftime("%d-%b-%Y"), *(rates[d].get(c, "") for c in series)])
    return path


def business_days(start: date, end: date, skip: Iterable[date] = ()):
    skip, d = set(skip), start
    while d <= end:
        if d.weekday() < 5 and d not in skip:
            yield d
        d += timedelta(days=1)


def flat_rates(start=FY_START - timedelta(days=800), end=FY_END, usd=0.5, hkd=4.0, skip=()):
    """USD 0.5 and HKD 4.0 per AUD on every business day: 1 USD = 2 AUD, 1 HKD = 0.25 AUD."""
    return {d: {"USD": usd, "HKD": hkd} for d in business_days(start, end, skip)}


@pytest.fixture
def data_dir(tmp_path) -> DataDir:
    d = DataDir.at(tmp_path / "data")
    d.flex.mkdir(parents=True)
    d.rba.mkdir()
    write_rba(d.rba / "f11.csv", flat_rates())
    return d
