#!/usr/bin/env python3
"""
ib_ato.py

Compute ATO-basis figures from Interactive Brokers Activity Flex Query CSVs
and RBA F11 daily exchange rates.

Usage:
  python ib_ato.py --flex flex/*.csv --rba f11/*.csv [options]

Options:
  --fy 2025                     Print only this financial year (FY2025 = 1 Jul 2024 to 30 Jun 2025)
  --prior-losses 0              Net capital losses carried into the earliest FY processed (AUD)
  --opening-lots opening.csv    Lots transferred in or bought outside the Flex history
                                columns: acq_date,symbol,conid,quantity,cost_total,currency
  --splits splits.csv           Share splits / consolidations
                                columns: date,symbol,conid,ratio   (2-for-1 split: ratio=2)
  --margin-foreign-share 1.0    Share of margin interest used to buy foreign shares (0 to 1)
  --interest-source foreign     Treat IB credit interest as 'foreign' (item 20) or 'au' (item 10)
  --out out                     Output directory

Requirements: pandas, numpy (xlrd only if you pass .xls RBA files).

Lot matching is FIFO, rebuilt from EXECUTION rows. Set IB's lot matching
method to FIFO, or expect differences from IB's Realised P&L.

Not tax advice. Check the output against your records and the ATO
instructions for the year.
"""
import argparse
import csv
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd

# RBA F11 Series IDs. Check the "Series ID" row in your F11 file and edit if needed.
RBA_SERIES = {
    "USD": "FXRUSD",
    "HKD": "FXRHKD",
    "EUR": "FXREUR",
    "GBP": "FXRUKPS",
    "JPY": "FXRJY",
    "CNY": "FXRCR",
    "SGD": "FXRSD",
    "NZD": "FXRNZD",
}
FX_TOLERANCE_DAYS = 7  # max gap to the previous RBA business day
EPS = 1e-9
WARNINGS = []


def warn(kind, message, **ctx):
    WARNINGS.append({"kind": kind, "message": message, **ctx})


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# ---------------------------------------------------------------- Flex parsing
KNOWN_COLS = {norm(c) for c in [
    "ClientAccountID", "AccountId", "CurrencyPrimary", "Currency", "AssetClass",
    "Symbol", "Description", "Conid", "ISIN", "TradeID", "TransactionID",
    "TradeDate", "DateTime", "Quantity", "TradePrice", "Proceeds", "IBCommission",
    "Type", "Amount", "LevelOfDetail", "ReportDate", "SettleDate", "ActionID",
]}
MARKERS = {"BOF", "EOF", "BOA", "EOA", "BOS", "EOS", "MSG"}


def _flush(tables, header, rows):
    if header is not None and rows:
        df = pd.DataFrame(rows, columns=header)
        df = df.loc[:, ~pd.Index(df.columns).duplicated()]
        tables.append(df)


def read_flex(path):
    """Split one Flex CSV (single or multi-section, with or without
    header/trailer records) into a list of DataFrames."""
    tables, header, rows = [], None, []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.reader(f):
            if not r or not any(c.strip() for c in r):
                continue
            tag = r[0].strip().upper()
            if tag in MARKERS:
                continue
            if tag in ("HEADER", "DATA"):
                r = r[2:]
            cells = [norm(c) for c in r]
            if tag != "DATA" and sum(c in KNOWN_COLS for c in cells) >= 3:
                _flush(tables, header, rows)
                header, rows = cells, []
                continue
            if header is None:
                continue
            r = (r + [""] * len(header))[:len(header)]
            rows.append([c.strip() for c in r])
    _flush(tables, header, rows)
    return tables


def classify(df):
    c = set(df.columns)
    if "tradeprice" in c and "quantity" in c:
        return "trades"
    if "type" in c and "amount" in c and "quantity" not in c:
        return "cash"
    if "direction" in c:
        return "transfers"
    if "type" in c and "quantity" in c:
        return "corporate_actions"
    return "other"


