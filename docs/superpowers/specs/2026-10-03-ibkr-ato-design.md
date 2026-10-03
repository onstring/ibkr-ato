# Plan: `ibkr-ato` — IB Flex → ATO individual tax figures, standalone project

## 1. Context

One 611-line script `ib_ato.py` turns Interactive Brokers (IB) Flex Query CSVs plus RBA F11
exchange rates into Australian individual tax-return figures (item 18 CGT, item 20 foreign income,
margin interest, fees). It works, but:

- Not a project: no commits, no tests, empty README, `ato` entry point points at a uv placeholder,
  `ib_ato.py2` is a stale copy differing by one line. `tax` symlink (personal path), `2026/` (real
  amounts, trade IDs) and `.claude/settings.local.json` are NOT git-ignored.
- Reliability unproven: no tests; silent-failure paths (missing FX rate → NaN → sums as 0; blank
  split ratio → divide by zero → inf; unmatched sell → proceeds dropped with warning only).
- Output: terminal print (ephemeral) + 8 flat CSVs. No per-security breakdown of headline numbers,
  no persistent human-readable record.
- Corporate actions (Dongfeng 489 spin-off / cash merger / 1-for-1 offer, Mar 2026) handled by
  hand-written Flex-style trade rows in `tax/flex/manual_dongfeng.csv`; `--splits` /
  `--opening-lots` exist but unused; IB's own Corporate Actions section only dumped for review.
- Two rule placements are wrong per ATO 2026 instructions (margin interest, fees; see §2.4).

Goal: standalone GitHub repo `ibkr-ato`, simple and clean, with (a) tested calculation checked
against ATO rules and reconciled against IB's own figures, (b) persistent per-FY HTML report
(gitignored, view-only) with per-security breakdown, (c) semi-automatic corporate-action handling,
(d) automated RBA download, (e) delta tracking against already-lodged FY2022–FY2025 figures.

User decisions (2026-10-02): name `ibkr-ato`; HTML report, not committed; RBA fetch only (IB Flex
stays manual export); FY2022–FY2025 already lodged with this script's figures.

## 2. What exists (exploration findings)

### 2.1 Current script shape (`ib_ato.py`)
Module-level functions, global `WARNINGS` list. Flow: `read_flex` (section split by ≥3 known
columns) → `classify` (trades/cash/transfers/corporate_actions by column presence; NAV, Funds,
Positions, TxnTaxes fall to `other` and are ignored) → `prep_trades` (STK EXECUTION rows; FX per
currency on trade date; cost = proceeds+taxes+commission each at own-currency rate) → `run_ledger`
(strict FIFO deque per conid; splits; opening lots; discount test `disp > acq + 1y`) →
`prep_cash` + `categorise` (div/wht/pil/int/fee from Type text; source AU vs foreign by ISIN prefix)
→ `build_summary` (per FY 18H/18A/18V, 20E/20M/20O, flags, carry-forward losses across FYs) →
CSVs + print. `--fy` only filters printing; all FYs always computed.

### 2.2 Inputs
- Flex CSVs `tax/flex/ATO_FY_*.csv` (5 files, account open Jan 2022 → FY2026). One query template,
  8 sections in fixed order, each starting with a `"ClientAccountID"` header row: Change in NAV,
  Statement of Funds, Open Positions, Trades (EXECUTION + CLOSED_LOT rows, 87 cols), Transaction
  Taxes/Fees, Corporate Actions, Cash Transactions, Transfers. Dates `YYYYMMDD`, datetimes
  `YYYYMMDD;HHMMSS`. Every row has `FXRateToBase`. Currencies USD (FY22–25), HKD (FY25–26), base AUD.
  Cash types present: Dividends, Withholding Tax, Other Fees, Deposits/Withdrawals (no interest, no
  PIL in real data → synthetic fixtures must cover those).
- RBA F11: `rba_f11.csv` (BOM, metadata rows, `Series ID` header row, `DD-Mon-YYYY`, Jan 2023 → Oct
  2026) and `rba_f11_2018-2022.xls` (same layout, Excel dates, xlrd). Rates = FC per 1 AUD.
- `manual_dongfeng.csv`: 3 Flex-Trades-style rows (`MANUAL-489-OUT`, `MANUAL-7489-IN`,
  `MANUAL-489SPO-IN`) injected via `--flex`. Semantics: Voyah shares valued at market as part of
  Dongfeng capital proceeds; Voyah cost base = that market value.

