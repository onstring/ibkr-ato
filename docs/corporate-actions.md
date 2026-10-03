# Corporate actions

IB's Corporate Actions section records what happened to a holding: a split, a spin-off, a
takeover. It does not record how the ATO treats it, and some events have more than one possible
treatment. ibkr-ato therefore splits the work:

1. `ibkr-ato corp-actions` reads IB's rows and drafts `corporate_actions.csv` in your data
   directory.
2. You decide the treatment, fill in amounts, and set `status` to `confirmed`.
3. `ibkr-ato run` applies confirmed rows to the parcel ledger.

While any row is `needs_manual`, the report stays DRAFT.

## The file

| Column | Meaning |
|---|---|
| `action_id` | `ib:<IB ActionID>:<conid>`; keeps rows stable across runs |
| `status` | `auto` (applied), `confirmed` (applied), `needs_manual` (blocks FINAL), `ignored` |
| `type` | `split`, `remove_for_cash`, `add_lots` or `rollover` |
| `date` | date of the event (CGT event date for disposals) |
| `conid`, `symbol` | the security affected |
| `to_conid`, `to_symbol` | target security for `rollover` |
| `quantity` | shares affected (blank = whole holding, for `remove_for_cash` and `rollover`) |
| `ratio` | new shares per old share (`split`, `rollover`) |
| `amount`, `currency` | total proceeds (`remove_for_cash`) or total cost (`add_lots`) in that currency |
| `acq_date` | acquisition date for `add_lots` (default: `date`) |
| `note`, `ib_description` | your notes; IB's text, verbatim |

Re-running `corp-actions` never changes existing rows. It only appends new IB actions.

## Event types

| Type | Ledger effect | Typical use |
|---|---|---|
| `split` | quantity × ratio; cost per share ÷ ratio; acquisition dates kept | share split or consolidation (no CGT event) |
| `remove_for_cash` | disposes of shares FIFO with the given proceeds | takeover for cash, or for cash plus shares |
| `add_lots` | adds a new lot at the given cost and acquisition date | shares received in specie, spin-off shares |
| `rollover` | moves lots to a new security; cost and acquisition dates kept | scrip-for-scrip rollover |

Invalid rows stop the run with a clear message: a missing or non-positive ratio, a missing
amount or currency, an unknown type or status, a date that does not parse.

Plain splits are drafted as `auto` and applied without a decision. Reconciliation section B
checks your holdings at each 30 June against IB's, so a mis-read ratio shows up as a mismatch.

Demerger rollover (Division 125) needs the parent's cost base apportioned by market value. That
is not automated; express it with `add_lots` for the new shares at their apportioned cost, and
record the reduced parent cost in `opening_lots.csv` if needed, or ask for an `adjust_cost`
event to be added.

## New financial year checklist

1. Export the Flex Query for the year into `flex/`.
2. `ibkr-ato fetch-rba` to refresh exchange rates.
3. `ibkr-ato corp-actions`, then resolve every `needs_manual` row.
4. `ibkr-ato run`; work through the report until it says FINAL.
5. After lodging: `ibkr-ato lodge --fy <year>`.

## Worked example: Dongfeng Motor (489) privatisation, March 2026

IB recorded four rows:

| IB type | Description (shortened) | Meaning |
|---|---|---|
| SO | `489 SPINOFF 3552608 FOR 10000000 (489.SPO ...)` | 0.3552608 Voyah entitlement (489.SPO) per Dongfeng share |
| TC | `489 MERGED(Acquisition) FOR HKD 6.68 PER SHARE` | Dongfeng shares cancelled for cash |
| TC (two rows) | `489.SPO MERGED(Mandatory Offer Allocation) WITH CNE100007FB7 1 FOR 1` | 489.SPO swapped 1-for-1 into Voyah (7489); the fraction paid as cash |

`corp-actions` drafts three rows: `add_lots` for 489.SPO, `remove_for_cash` for 489, and one
`rollover` from 489.SPO to 7489 (IB's two offer rows are paired by ActionID).

### The decision

The Voyah shares can be treated in three ways:

- **(a) Dividend in specie.** The shares are a distribution: assessable foreign income at
  market value (item 20), and that value becomes their cost base. The 489 proceeds are the cash
  only.
- **(b) Part of the takeover consideration.** The shares are part of what you received for your
  Dongfeng shares. Capital proceeds = cash + market value of the Voyah shares; their cost base is
  that market value. This is what the original `manual_dongfeng.csv` encoded.
- **(c) Demerger rollover.** If Division 125 applies, the Dongfeng cost base is split between
  Dongfeng and Voyah by market value, with no gain on the spin-off. Not automated.

The rows in your `corporate_actions.csv` are pre-filled for (b), with the amounts and dates
from `manual_dongfeng.csv`, and left as `needs_manual`. The choice is yours (or your tax
adviser's).

### Chosen: treatment (b), as in the original manual file

Confirmed by the owner on 3 October 2026. `corporate_actions.csv` holds three confirmed rows:

| Type | Date | Symbol | Meaning |
|---|---|---|---|
| `add_lots` | 11 Mar 2026 | 489.SPO | the fractional entitlement, at market value; IB sold it for cash in lieu |
| `add_lots` | 18 Mar 2026 | 7489 | whole Voyah shares received as consideration, cost base = market value on 18 Mar |
| `remove_for_cash` | 18 Mar 2026 | 489 | Dongfeng disposal; proceeds = cash + market value of the Voyah shares |

IB's share-for-share row was changed from the drafted `rollover` to the second `add_lots`.
Under (b) the Voyah shares are acquired on the takeover date at market value. A rollover would
instead carry an earlier date and cost. `manual_dongfeng.csv` was retired from `flex/`.

Result, checked against the original script: every figure for FY2022 to FY2026 is unchanged,
including the Voyah cost base. Reconciliation has no mismatches. The cost of 7489 and of the
489.SPO fraction differs from IB's figure, because IB allocates spin-off cost its own way. Those
rows show as info.

For (a), keep the `add_lots` and `rollover` rows, set the `remove_for_cash` amount to the cash
proceeds only, and add the market value to your foreign income. That last step is outside the
tool.