def get(df, *names):
    for n in names:
        k = norm(n)
        if k in df.columns:
            return df[k].fillna("").astype(str).str.strip()
    return pd.Series([""] * len(df), index=df.index, dtype=object)


def num(s):
    s = s.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(s, errors="coerce").fillna(0.0)


def to_date(s):
    ext = s.astype(str).str.extract(r"(\d{4})-?(\d{2})-?(\d{2})")
    return pd.to_datetime(ext[0] + ext[1] + ext[2], format="%Y%m%d", errors="coerce")


def fy_of(d):
    return (d.dt.year + (d.dt.month >= 7).astype(int)).astype("Int64")


def dedupe(df, id_col):
    has = df[id_col] != ""
    return pd.concat([df[has].drop_duplicates(id_col), df[~has].drop_duplicates()])


# ---------------------------------------------------------------- RBA F11
def _read_rows(path):
    p = Path(path)
    if p.suffix.lower() in (".xls", ".xlsx"):
        raw = pd.read_excel(p, header=None, dtype=object)
        return [["" if pd.isna(v) else str(v) for v in row]
                for row in raw.itertuples(index=False)]
    with open(p, newline="", encoding="latin-1") as f:
        return list(csv.reader(f))


def load_rba(paths):
    frames = []
    for p in paths:
        rows = _read_rows(p)
        hdr = next((i for i, r in enumerate(rows) if r and r[0].strip() == "Series ID"), None)
        if hdr is None:
            sys.exit(f"{p}: no 'Series ID' row; is this an RBA F11 file?")
        cols = [c.strip() for c in rows[hdr]]
        data = [(r + [""] * len(cols))[:len(cols)] for r in rows[hdr + 1:] if r]
        df = pd.DataFrame(data, columns=cols)
        first = df[cols[0]].astype(str).str.strip()
        d = pd.to_datetime(first, format="%d-%b-%Y", errors="coerce")
        d = d.fillna(pd.to_datetime(first.str[:10], format="%Y-%m-%d", errors="coerce"))
        df = df[d.notna()].copy()
        df.index = d[d.notna()]
        sid_cols = [c for c in cols if c.startswith("FXR")]
        frames.append(df[sid_cols].apply(pd.to_numeric, errors="coerce"))
    rates = pd.concat(frames)
    rates = rates[~rates.index.duplicated(keep="last")].sort_index()
    rates.index.name = "d"
    return rates


def fx_lookup(rates, cur, dates, context):
    """Foreign units per AUD 1 on the date, or the previous RBA business day."""
    out = pd.Series(np.nan, index=cur.index)
    for c in cur.dropna().unique():
        m = (cur == c) & dates.notna()
        if not m.any():
            continue
        if c == "AUD":
            out.loc[m] = 1.0
            continue
        sid = RBA_SERIES.get(c)
        if sid is None or sid not in rates.columns:
            warn("fx_missing_series", f"No RBA series for {c}; edit RBA_SERIES",
                 context=context, currency=c, rows=int(m.sum()))
            continue
        s = rates[sid].dropna()
        d = dates[m].values
        idx = s.index.searchsorted(d, side="right") - 1
        vals = np.full(len(d), np.nan)
        ok = idx >= 0
        if ok.any():
            found = s.index.values[idx[ok]]
            gap = (d[ok] - found) / np.timedelta64(1, "D")
            v = s.values[idx[ok]]
            vals[ok] = np.where(gap <= FX_TOLERANCE_DAYS, v, np.nan)
        out.loc[m] = vals
        bad = np.isnan(vals)
        if bad.any():
            bd = pd.to_datetime(d[bad])
            warn("fx_missing_rate", f"No RBA {c} rate within {FX_TOLERANCE_DAYS} days",
                 context=context, currency=c, rows=int(bad.sum()),
                 first=str(bd.min().date()), last=str(bd.max().date()))
    return out