### 2.3 IB Corporate Actions grammar (FY2026 rows)
Columns: `Type` (SO spin-off, TC tender/merger; splits FS/RS), `ActionDescription`, `Quantity`
(signed), `Conid`, `ISIN`, `Date/Time`, `Proceeds`, `FifoPnlRealized`. Patterns:
- `489(ISIN) SPINOFF  3552608 FOR 10000000 (489.SPO, ...)` → new/old ratio
- `489.SPO(ISIN) MERGED(Mandatory Offer Allocation) WITH <ISIN> 1 FOR 1 (...)` → two rows, opposite
  signs, identical description → share-for-share
- `489(ISIN) MERGED(Acquisition) FOR HKD N.NN PER SHARE (...)` → cash takeover; IB row carries cash
  `Proceeds` and `FifoPnlRealized`
- expected for splits: `SPLIT <n> FOR <m>` with Type FS/RS (fixture only)

### 2.4 Independent oracle already inside the Flex files
- Trades `CLOSED_LOT` rows: `OpenDateTime`, `CostBasis`, `FifoPnlRealized`, `Notes/Codes` LT/ST per
  disposal → compare with our parcels in FC. IB default is FIFO too.
- Open Positions at 30 Jun: `Quantity`, `CostBasisMoney` per conid → must equal our holdings.
- Change in NAV per FY: `Dividends`, `WithholdingTax`, `Interest`, `Commissions`,
  `ForexCommissions`, `OtherFees`, `TransactionTax`, `CorporateActionProceeds` → category sums at
  IB's `FXRateToBase` must match (completeness); RBA vs IB rate difference shown as info.

### 2.5 Rules check: current code vs ATO (research 2026-10-02)
Caveat: ato.gov.au returned HTTP 403 to automated fetch; quotes came via search-index snippets of
the named ATO pages ("PARTIALLY VERIFIED"). Implementation re-fetches each URL (curl with browser
UA, or browser) and records verbatim quote + access date in `docs/rules.md`.

| Rule | Code now | Finding | Action |
|---|---|---|---|
| FIFO parcel matching | deque per conid | taxpayer may choose parcels; FIFO acceptable; IB default FIFO | keep, document |
| Buy brokerage + txn tax in cost base | yes | incidental costs = 2nd element (incl. stamp duty) | keep |
| Sell brokerage | subtracted from proceeds | ATO: also 2nd element of cost base. Same arithmetic for gain and reduced cost base | relabel in report only |
| 50% discount | `disp > acq + 1y` | exclude acq day and event day; exact anniversary NOT eligible (ATO example: acq 2 Feb 2024 → eligible on/after 3 Feb 2025) | keep; boundary tests |
| Loss order | non-discount first, then discount | taxpayer may choose; this order gives lowest tax; carried losses oldest first | keep; test |
| 18H / 18A / 18V | gross / net / carry | labels confirmed 2026 | keep |
| CGT schedule flag | `max(gains, cur-yr losses) > 10000` | myTax individuals DO need schedule when total current-year gains OR losses > $10,000 | keep; label "myTax" |
| 20E gross foreign div + PIL + interest | yes | gross-up confirmed | keep |
| 20O = foreign WHT; >$1,000 flag | yes | ≤$1,000 claim in full; >$1,000 compute FITO limit | keep; wording "compute FITO limit" |
| Margin interest × share → 20M, rest D8 | yes | WRONG: item 20 "Deductible expenses" EXCLUDES debt deductions; interest for foreign shares → Other deductions (paper D15); interest for AU shares → D8 | `D15 = interest × foreign share`, `D8 = interest × (1−share)`; 20M no longer nets interest. Zero impact on lodged FYs (no interest paid in data) |
| Fees (ADR, market data, account) | reported only | non-debt expenses in earning foreign dividends → item 20 Deductible expenses (net into 20M); ADR fees specifically NOT FOUND | report `fees_foreign` (foreign-ISIN symbol) and `fees_unallocated` (no symbol) as separate lines. Netting into 20M only when `deduct_foreign_fees = true` in `[defaults]` (default false, stated in report Assumptions). Default changes no lodged label |
| FX: RBA daily on txn date, ≤7d lookback | yes | ATO uses RBA since 2020; specific-date rule for CGT; prior published day for weekends (ATO practice). Averages allowed for regular income, not one-off disposals | keep daily for all (conservative, simplest); missing rate = hard error |
| Forex gains on USD/HKD cash balances | ignored | FRE2 on FX cash withdrawal prima facie assessable; 12-month rule covers only trade→settlement; $250k election for "qualifying forex accounts" (broker cash: NOT FOUND) | KNOWN GAP, listed in report "not computed, seek advice"; optional Phase 8 |
| Payment in lieu | in 20E, flagged | lender-side treatment NOT FOUND | keep + flag |
| Corporate actions | manual trade rows | split: no CGT event, cost spread, acq date kept. Demerger: cost base apportioned by market value, acq date kept, Div 125 tests (foreign: self-assess). Cash takeover: A1 at disposal date, proceeds = cash + market value of property received. Scrip-for-scrip: rollover if ≥80% acquired, else disposal at market value | event types §3.5; human picks treatment |
| Franking credits (AU via IB) | not computed | labels 11S/11T/11U; entitlement via foreign custodian unconfirmed | document only |

