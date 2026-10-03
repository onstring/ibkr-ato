# Tax rules used by ibkr-ato

This page lists each rule the tool applies, the ATO wording it rests on, and where the tool
implements it. Quotes were taken from ato.gov.au on 3 October 2026 for the 2025–26 income year.
Rules marked **not verified** have no ATO source confirmed; treat those figures as prompts to
check, not answers.

This is not tax advice.

## Capital gains (item 18)

### Parcel matching: first in, first out

The tool matches disposals to the oldest lot first (FIFO). The ATO lets you identify which
parcel you sold; FIFO is a consistent, documented choice and matches IB's default lot method.
Reconciliation section A compares our parcels with IB's own closed lots.

Code: `ledger.py`.

### 50% CGT discount: more than 12 months, excluding both days

> "You exclude the day of acquisition and the day of the CGT event when working out if you owned
> the CGT asset for at least 12 months before the 'CGT event' happens."
> ([CGT discount](https://www.ato.gov.au/individuals-and-families/investments-and-assets/capital-gains-tax/cgt-discount))

So a disposal on the exact first anniversary does not qualify; the day after does. Code:
`ledger.discount_eligible`; tests in `tests/test_ledger.py`.

### Order of applying capital losses

> "You can choose which capital gains to subtract your capital losses from. If you have any
> capital gains that are not eligible for the CGT discount, subtract your capital losses from
> these gains first."
> ([Using capital losses](https://www.ato.gov.au/individuals-and-families/investments-and-assets/capital-gains-tax/calculating-your-cgt/using-capital-losses-to-reduce-capital-gains))

Current-year and carried-forward losses go against non-discount gains, then discount gains;
the 50% discount applies to what is left. Code: `summary.cgt_for_fy`.

### Cost base: foreign currency and incidental costs

> "If the cost base or reduced cost base includes an amount paid in a foreign currency, you must
> convert it to Australian currency. Use the exchange rate at the time of the relevant
> transaction or event – for example, when the money was paid for the asset."
>
> "Second element: incidental costs of acquiring the CGT asset or that relate to the CGT event"
> ([Cost base of assets](https://www.ato.gov.au/individuals-and-families/investments-and-assets/capital-gains-tax/calculating-your-cgt/cost-base-of-asset))

Brokerage and transaction taxes (for example Hong Kong stamp duty) on a purchase are added to
the cost; on a sale they reduce the proceeds. The gain is the same either way. Each amount is
converted at the RBA rate on the trade date. Code: `ledger.prepare_trades`.

### CGT schedule

> "You must complete the Capital gains tax (CGT) schedule if: your total current year capital
> gains or losses are more than $10,000."
> ([myTax 2026 CGT schedule](https://www.ato.gov.au/individuals-and-families/your-tax-return/instructions-to-complete-your-tax-return/mytax-instructions/2026/income/australian-income-or-losses-from-investments-or-property/capital-gains-tax-schedule))

Summary key `cgt_schedule_required`.

## Foreign income (item 20)

### Gross foreign income

> "At Gross income: include any assessable foreign income (including foreign tax on that
> income) that you haven't already shown on your tax return."
> ([myTax 2026 Other foreign income](https://www.ato.gov.au/individuals-and-families/your-tax-return/instructions-to-complete-your-tax-return/mytax-instructions/2026/income/foreign-income/other-foreign-income))

IB reports dividends gross and withholding tax as a separate row, so 20E sums the gross amounts
and 20O sums the withholding tax.

### Foreign income tax offset

> "Does the total amount of the foreign tax you paid during 2025–26 exceed $1,000?"
> ([myTax 2026 FITO](https://www.ato.gov.au/individuals-and-families/your-tax-return/instructions-to-complete-your-tax-return/mytax-instructions/2026/tax-offsets/foreign-income-tax-offset))

Above $1,000 you must work out the offset limit; the report flags it (`fito_over_1000`).

### Expenses against foreign income exclude interest

> "At Deductible expenses: include any deductible expenses that you incurred in earning your
> other foreign income, excluding any debt deductions such as interest and borrowing costs."
> ([myTax 2026 Other foreign income](https://www.ato.gov.au/individuals-and-families/your-tax-return/instructions-to-complete-your-tax-return/mytax-instructions/2026/income/foreign-income/other-foreign-income))
>
> "you may claim 'debt deductions' you incurred in earning assessable income (for example,
> foreign source income that you included at Other foreign income on your tax return), if you
> haven't claimed them elsewhere on your tax return."
> ([myTax 2026 Other deductions](https://www.ato.gov.au/individuals-and-families/your-tax-return/instructions-to-complete-your-tax-return/mytax-instructions/2026/deductions/other-deductions/other-deductions))

Margin interest is therefore reported at D15 (share used for foreign shares, setting
`margin_foreign_share`) and D8 (the rest), not netted into 20M. The original script netted it
into 20M; no margin interest was paid in FY2022 to FY2026, so lodged figures are unaffected.

### Fees on foreign shares (ADR fees): **not verified**

Non-debt expenses incurred in earning foreign income go to item 20 "Deductible expenses"
(quote above). Whether ADR pass-through fees are deductible was not confirmed. The tool reports
them as `fees_foreign` and only nets them into 20M when you set `deduct_foreign_fees = true`.

### Return of capital paid as a "dividend": **not verified**

IB labels some Hong Kong payments "Return of Capital". If the payment is a return of capital
under Australian law, it is not income; it reduces the cost base of the shares (CGT event G1),
and any excess over the cost base is a capital gain. No ATO page was confirmed for a foreign
company's return of capital. Setting `return_of_capital`:

- `dividend` (default, same as the original script): counted in 20E.
- `cost_base`: left out of 20E and taken off the cost base of the shares held on the payment
  date. This matches how IB adjusts its own cost basis (reconciliation section B shows it).

### Payments in lieu of dividends: **not verified**

Counted in 20E and flagged in the report.

## Exchange rates

> "If you use a foreign exchange rate for currency not listed, you may use any reasonable
> externally sourced exchange rate for that currency."
> ([Foreign exchange rates overview](https://www.ato.gov.au/tax-rates-and-codes/foreign-exchange-rates-overview))

The tool uses RBA F11 daily rates on the transaction date for every amount. For a day without a
published rate it uses the latest earlier rate, up to 7 days back (`fx_max_gap_days`); a longer
gap stops the run. The ATO also accepts average rates for regular income, but not for one-off
capital disposals; the tool does not use averages.

## Not computed

### Forex gains on foreign currency cash

> "The 12-month rule generally provides that the measures do not apply to forex gains and losses
> on the acquisition or disposal of capital assets if the time between that acquisition or
> disposal and the due time for payment is not more than 12 months."
> ([Forex 12-month rule](https://www.ato.gov.au/forex12mthrule))

That rule covers the gap between a trade and its settlement. It does not cover gains or losses
on holding USD or HKD cash at IB and later spending it, which may be assessable forex
realisation gains. The tool lists currency conversions in `fx_conversions.csv` but does not
compute these gains. Seek advice if the balances are material.

### Franking credits

Franking credits on Australian shares are not read from IB. Use the company statements.