# ---------------------------------------------------------------- trades
def prep_trades(tr, rates):
    df = pd.DataFrame(index=tr.index)
    df["tid"] = get(tr, "TradeID")
    df["level"] = get(tr, "LevelOfDetail").str.upper()
    df["asset"] = get(tr, "AssetClass").str.upper()
    df["symbol"] = get(tr, "Symbol")
    df["conid"] = get(tr, "Conid")
    df["isin"] = get(tr, "ISIN")
    df["cur"] = get(tr, "CurrencyPrimary", "Currency").str.upper()
    df["commcur"] = get(tr, "IBCommissionCurrency").str.upper()
    raw_dt = get(tr, "DateTime")
    td = get(tr, "TradeDate")
    df["date"] = to_date(td.where(td != "", raw_dt))
    df["sortkey"] = raw_dt.where(raw_dt != "", td)
    df["qty"] = num(get(tr, "Quantity"))
    df["price"] = num(get(tr, "TradePrice"))
    df["mult"] = num(get(tr, "Multiplier")).replace(0, 1)
    df["proceeds"] = num(get(tr, "Proceeds"))
    df["comm"] = num(get(tr, "IBCommission"))
    df["taxes"] = num(get(tr, "Taxes"))

    df = df[df["level"].isin(["EXECUTION", ""])]
    df = dedupe(df, "tid")

    nodate = df["date"].isna()
    for r in df[nodate].itertuples():
        warn("trade_no_date", "Unparseable trade date; set Flex date format to yyyyMMdd",
             symbol=r.symbol, tid=r.tid)
    df = df[~nodate].copy()

    fx_trades = df[df["asset"] == "CASH"].copy()
    other = df[~df["asset"].isin(["STK", "CASH", ""])]
    for a, g in other.groupby("asset"):
        warn("asset_not_handled", f"Asset class {a} excluded from CGT; handle manually",
             rows=len(g), symbols=",".join(sorted(g["symbol"].unique())[:20]))
    df = df[df["asset"].isin(["STK", ""])].copy()

    df["key"] = df["conid"].where(df["conid"] != "", df["symbol"])
    df["commcur"] = df["commcur"].where(df["commcur"] != "", df["cur"])
    zero = (df["proceeds"] == 0) & (df["qty"] != 0)
    df.loc[zero, "proceeds"] = -df.loc[zero, "qty"] * df.loc[zero, "price"] * df.loc[zero, "mult"]

    df["fx"] = fx_lookup(rates, df["cur"], df["date"], "trades")
    df["fx_comm"] = fx_lookup(rates, df["commcur"], df["date"], "trades_commission")
    df["net_aud"] = (df["proceeds"] + df["taxes"]) / df["fx"] + df["comm"] / df["fx_comm"]
    same = df["commcur"] == df["cur"]
    df["net_fc"] = np.where(same, df["proceeds"] + df["comm"] + df["taxes"], np.nan)
    return df, fx_trades


def load_opening(path, rates, sym2key):
    o = pd.read_csv(path, dtype=str).fillna("")
    o.columns = [norm(c) for c in o.columns]
    df = pd.DataFrame(index=o.index)
    df["date"] = to_date(get(o, "acqdate"))
    df["symbol"] = get(o, "symbol")
    conid = get(o, "conid")
    df["key"] = [c if c else sym2key.get(s, s) for c, s in zip(conid, df["symbol"])]
    df["qty"] = num(get(o, "quantity"))
    df["cost"] = num(get(o, "costtotal"))
    df["cur"] = get(o, "currency").str.upper()
    df["fx"] = fx_lookup(rates, df["cur"], df["date"], "opening_lots")
    df["cost_aud"] = df["cost"] / df["fx"]
    return df