Source URLs to re-fetch for `docs/rules.md`: CGT discount
(`ato.gov.au/individuals-and-families/investments-and-assets/capital-gains-tax/cgt-discount`);
losses (`.../calculating-your-cgt/using-capital-losses-to-reduce-capital-gains`); items 18 and 20
2026 (`ato.gov.au/forms-and-instructions/individual-supplementary-tax-return-2026-instructions/...`);
CGT schedule myTax 2026 (`.../mytax-instructions/2026/income/australian-income-or-losses-from-investments-or-property/capital-gains-tax-schedule`);
FITO (`.../mytax-instructions/2026/tax-offsets/foreign-income-tax-offset`); other foreign income
(`.../mytax-instructions/2026/income/foreign-income/other-foreign-income`); other deductions
(`.../mytax-instructions/2026/deductions/other-deductions/other-deductions`); FX overview
(`ato.gov.au/tax-rates-and-codes/foreign-exchange-rates-overview`); average rates
(`.../foreign-exchange-gains-and-losses/in-detail/general-information-on-average-rates`); forex
(`.../foreign-exchange-gains-and-losses`, `ato.gov.au/forex12mthrule`); cost base
(`.../capital-gains-tax-guide-2021/.../what-is-the-cost-base`); D8 2026; TD 2004/1.
RBA (fetched OK): `rba.gov.au/statistics/tables/csv/f11.1-data.csv` (2023→now),
`rba.gov.au/statistics/tables/xls-hist/2018-2022.xls`, `.../xls-hist/2023-current.xls`.

## 3. Design

### 3.1 Repo and data layout
```
ibkr-ato/                         (this working dir as-is; `gh repo create ibkr-ato --source=.`
                                   works with any folder name; user may rename later)
  pyproject.toml                  name ibkr-ato, script ibkr-ato = "ibkr_ato.cli:main"
  README.md                       data layout, IB Flex export steps, yearly workflow
  docs/rules.md                   ATO rules used, verbatim quotes, URLs, access dates
  docs/corporate-actions.md       event types + Dongfeng/Voyah worked example
  src/ibkr_ato/ ...               (§3.2)
  tests/ ...                      (§3.7)
$IBKR_ATO_DATA/  (private; = current tax symlink target, or any dir; never in repo)
  flex/*.csv  rba/*  corporate_actions.csv  opening_lots.csv  ibkr-ato.toml
  lodged/FY2025.csv ...  golden/ (baseline)  out/FY2026/{report.html,*.csv}
```
`.gitignore` adds: `tax`, `2026/`, `out/`, `.claude/settings.local.json`, `*.py2`.

### 3.2 Modules (`src/ibkr_ato/`, target 100–320 lines each)
| module | responsibility | key public names |
|---|---|---|
| `cli.py` | argparse, 4 subcommands, only module that prints | `main(argv)` |
| `config.py` | `DataDir` paths, `RunConfig`, `[defaults]` from `ibkr-ato.toml` (stdlib tomllib) + CLI overrides | `DataDir.at(root)`, `RunConfig`, `load_config` |
| `flex.py` | read Flex CSV → 8 named sections (signature columns per section; unknown section raises); normalisers with fixed schemas | `read_flex`, `load_flex_dir`, `executions`, `closed_lots`, `open_positions`, `nav_totals`, `cash_rows`, `corp_action_rows` |
| `fx.py` | RBA F11 csv/xls → `RateTable`; lookup prior-business-day ≤ `max_gap_days`; `FxRateMissing` hard error listing all misses; `fetch_rba` | `load_rba`, `RateTable.to_aud`, `fetch_rba` |
| `income.py` | categorise cash rows (div/pil/wht/int_recv/int_paid/fee/None), source AU vs foreign, `fee_scope` foreign/au/unallocated, AUD at RBA and at IB rate; returns (income, ignored) | `categorise`, `build_income` |
| `corporate_actions.py` | event dataclasses `Split`, `RemoveForCash`, `AddLots`, `Rollover`; load/validate `corporate_actions.csv` (`CorpActionInvalid`); `draft_corporate_actions` from IB rows | `load_corporate_actions`, `parse_action`, `draft_corporate_actions` |
| `ledger.py` | `Trade` records from executions; FIFO book; apply events; `Parcel`, `Holding`, `Unresolved`; `discount_eligible`; `until=` for holdings at a date | `prepare_trades`, `load_opening_lots`, `run_ledger`, `discount_eligible` |
| `summary.py` | `LABELS` (EN+ZH kept), `CgtSummary`, `IncomeSummary`, `FySummary`, loss application, lodged snapshot load/write/delta | `build_summaries`, `summary_frame`, `load_lodged`, `write_lodged`, `lodged_delta` |
| `reconcile.py` | three oracles → `ReconRow(section, check, scope, ours, ib, diff, tol, status, note)` | `reconcile_fy` |
| `report.py` | Jinja2 context + render `templates/report.html.j2`; FINAL/DRAFT status | `report_status`, `write_report` |
| `pipeline.py` | `run_pipeline(cfg) -> PipelineResult`; `write_outputs(result, fy, out_dir)` | |

