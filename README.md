# ibkr-ato

Turn Interactive Brokers (IB) Flex Query exports into the figures an Australian resident
individual needs for the ATO tax return: capital gains (item 18), foreign income and the foreign
income tax offset (item 20), and related deductions.

It is built for a personal share portfolio held through IB. It is not tax advice. Check every
figure against your records and the ATO instructions for the year.

## What it does

- Rebuilds a first-in, first-out (FIFO) parcel ledger from IB trades and works out capital gains
  in Australian dollars at RBA daily exchange rates.
- Applies the 50% CGT discount, and current and carried-forward capital losses.
- Sums foreign dividends, withholding tax, interest and fees per financial year (FY).
- Reconciles its results against IB's own closed lots, positions and Change in NAV in the same
  export.
- Writes a self-contained HTML report per FY: the return labels, a per-security breakdown,
  the reconciliation, open items and the assumptions used.
- Marks the report DRAFT until every open item is resolved, then FINAL.

The rules and their ATO sources are in [docs/rules.md](docs/rules.md). Corporate actions are
explained in [docs/corporate-actions.md](docs/corporate-actions.md).

## Install

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```
git clone <this repo> && cd ibkr-ato
uv sync
uv run ibkr-ato --help
```

## Data directory

Your tax data stays outside this repository. Point the tool at it with `--data DIR` or the
`IBKR_ATO_DATA` environment variable.

```
$IBKR_ATO_DATA/
  flex/                  IB Flex Query CSV exports, one per FY
  rba/                   RBA F11 exchange rates (ibkr-ato fetch-rba)
  corporate_actions.csv  corporate actions to apply (ibkr-ato corp-actions drafts it)
  opening_lots.csv       optional: shares acquired outside the IB history
  ibkr-ato.toml          optional: settings (see ibkr-ato.example.toml)
  lodged/FY2025.csv      figures as lodged with the ATO (ibkr-ato lodge)
  out/FY2026/            report.html and CSV detail (generated)
```

`opening_lots.csv` columns: `acq_date,symbol,conid,quantity,cost_total,currency`, with
`cost_total` in the trade currency including brokerage.

## Exporting the IB Flex Query

Create one Activity Flex Query in IB Portal (Performance & Reports > Flex Queries) with these
sections and all their columns:

- Change in NAV
- Statement of Funds
- Open Positions
- Trades, with the Execution and Closed Lots levels of detail
- Transaction Taxes/Fees
- Corporate Actions
- Cash Transactions
- Transfers

Delivery settings: format CSV, date format `yyyyMMdd`, time format `HHmmss`, date/time separator
`;`. Run it for each FY (1 July to 30 June) and save each file in `flex/`. Set IB's tax lot
matching method to FIFO, so IB's closed lots line up with ours.

## Each year

```
export IBKR_ATO_DATA=~/tax/ibkr
uv run ibkr-ato fetch-rba              # refresh RBA exchange rates
uv run ibkr-ato corp-actions           # draft new corporate actions; resolve needs_manual rows
uv run ibkr-ato run                    # latest FY; --fy 2026 or --all-fy for others
open "$IBKR_ATO_DATA/out/FY2026/report.html"
# after you lodge:
uv run ibkr-ato lodge --fy 2026
```

`run` exits with 0 when the FY is FINAL, 2 when it is DRAFT, and 1 on an error (for example a
missing exchange rate or an invalid corporate-action row).

### Why `lodge`

`run` recomputes every year from the raw exports. A rule fix, a confirmed corporate action or a
re-export can change a prior year's figures. `lodge` freezes what you actually lodged. Later
runs show any drift against it as `review/amend`. They also take the lodged 18V (net capital
losses carried forward) as the next year's starting point. `lodge --from summary.csv` imports
figures you lodged before using this tool.

## Report

`out/FY<year>/report.html` contains:

1. Summary of the tax-return labels, in English and Chinese, with lodged values and deltas.
2. Capital gains by security: each parcel with its dates, exchange rates, AUD cost, proceeds and
   discount status, then the walk from gross gains to the net capital gain.
3. Holdings at 30 June.
4. Foreign income by security and country, with each payment.
5. Deductions and fees.
6. Reconciliation with IB.
7. Open items, known gaps, and corporate actions applied.
8. Assumptions and input files.

The same detail is written as CSV next to the report.

## Development

```
uv run pytest                                  # synthetic data only
IBKR_ATO_DATA=... uv run pytest -m golden      # regression against your private baseline
IBKR_ATO_DATA=... IBKR_ATO_UPDATE_GOLDEN=1 uv run pytest -m golden   # accept a change
uv run ruff check
```

The golden tests read and write `$IBKR_ATO_DATA/golden/`. They print private amounts on
failure, so run them only on your own machine.