def load_splits(path, sym2key):
    s = pd.read_csv(path, dtype=str).fillna("")
    s.columns = [norm(c) for c in s.columns]
    df = pd.DataFrame(index=s.index)
    df["date"] = to_date(get(s, "date"))
    df["symbol"] = get(s, "symbol")
    conid = get(s, "conid")
    df["key"] = [c if c else sym2key.get(x, x) for c, x in zip(conid, df["symbol"])]
    df["ratio"] = num(get(s, "ratio"))
    return df


def run_ledger(tr, opening, splits):
    events = []
    for r in opening.itertuples():
        events.append((r.date, 0, "", 0, "open", r))
    for r in splits.itertuples():
        events.append((r.date, 1, "", 0, "split", r))
    for r in tr.itertuples():
        events.append((r.date, 2, r.sortkey, 0 if r.qty > 0 else 1, "trade", r))
    events.sort(key=lambda e: e[:4])

    lots = defaultdict(deque)
    info = {}
    parcels = []
    for _, _, _, _, kind, r in events:
        if kind == "open":
            lots[r.key].append(dict(acq=r.date, qty=r.qty, aud_ps=r.cost_aud / r.qty,
                                    fc_ps=r.cost / r.qty, fx=r.fx, src="opening_lots"))
            info.setdefault(r.key, (r.symbol, "", r.cur))
            continue
        if kind == "split":
            for lot in lots[r.key]:
                lot["qty"] *= r.ratio
                lot["aud_ps"] /= r.ratio
                lot["fc_ps"] /= r.ratio
            continue
        info[r.key] = (r.symbol, r.isin, r.cur)
        if r.qty > EPS:
            lots[r.key].append(dict(acq=r.date, qty=r.qty, aud_ps=-r.net_aud / r.qty,
                                    fc_ps=-r.net_fc / r.qty, fx=r.fx, src=f"trade {r.tid}"))
        elif r.qty < -EPS:
            remain = -r.qty
            p_aud_ps = r.net_aud / remain
            p_fc_ps = r.net_fc / remain
            while remain > EPS and lots[r.key]:
                lot = lots[r.key][0]
                take = min(remain, lot["qty"])
                cost_aud = take * lot["aud_ps"]
                proc_aud = take * p_aud_ps
                parcels.append(dict(
                    symbol=r.symbol, isin=r.isin, currency=r.cur,
                    acq_date=lot["acq"].date(), disp_date=r.date.date(), quantity=take,
                    cost_fc=take * lot["fc_ps"], proceeds_fc=take * p_fc_ps,
                    fx_acq=lot["fx"], fx_disp=r.fx,
                    cost_aud=cost_aud, proceeds_aud=proc_aud, gain_aud=proc_aud - cost_aud,
                    discount_eligible=bool(r.date > lot["acq"] + pd.DateOffset(years=1)),
                    acq_source=lot["src"], disp_trade=r.tid,
                ))
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= EPS:
                    lots[r.key].popleft()
            if remain > EPS:
                warn("unmatched_sell",
                     "Sell exceeds known holdings (missing history, transfer-in or short); "
                     "add to --opening-lots", symbol=r.symbol, date=str(r.date.date()),
                     quantity=remain)

    held = []
    for key, dq in lots.items():
        sym, isin, cur = info.get(key, (key, "", ""))
        for lot in dq:
            if lot["qty"] > EPS:
                held.append(dict(symbol=sym, isin=isin, currency=cur,
                                 acq_date=lot["acq"].date(), quantity=lot["qty"],
                                 cost_fc=lot["qty"] * lot["fc_ps"],
                                 cost_aud=lot["qty"] * lot["aud_ps"], source=lot["src"]))
    p = pd.DataFrame(parcels)
    if not p.empty:
        p["fy"] = fy_of(pd.to_datetime(p["disp_date"]))
    return p, pd.DataFrame(held)


# ---------------------------------------------------------------- cash
def categorise(t):
    t = t.lower()
    if "withholding" in t:
        return "wht"
    if "payment in lieu" in t:
        return "pil"
    if "dividend" in t:
        return "div"
    if "interest" in t and "received" in t:
        return "int_recv"
    if "interest" in t and "paid" in t:
        return "int_paid"
    if "fee" in t:
        return "fee"
    return "other"