Conventions: frozen dataclasses / NamedTuples for records; DataFrames treated as values (`assign`,
never mutate caller's frame); no module-level mutable state (legacy `WARNINGS` → `Issue` tuples
returned by each stage); `logging` for diagnostics, `print` only in `cli.py`. One documented
exception to immutability: the FIFO book `dict[str, deque[_Lot]]` inside `ledger.py`; mutation
confined to private helpers, `run_ledger` returns frozen records.

Reuse from `ib_ato.py`: `norm()`, header detection (≥3 known columns, HEADER/DATA prefix skip),
`to_date` regex, `fy_of`, `dedupe`, `load_rba` parsing, `fx_lookup` searchsorted logic, FIFO loop,
`categorise`, `LABELS` text, loss application arithmetic, `print_summary` format.

### 3.3 CLI
```
ibkr-ato run          [--data DIR] [--fy N | --all-fy] [--prior-losses X] [--margin-foreign-share X]
                      [--interest-source foreign|au] [--fx-max-gap-days N] [--no-lodged-carry] [-v]
ibkr-ato fetch-rba    [--data DIR] [--force]
ibkr-ato corp-actions [--data DIR] [--dry-run]
ibkr-ato lodge        --fy N [--data DIR] [--from SUMMARY.csv] [--force] [--note TEXT]
```
- `--data` defaults to `$IBKR_ATO_DATA`; error if neither set. All paths relative to it.
- `run`: computes all FYs (carry chain), writes `out/FY<N>/` for requested FY (default latest):
  `report.html`, `summary.csv`, `cgt_parcels.csv`, `open_lots.csv`, `income_detail.csv`,
  `dividends_by_country.csv`, `fx_conversions.csv`, `reconciliation.csv`, `unresolved.csv`,
  `issues.csv`. Prints per-FY summary table (as today) + status line `FINAL` / `DRAFT (reasons)` +
  report path. Exit 0 FINAL, 2 DRAFT (pending corp actions, unresolved sells, recon mismatch),
  1 error (`FxRateMissing`, `CorpActionInvalid`, no Flex files).
- `fetch-rba`: downloads `f11.1-data.csv` + `2018-2022.xls` into `rba/`, skips existing unless
  `--force`, loads result and prints covered range; on HTTP error prints manual URL.
- `corp-actions`: reads IB Corporate Actions rows from all Flex files, merges into
  `corporate_actions.csv` keyed by `action_id` (never alters existing rows), prints by status.
- `lodge`: snapshots computed summary for FY N into `lodged/FY<N>.csv`; refuses if DRAFT or file
  exists unless `--force`; `--from` imports from an existing summary CSV (legacy `ato_summary.csv`)
  so FY2022–FY2025 lodged figures come from the baseline run, not hand-typing.
  Why it exists (user confirmed 2026-10-03): `run` recomputes every FY from raw data each time, so
  prior-year figures can drift after a rule fix, corporate-action confirmation, re-export or config
  change. The lodged snapshot never changes; `run` flags drift as `review/amend` and uses lodged
  18V of FY N-1 as the carry-in for FY N (what the ATO holds, not a recomputation).
- No `reconcile` subcommand: `run` prints reconciliation status and exit code covers it.
- argparse (stdlib). No click/typer.

