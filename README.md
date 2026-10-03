# ibkr-ato

Turn Interactive Brokers (IB) Flex Query exports into the figures an Australian resident
individual needs for the ATO tax return: capital gains (item 18), foreign income and foreign
income tax offset (item 20), and related deductions.

It is built for a personal share portfolio held through IB. It is not tax advice. Check every
figure against your records and the ATO instructions for the year.

## What it does

- Rebuilds a first-in, first-out (FIFO) parcel ledger from IB trades and works out capital gains
  in Australian dollars at RBA daily exchange rates.
- Applies the 50% CGT discount and current and carried-forward capital losses.
- Sums foreign dividends, withholding tax, interest and fees per financial year.
- Reconciles its results against IB's own figures in the same export.
- Writes a self-contained HTML report per financial year with a per-security breakdown.

## Data directory

Your tax data stays outside this repository. Point the tool at it with `--data` or the
`IBKR_ATO_DATA` environment variable.

```
$IBKR_ATO_DATA/
  flex/                  IB Flex Query CSV exports, one per financial year
  rba/                   RBA F11 exchange-rate files (ibkr-ato fetch-rba downloads them)
  corporate_actions.csv  resolved corporate actions (ibkr-ato corp-actions drafts it)
  opening_lots.csv       optional: shares acquired outside the IB history
  ibkr-ato.toml          optional: defaults for this data set
  lodged/FY2025.csv      figures as lodged with the ATO (ibkr-ato lodge)
  out/FY2026/            generated report.html and CSV detail
```

## Exporting the IB Flex Query

Create one Activity Flex Query in IB Account Management (Performance & Reports > Flex Queries)
with these sections, all columns selected:

- Change in NAV
- Statement of Funds
- Open Positions
- Trades, with "Execution" and "Closed Lots" levels of detail
- Transaction Taxes/Fees
- Corporate Actions
- Cash Transactions
- Transfers

Settings: format CSV, date format `yyyyMMdd`, time format `HHmmss`, date/time separator `;`.
Run it once per financial year (1 July to 30 June) and save each file in `flex/`.

## Usage

```
uv sync
uv run ibkr-ato --help
```

The full workflow is documented once the commands land.

## Development

```
uv run pytest                     # unit tests, synthetic data only
IBKR_ATO_DATA=... uv run pytest -m golden   # regression test against your private baseline
uv run ruff check
```