def prep_cash(ct, rates, interest_source):
    df = pd.DataFrame(index=ct.index)
    df["tid"] = get(ct, "TransactionID")
    df["type"] = get(ct, "Type")
    df["cur"] = get(ct, "CurrencyPrimary", "Currency").str.upper()
    df["symbol"] = get(ct, "Symbol")
    df["isin"] = get(ct, "ISIN")
    df["description"] = get(ct, "Description")
    df["amount"] = num(get(ct, "Amount"))
    dt, sd, rd = get(ct, "DateTime"), get(ct, "SettleDate"), get(ct, "ReportDate")
    df["date"] = to_date(dt.where(dt != "", sd.where(sd != "", rd)))
    df = dedupe(df, "tid")
    df["cat"] = df["type"].map(categorise)
    df = df[df["cat"] != "other"].copy()
    nodate = df["date"].isna()
    if nodate.any():
        warn("cash_no_date", "Unparseable cash transaction dates", rows=int(nodate.sum()))
    df = df[~nodate].copy()

    df["country"] = df["isin"].str[:2].str.upper()
    src = np.where(df["country"] == "AU", "au", "foreign")
    src = np.where(df["cat"].isin(["int_recv", "int_paid"]), interest_source, src)
    df["source"] = np.where(df["cat"] == "fee", "n/a", src)
    df["fx"] = fx_lookup(rates, df["cur"], df["date"], "cash")
    df["aud"] = df["amount"] / df["fx"]
    df["fy"] = fy_of(df["date"])
    return df


# ---------------------------------------------------------------- summary
LABELS = [
    # key, item, label, English, 中文
    ("18H", "18", "H", "Total current year capital gains", "当年资本利得总额（未抵亏损、未折扣）"),
    ("18A", "18", "A", "Net capital gain", "净资本利得"),
    ("18V", "18", "V", "Net capital losses carried forward to later income years", "结转以后年度的净资本亏损"),
    ("cgt_losses_current", "18", "", "Current year capital losses (info)", "当年资本亏损（参考）"),
    ("cgt_gains_discountable", "18", "", "Gains held 12 months or more, before discount (info)", "持有满12个月的盈利，折扣前（参考）"),
    ("cgt_gains_other", "18", "", "Gains held under 12 months (info)", "持有不满12个月的盈利（参考）"),
    ("cgt_schedule_required", "18", "", "CGT schedule required (gains or losses over $10,000)", "是否需提交 CGT schedule"),
    ("20E", "20", "E", "Assessable foreign source income", "应税境外来源收入"),
    ("20M", "20", "M", "Other net foreign source income", "其他境外来源净收入"),
    ("20O", "20", "O", "Foreign income tax offset", "境外税收抵免"),
    ("fito_over_1000", "20", "", "FITO over $1,000: limit calculation needed", "FITO 超过 $1,000，需计算上限"),
    ("foreign_dividends", "20", "", "Foreign dividends, gross (info)", "境外股息税前金额（参考）"),
    ("payment_in_lieu", "20", "", "Payment in lieu of dividends (check treatment)", "替代股息（需确认处理方式）"),
    ("interest_received", "", "", "IB credit interest received", "IB 利息收入"),
    ("margin_interest_paid", "", "", "Margin interest paid (total)", "融资利息支出（总额）"),
    ("margin_interest_deducted_20", "20", "", "Margin interest deducted against foreign income", "冲减境外收入的融资利息"),
    ("margin_interest_remaining", "D8", "", "Margin interest not deducted at 20 (Australian shares share)", "未在第20题扣除的融资利息（澳股部分）"),
    ("au_dividends_cash", "11", "", "Australian dividends cash (franking from company statements)", "澳洲股息现金额（franking 以公司股息单为准）"),
    ("au_interest", "10", "L", "Gross interest (if IB interest treated as Australian)", "利息总额（若 IB 利息按澳洲来源）"),
    ("fees_other", "", "", "Other IB fees, e.g. market data (check deductibility)", "其他 IB 费用（需确认能否扣除）"),
]