### 3.4 Data flow
```
flex/*.csv ─► load_flex_dir ─► FlexFile[] ─┬─► executions ─► prepare_trades ─► Trade[] ─┐
rba/*      ─► load_rba ─► RateTable ───────┤                                             │
opening_lots.csv ─► load_opening_lots ─────┼─────────────────────────────────────────────┤
corporate_actions.csv ─► load_corporate_actions ─► events(applied) + pending ────────────┤
                                           │                                   run_ledger ─► parcels, holdings, unresolved
                                           ├─► cash_rows ─► build_income ─► income        │
                                           ├─► closed_lots, open_positions, nav_totals ─► reconcile_fy ◄─┘
lodged/FY*.csv ─► load_lodged ─► build_summaries (carry-in from lodged 18V of FY N-1) ─► FySummary + deltas
                                           └─► report.build_context ─► render ─► out/FY<N>/report.html + CSVs
```

### 3.5 Corporate-action events (`corporate_actions.csv`)
Columns: `action_id,status,type,date,conid,symbol,to_conid,to_symbol,quantity,ratio,amount,currency,acq_date,note,ib_description`.
Status: `auto` (drafted split, applied), `confirmed` (human-resolved, applied), `needs_manual`
(blocks FINAL), `ignored`.

| type | fields | ledger effect |
|---|---|---|
| `split` | date, conid, ratio | qty × ratio, per-unit cost ÷ ratio, acq dates unchanged |
| `remove_for_cash` | date, conid, quantity (blank = all), amount = total proceeds FC, currency | FIFO disposal via same `_dispose` as sells; `disp_source = action_id` |
| `add_lots` | date, conid, symbol, quantity, amount = total cost FC, currency, acq_date (default date) | append lot |
| `rollover` | date, conid, to_conid, to_symbol, ratio (default 1), quantity (blank = all) | move lots FIFO to new key, qty × ratio, cost and acq date preserved (scrip-for-scrip) |

Validation (`CorpActionInvalid`, stops run): ratio finite > 0; quantity > 0 where given; amount +
currency for remove_for_cash/add_lots; dates parse; conid present; known type/status. Blank ratio
can no longer become `inf`. Demerger cost-base apportionment not supported (would need a fifth
`adjust_cost` event); note tells user how to express alternatives with existing types.

Draft generator (`corp-actions`): FS/RS `SPLIT a FOR b` → `split` ratio a/b `auto` (safety net:
reconciliation B checks 30 Jun quantity and cost against IB Open Positions, so a mis-parsed ratio
surfaces as a mismatch); SO `SPINOFF a FOR b` → `add_lots` qty from IB row, amount blank,
`needs_manual`, note "decide: (a) assessable dividend in specie at market value (item 20),
(b) disposal consideration (parent proceeds = cash + market value), (c) demerger rollover
(manual, not supported automatically)"; TC
`MERGED(Acquisition) FOR CCY x PER SHARE` → `remove_for_cash`, amount prefilled from IB `Proceeds`,
`needs_manual`, note "IB proceeds cash only; add market value of property received"; TC
`MERGED(...) WITH <ISIN> a FOR b` → pair the two rows (same date+description, opposite signs) into
one `rollover`, `needs_manual`, note "scrip-for-scrip if eligible and chosen, else remove_for_cash +
add_lots at market value"; anything else → blank type, `needs_manual`. Every row carries
`ib_description` verbatim.

Dongfeng/Voyah drafted as `needs_manual` (NOT pre-confirmed: live FY2026 position, three candidate
treatments; `manual_dongfeng.csv` encoded treatment (b) and the user must confirm or change it):
```
<SO id>,needs_manual,add_lots,2026-03-12,863175414,489.SPO,,,<qty>,,,HKD,,"decide (a)/(b)/(c); for (b) amount = qty × market value",...
<TC id 489>,needs_manual,remove_for_cash,2026-03-12,46636521,489,,,<qty>,,<IB cash proceeds>,HKD,,"IB proceeds cash only; for (b) add market value of Voyah shares received",...
<TC id SPO>,needs_manual,rollover,2026-03-18,863175414,489.SPO,862833915,7489,<whole qty>,1,,HKD,,"1-for-1 offer allocation: scrip-for-scrip rollover if eligible and chosen, else remove_for_cash + add_lots at market value; fraction sold by IB cash-in-lieu trade",...
```
Phase 4 verification: user reviews the three rows, picks treatment, flips status to `confirmed`;
run goes DRAFT → FINAL; golden baseline updated with the recorded reason. If the user keeps
treatment (b) with the manual file's amounts and dates, the only divergence from legacy output is
`open_lots` showing 7489 acquired 11/12 Mar (via rollover) instead of 18 Mar; summary and parcels
unchanged. IB dates events 12 Mar; manual used 11/18 Mar; report shows both.

### 3.6 Reconciliation (per FY; FC where IB gives it, AUD for NAV)
Tolerances: qty 1e-6; money per lot `max(0.05, 0.1%)`; per-FY aggregate `max(0.50, 0.1%)`; NAV
completeness 0.50 AUD.
- **A. CLOSED_LOT vs parcels**, key (conid, disp_date, acq_date): quantity, `cost_fc` vs
  `CostBasis`, `proceeds_fc − cost_fc` vs `FifoPnlRealized`; LT/ST vs `discount_eligible` = info.
  Per-FY sum of FC gains vs sum `FifoPnlRealized`. Fallback: per-disposal aggregates match but lots
  do not → info "lot method differs (IB not FIFO?)". Event-sourced parcels → `n/a`, with info row
  for IB's cash-only `Proceeds`/`FifoPnlRealized` on the 489 acquisition.
- **B. Open Positions 30 Jun vs `run_ledger(until=as_of)` holdings**, key (as_of, conid): quantity;
  `cost_fc` vs `CostBasisMoney` (downgraded to info when conid touched by an applied event — IB's
  spin-off cost allocation differs by design). One-sided → mismatch.
- **C. Change in NAV vs cash + trades**, stage 1 completeness at IB rate (`amount × fx_ib`):
  `div+pil` vs Dividends; `wht` vs WithholdingTax; `int_recv+int_paid` vs Interest; `fee` vs
  OtherFees; STK comm vs Commissions; CASH comm vs ForexCommissions; taxes vs TransactionTax.
  Stage 2 FX-source: RBA `aud` vs `aud_ib` per category as info; >5% → mismatch (inverted rate).
  `CorporateActionProceeds` vs `remove_for_cash` AUD → info.
Presentation: one table per section in report + `reconciliation.csv`; mismatches first, status
classes `.mismatch/.ok/.info`. FY not FINAL while any `mismatch`.

### 3.7 HTML report (`templates/report.html.j2`, inline CSS ~30 lines, autoescape, tables only)
1. Header: FY period, generated at, version; banner FINAL/DRAFT with reasons; sources (Flex files
   with from/to, RBA files with range + publication date).
2. Summary: item, label, EN, ZH, value; + Lodged, Delta, Flag columns when lodged file exists
   (flag `review/amend` at |delta| ≥ 1.00).
3. CGT by security: per-symbol parcels table (acq, disp, qty, cost FC, proceeds FC, FX acq, FX disp,
   cost AUD, proceeds AUD, gain AUD, discount, source) + subtotal; FY totals; loss-application
   walk-through (carry-in, current losses, applied to other, applied to discount, 50%, net, carry-out).
4. Holdings at 30 Jun.
5. Foreign income by symbol and country (div, PIL, WHT) + detail rows; interest.
6. Deductions: margin interest (D15 foreign share / D8 AU share), fees foreign (netted into 20M
   only if `deduct_foreign_fees` elected, else shown for the user to decide), fees unallocated.
7. Australian dividends (franking reminder).
8. Reconciliation (3 tables).
9. Warnings and unresolved: unmatched sells, pending corp actions, excluded asset classes, ignored
   cash types with counts, known gaps (forex on cash balances, PIL treatment).
10. Corporate actions applied this FY.
11. Assumptions: FX source + fallback window, FIFO, 12-month rule as implemented, loss order, margin
    share, interest source, carry-in source, rounding, "not tax advice".

### 3.8 Tests (pytest; `tests/`)
Fixtures (`conftest.py`, no personal data): `FLEX_HEADERS` (real header lists, names only);
`flex_csv(path, nav=, trades=, closed=, positions=, corp_actions=, cash=, ...)` builder with row
factories; `rba_csv(path, rates)` writing BOM + metadata + `Series ID` + `DD-Mon-YYYY`; static
`tests/fixtures/flex_mini.csv` (8 sections, ~20 rows, account `U0000000`, AAA/USD, BBB/HKD, CCC/AUD)
and `f11_mini.csv`; `.xls` via monkeypatched `pd.read_excel`.

Unit tests per module (rule tests first, TDD):
- `test_flex`: 8 sections classified; `Report Date`/`Date/Time` normalised; dedupe across
  overlapping files; `executions` excludes CLOSED_LOT; `closed_lots` parses `OpenDateTime`;
  unknown section raises.
- `test_fx`: AUD = 1; Saturday → Friday; gap > max → `FxRateMissing` listing all misses; unknown
  currency raises; overlapping files keep later; `fetch_rba` with fake `urlopen`, `--force`.