def build_summary(parcels, cash, prior_losses, share, interest_source):
    fys = set()
    if not parcels.empty:
        fys |= set(parcels["fy"].dropna().astype(int))
    if not cash.empty:
        fys |= set(cash["fy"].dropna().astype(int))
    out = {}
    carry = float(prior_losses)
    for fy in sorted(fys):
        r = {}
        if not parcels.empty:
            p = parcels[parcels["fy"] == fy]
            g, disc_m = p["gain_aud"], p["discount_eligible"].astype(bool)
            disc = g[(g > 0) & disc_m].sum()
            other = g[(g > 0) & ~disc_m].sum()
            losses = -g[g < 0].sum()
        else:
            disc = other = losses = 0.0
        avail = losses + carry
        use_o = min(avail, other)
        avail -= use_o
        use_d = min(avail, disc)
        avail -= use_d
        r["18H"] = disc + other
        r["18A"] = (other - use_o) + (disc - use_d) * 0.5
        r["18V"] = avail
        r["cgt_losses_current"] = losses
        r["cgt_gains_discountable"] = disc
        r["cgt_gains_other"] = other
        r["cgt_schedule_required"] = "YES" if max(disc + other, losses) > 10000 else "no"
        carry = avail

        c = cash[cash["fy"] == fy] if not cash.empty else cash

        def s(cat, source=None):
            if c.empty:
                return 0.0
            m = c["cat"] == cat
            if source:
                m &= c["source"] == source
            return float(c.loc[m, "aud"].sum())

        div_f, pil_f = s("div", "foreign"), s("pil", "foreign")
        int_r = s("int_recv")
        int_p = -s("int_paid")
        wht_f = -s("wht", "foreign")
        e = div_f + pil_f + (int_r if interest_source == "foreign" else 0.0)
        ded = int_p * share
        r["20E"] = e
        r["20M"] = e - ded
        r["20O"] = wht_f
        r["fito_over_1000"] = "YES" if wht_f > 1000 else "no"
        r["foreign_dividends"] = div_f
        r["payment_in_lieu"] = pil_f
        r["interest_received"] = int_r
        r["margin_interest_paid"] = int_p
        r["margin_interest_deducted_20"] = ded
        r["margin_interest_remaining"] = int_p - ded
        r["au_dividends_cash"] = s("div", "au")
        r["au_interest"] = int_r if interest_source == "au" else 0.0
        r["fees_other"] = -s("fee")
        out[fy] = r
    return out


def summary_frame(summary):
    rows = []
    for fy, r in summary.items():
        for key, item, label, en, zh in LABELS:
            v = r.get(key, 0.0)
            rows.append(dict(fy=f"FY{fy}", item=item, label=label, key=key,
                             description_en=en, description_zh=zh,
                             value=round(v, 2) + 0.0 if isinstance(v, float) else v))
    return pd.DataFrame(rows)