- `test_income`: `categorise` table incl. PIL, Broker Interest Received/Paid, Other Fees, None for
  Deposits; country from ISIN; interest_source switch; fee_scope; ignored rows returned.
- `test_ledger`: FIFO across lots; partial lot; `discount_eligible` boundary (acq 2025-03-01: disp
  2026-03-01 False, 2026-03-02 True; acq 2024-02-29: 2025-02-28 False, 2025-03-01 True); unmatched
  sell → `Unresolved` + matched part still parcels; split; remove_for_cash; add_lots acq_date;
  rollover preserves acq date/cost; partial rollover; `until`; commission in other currency gives
  defined `net_fc`.
- `test_corporate_actions`: blank/zero/negative/non-numeric ratio → `CorpActionInvalid`; unknown
  type/status; `parse_action` for split, spin-off, cash merger, scrip merger; scrip pair → one
  rollover draft; draft never alters existing rows; `needs_manual` → pending.
- `test_summary`: loss ordering (other 100, disc 100, losses 150 → 18A 25, 18V 0); carry across
  FYs; carry-in from lodged 18V; schedule threshold on gains or losses; D15/D8 split; 20M = 20E by
  default and 20E − fees_foreign when `deduct_foreign_fees` is set; `lodged_delta` flags at 1.00
  not 0.40.
- `test_reconcile`: consistent fixture → no mismatch; dropped sell → A mismatch; altered position →
  B mismatch; removed cash row → C stage-1 mismatch; event-touched conid → info; LT/ST → info.
- `test_report`: renders; EN+ZH present; DRAFT when pending; FINAL otherwise; escapes `<`.
- `test_cli`: exit codes 0/2/1; `lodge` refuses on DRAFT/existing; `corp-actions --dry-run` writes
  nothing; missing `--data` message.

Golden test (`tests/test_golden.py`, `@pytest.mark.golden`, skipped unless `IBKR_ATO_DATA` set):
baseline at `$IBKR_ATO_DATA/golden/` generated ONCE by legacy
`python ib_ato.py --flex $IBKR_ATO_DATA/flex/*.csv --rba $IBKR_ATO_DATA/rba/* --out $IBKR_ATO_DATA/golden`.
Compares summary (all FYs), parcels, open lots, income detail: intersection of columns, rows sorted
on stable keys, numeric tol 0.005, strings exact. Summary rows compared by `key` through an explicit
`LEGACY_KEY_MAP` (`margin_interest_deducted_20` → `D15_margin_interest_foreign`,
`margin_interest_remaining` → `D8_margin_interest_au`, `fees_other` → `fees_foreign` +
`fees_unallocated` summed); new keys with no legacy counterpart are excluded from the comparison,
so the only failures are real value changes. Also asserts no `mismatch` recon rows per FY once
Phase 3 lands. `IBKR_ATO_UPDATE_GOLDEN=1` rewrites baseline; reason goes in commit message.
Expected deliberate deltas (recorded when they land): Phase 2 none in values (D15/D8 are zero in
real data, fee netting off by default); Phase 4 changes 7489 acq date in open lots after the user
confirms treatment.

## 4. Phases (each verifiable; FY2026 reconciled report lands at Phase 3)

**Phase 0 — repo hygiene.** Write this design as `docs/superpowers/specs/2026-10-03-ibkr-ato-design.md`
(brainstorming spec, deferred from plan mode) and commit it with the hygiene change. `.gitignore`
(+ `tax`, `2026/`, `out/`, `.claude/settings.local.json`,
`*.py2`); delete `ib_ato.py2`, `__pycache__`; `src/ato` → `src/ibkr_ato`; `pyproject.toml` name
`ibkr-ato`, script `ibkr-ato = "ibkr_ato.cli:main"`, deps + `jinja2`, dev group `pytest`,
`pytest-cov`, `ruff`; README skeleton (data dir, `IBKR_ATO_DATA`, IB Flex export steps: 8 sections,
`yyyyMMdd`, include CLOSED_LOT, one file per FY). Copy `2026/` to `$IBKR_ATO_DATA/golden/legacy-2026/`
then remove from repo. Confirm with user: keep personal gmail in `pyproject.toml` authors? Repo
public or private? First commit only after `git status` shows no private paths.
Verify: `uv sync`, `uv run ibkr-ato --help`, `git status`.

**Phase 1 — freeze baseline + fixtures.** Generate golden baseline with legacy `ib_ato.py` (kept at
repo root until Phase 2 ends). Write `conftest.py` builders, `flex_mini.csv`, `f11_mini.csv`,
`test_golden.py` (initially compares legacy output to itself to prove mechanics).
Verify: `pytest -m golden` green; `pytest -m "not golden"` green.

**Phase 2 — modules, TDD.** Write pinning tests (12-month boundary, loss order, 7-day lookback,
FIFO) RED; build `config`, `flex`, `fx`, `income`, `ledger`, `summary`, `pipeline`, thin `cli run`
GREEN; land hard-fail changes (`FxRateMissing`, `Unresolved`) and rule fixes (D15/D8, 20M fees)
each with own test; write `docs/rules.md` with re-fetched ATO quotes. Delete `ib_ato.py`.
Verify: all tests green; golden green except the recorded, expected deltas (then update baseline
with reason); each file < 400 lines; `ruff check` clean.

**Phase 3 — reconciliation + HTML report.** `reconcile.py`, `report.py`, template,
`write_outputs`, status line, exit codes.
Verify: `ibkr-ato run --fy 2026` → `out/FY2026/report.html`; recon shows no `mismatch` for FY2026
(expect `info` on 7489 cost basis and 489 CA row, `n/a` on event parcels); golden green; `uv build`
wheel contains `templates/`.

**Phase 4 — corporate actions.** `corporate_actions.py` (validation tests first), ledger event
application, `corp-actions` subcommand, FINAL/DRAFT gating. Run `corp-actions` → 4 Dongfeng rows
drafted `needs_manual`; user picks treatment (a)/(b)/(c) and flips to `confirmed`; move
`manual_dongfeng.csv` out of `flex/`. `docs/corporate-actions.md` with Dongfeng/Voyah worked
example (all three treatments described, chosen one marked) and the "new year" procedure.
Verify: run is DRAFT until confirmed, FINAL after; with treatment (b) golden green on summary +
parcels, open lots differ only in 7489 acq date → deliberate baseline update.

**Phase 5 — lodged snapshots.** `load_lodged`, `write_lodged`, `lodged_delta`, carry-in from lodged
18V, delta columns in report, `lodge` subcommand with `--from` import from
`golden/ato_summary.csv` for FY2022–FY2025.
Verify: deltas render for FY2022–2025 (expect zero with default config; non-zero only if
`deduct_foreign_fees = true` is elected, then flagged review/amend); FY2026 carry-in equals lodged
FY2025 18V.

**Phase 6 — `fetch-rba`.** `fetch_rba` with injected `urlopen`; subcommand.
Verify: fetched files load and cover every trade/cash date.

**Phase 7 — config + docs + publish.** `ibkr-ato.toml` `[defaults]`; README yearly workflow
(export Flex → `fetch-rba` → `corp-actions` → resolve → `run` → `lodge`); `ruff` config; coverage
≥ 80%. Create GitHub repo (`gh repo create`, visibility per user) and push — confirm before push.

**Phase 8 (optional, user to decide) — forex ledger.** Informational FIFO over foreign-currency
cash from `fx_conversions` + cash rows → FRE2 gains table in report, not in totals, "seek advice".

## 5. Verification (end-to-end)
1. `uv sync && uv run pytest -m "not golden"` → all green, coverage ≥ 80%.
2. `IBKR_ATO_DATA=<private dir> uv run pytest -m golden` → green (after recorded baseline updates).
3. `uv run ibkr-ato run --fy 2026` → exit 0, `out/FY2026/report.html` opens in browser; summary
   table matches terminal; CGT section lists every FY2026 disposal by symbol with subtotals;
   reconciliation A/B/C show ok/info only; assumptions and known gaps present.
4. `uv run ibkr-ato run --all-fy` → FY2022–FY2025 reports show lodged vs computed deltas.
5. `uv run ibkr-ato corp-actions --dry-run` on FY2026 → drafts the 4 Dongfeng rows with correct
   types and `needs_manual`.
6. `uv run ibkr-ato fetch-rba --force` into a temp data dir → both files download and load.
7. `git status` clean of private paths; `git log` shows conventional commits; `uv build` succeeds.

## 6. Risks / notes
- IB lot method not FIFO → per-lot A mismatches; README says set IB to FIFO; fallback info wording.
- IB cost basis for 7489 after spin-off differs by design → info, not mismatch.
- Event dates (manual 11/18 Mar vs IB 12 Mar) could flip 12-month status for a 489 lot bought near
  Mar 2025 → report shows both dates.
- Lodged FY2022–2025 figures imported from legacy baseline: user confirms they equal what was lodged.
- Forex on cash balances out of scope (documented gap).
- pandas 3 string dtype / copy-on-write behaviour changes during extraction → golden test detects.
- Golden diffs print private amounts → run golden only locally, never in shared CI.