def print_summary(summary, only_fy):
    for fy, r in summary.items():
        if only_fy and fy != only_fy:
            continue
        print(f"\n=== FY{fy}  (1 Jul {fy - 1} to 30 Jun {fy}) ===")
        for key, item, label, en, zh in LABELS:
            v = r.get(key, 0.0)
            if isinstance(v, float):
                v = round(v, 2) + 0.0
            tag = f"{item}{label}" if label else (item or "-")
            val = f"{v:>14,.2f}" if isinstance(v, float) else f"{v:>14}"
            print(f"  {tag:<5} {val}  {en} / {zh}")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flex", nargs="+", required=True)
    ap.add_argument("--rba", nargs="+", required=True)
    ap.add_argument("--fy", type=int)
    ap.add_argument("--prior-losses", type=float, default=0.0)
    ap.add_argument("--opening-lots")
    ap.add_argument("--splits")
    ap.add_argument("--margin-foreign-share", type=float, default=1.0)
    ap.add_argument("--interest-source", choices=["foreign", "au"], default="foreign")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    if not 0 <= a.margin_foreign_share <= 1:
        sys.exit("--margin-foreign-share must be between 0 and 1")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rates = load_rba(a.rba)
    print(f"RBA rates: {rates.index.min().date()} to {rates.index.max().date()}, "
          f"series {', '.join(rates.columns)}")

    buckets = defaultdict(list)
    for f in a.flex:
        for t in read_flex(f):
            buckets[classify(t)].append(t)
    print("Flex sections found: " + ", ".join(f"{k}={sum(len(t) for t in v)} rows"
                                             for k, v in buckets.items()))
    if not buckets["trades"] and not buckets["cash"]:
        sys.exit("No Trades or Cash Transactions found in Flex files")

    parcels, held, cash = pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    if buckets["trades"]:
        tr, fx_trades = prep_trades(pd.concat(buckets["trades"], ignore_index=True), rates)
        sym2key = dict(zip(tr["symbol"], tr["key"]))
        empty = pd.DataFrame(columns=["date", "key", "symbol", "qty", "cost", "cost_aud",
                                      "cur", "fx", "ratio"])
        opening = load_opening(a.opening_lots, rates, sym2key) if a.opening_lots else empty
        splits = load_splits(a.splits, sym2key) if a.splits else empty
        parcels, held = run_ledger(tr, opening, splits)
        if not fx_trades.empty:
            fx_trades.drop(columns=["key"], errors="ignore").to_csv(out / "fx_conversions.csv", index=False)
    if buckets["cash"]:
        cash = prep_cash(pd.concat(buckets["cash"], ignore_index=True), rates, a.interest_source)

    summary = build_summary(parcels, cash, a.prior_losses, a.margin_foreign_share, a.interest_source)

    if not parcels.empty:
        parcels.sort_values(["fy", "disp_date", "symbol"]).to_csv(out / "cgt_parcels.csv", index=False)
    if not held.empty:
        held.sort_values(["symbol", "acq_date"]).to_csv(out / "open_lots_end.csv", index=False)
    if not cash.empty:
        cols = ["fy", "date", "cat", "source", "type", "symbol", "isin", "country",
                "cur", "amount", "fx", "aud", "description", "tid"]
        cash.sort_values(["fy", "date"])[cols].to_csv(out / "income_detail.csv", index=False)
        dv = cash[cash["cat"].isin(["div", "pil", "wht"])]
        if not dv.empty:
            by = dv.pivot_table(index=["fy", "country"], columns="cat", values="aud",
                                aggfunc="sum", fill_value=0).reset_index()
            by.to_csv(out / "dividends_by_country.csv", index=False)
    for k in ("corporate_actions", "transfers"):
        if buckets[k]:
            pd.concat(buckets[k], ignore_index=True).to_csv(out / f"review_{k}.csv", index=False)
            warn("review", f"{k} present: check {out / f'review_{k}.csv'} and add "
                 "--splits / --opening-lots entries where needed")
    summary_frame(summary).to_csv(out / "ato_summary.csv", index=False)
    if WARNINGS:
        pd.DataFrame(WARNINGS).to_csv(out / "warnings.csv", index=False)

    print_summary(summary, a.fy)
    print(f"\nAssumptions: FIFO; margin interest share against foreign income = "
          f"{a.margin_foreign_share}; IB interest source = {a.interest_source}; "
          f"prior losses = {a.prior_losses:,.2f}")
    print(f"Output: {out.resolve()}")
    if WARNINGS:
        print(f"WARNINGS: {len(WARNINGS)} (see warnings.csv)")
        for w in WARNINGS[:10]:
            print("  -", w["kind"], ":", w["message"])


if __name__ == "__main__":
    main()
