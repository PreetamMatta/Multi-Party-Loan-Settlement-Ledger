# Exit Scenarios

Any owner can choose to exit the property arrangement at any time. Across a
20-year horizon this **will** happen — life situations change, marriages
diverge, careers move people across continents, deaths trigger inheritance.
The system's job is to surface fair, transparent math when that moment
comes; **the owners and their lawyers decide the actual buyout number.**

---

## Philosophy: the app surfaces math, humans decide

The exit calculator computes **three buyout numbers, side by side.** It
does not pick a winner. It does not advise. It does not initiate a legal
transfer. It does not adjust equity in the database.

It is a calculator. The output is a one-page summary that shows:

> "Here are three different ways to value the exiting owner's stake.
> Number 1 reflects what they put in. Number 2 reflects today's asset
> value. Number 3 is a blend. The remaining owners and the exiting owner
> read these and negotiate. Lawyers handle the rest."

This separation — math is software, decisions are human — is intentional.
A buyout figure has tax consequences, family-relationship consequences,
and legal consequences that no algorithm should resolve unilaterally.

---

## Buyout Number 1 — Net contribution, inflation-adjusted

> "What you actually put in, in today's purchasing power, minus what you
> owe other owners."

### Formula

```
net_contribution = total_capex_contributed
                 − total_interpersonal_debt_owed_to_others
                 + total_interpersonal_credit_owed_by_others

inflation_adjustment = apply CPI index from each contribution date to exit_date
                       (year-by-year compounding using the property's CPI series)

buyout_1 = inflation_adjusted_net_contribution
```

### What goes into it

- **`total_capex_contributed`** — the exiting owner's lifetime CapEx
  contribution total in property currency: every CONTRIBUTION,
  EMI_PAYMENT (principal portion only), and BULK_PREPAYMENT they
  made, valued at the `inr_landed` of each event. (See
  [balances-and-equity.md — contribution total](balances-and-equity.md#contribution-total-per-owner).)
  OpEx is **not** included — that's not equity-building outlay.
- **`total_interpersonal_debt_owed_to_others`** — sum across all
  pairs where the exiting owner is the borrower. Includes accrued
  interest as of `exit_date`.
- **`total_interpersonal_credit_owed_by_others`** — sum across all
  pairs where the exiting owner is the lender. Includes accrued
  interest as of `exit_date`.

### Why inflation adjust?

A ₹1,000,000 contribution made in 2026 is not the same economic
contribution as ₹1,000,000 made in 2046. Failing to inflation-adjust
would systematically under-compensate early contributors — the owner who
fronted the downpayment in year one would be repaid in much-cheaper
rupees decades later.

The CPI series used is configurable per-property in
`property_settings.cpi_series_source` (default: India's MoSPI CPI for
INR-denominated properties; US BLS CPI-U for USD-denominated). If no
CPI series is configured, the calculator surfaces "buyout 1: not
available — CPI source not configured" rather than producing a wrong
number.

### What it is

This number reflects the exiting owner's **economic outlay** — what they
actually parted with, normalized for the time value of money — minus
their net inter-personal debt position. It treats the property as a
storage of contributed capital.

### What it is not

- It does **not** reflect property appreciation.
- It does **not** reflect their equity share of the asset's current value.
- It does **not** account for taxes, stamp duty, or transaction costs.

---

## Buyout Number 2 — Market value share

> "What your slice of the asset is worth today."

### Formula

```
buyout_2 = current_market_value_property_currency * (owner_equity_pct / 100)
        − owner_equity_pct / 100 * outstanding_bank_loan_principal_total
```

The second term subtracts the exiting owner's pro-rata share of any
**still-outstanding bank loan principal**. This is essential: if the
property is worth ₹15M but has ₹5M outstanding on a bank loan, an owner
exiting with their full equity slice cannot reasonably claim
₹5M (33% of ₹15M) — they would be walking away with their share of the
asset *and* their share of the debt obligation. Net of the debt, their
share is ₹3.33M.

### Required input: market value

The calculator requires a **manually entered current market value** in
the `market_value_snapshots` table. This is not auto-pulled — there is
no reliable real-estate API for the locations this app supports, and
even if there were, owners should agree on the valuation source.

Typical practice:

- Get one or more broker estimates and a recent comparable sales report.
- Owners agree on a number (or a small range).
- The agreed value is entered into `market_value_snapshots` with a
  `notes` field describing the source.

If no market value snapshot exists, the calculator surfaces "buyout 2:
not available — market value not entered" rather than guessing.

### What it is

This number reflects the exiting owner's **claim on the asset's current
worth**, net of their share of remaining bank debt. It treats the
property as a current-market-value investment.

### What it is not

- It does **not** reflect inter-personal debts/credits (those are
  separately settled, not folded into the asset valuation).
- It does **not** reflect the exiting owner's actual contribution
  history — an owner who paid less than their share gets the same
  buyout-2 as one who paid more, given equal equity.

---

## Buyout Number 3 — Weighted blend

> "A configurable mix of the two."

### Formula

```
buyout_3 = (buyout_1 * weight_contribution + buyout_2 * weight_market) /
           (weight_contribution + weight_market)
```

### Default weights

Default is **50/50** — equal weighting. The UI exposes the weights as
adjustable sliders so owners can experiment in real time during
negotiation:

> "What if we weight contribution at 70% and market at 30%? At
> 30/70?"

### Why a blend?

In practice, neither pure formula reflects the spirit of the
arrangement:

- Pure buyout-1 (contribution-only) ignores that the asset has
  appreciated (or depreciated) — an owner who bought in at a low value
  and exits at a high value is leaving real wealth on the table.
- Pure buyout-2 (market-only) ignores that the early contributors took
  on the downpayment risk — being paid out only on equity ratio means
  they were never compensated for fronting capital.

A blend is honest about both. The right blend is a negotiation, which is
why the calculator does not pick one.

### Configurability

Weights are stored on the `EXIT` event's `metadata`:

```json
{
  "buyout_formula": "weighted_blend",
  "weight_contribution": 0.6,
  "weight_market": 0.4,
  "buyout_amount": 4150000
}
```

This preserves the audit trail for later questions ("which weights did
we use last time?").

---

## Shared floor election

In the original use case, the property has one or more **shared floors**
— common-area space (a guest floor, a rooftop, a courtyard) that all
owners use jointly. When an owner exits, the question of what happens to
their share of the shared space must be resolved.

The exiting owner elects one of two options:

### Option A — Retain

The exiting owner keeps their pro-rata share of the shared floor (e.g.,
1/3 of a shared floor for one of three owners). They retain **non-equity
usage rights** to that share — they may continue to use the space, or
license it back to the remaining owners, depending on the legal
instrument.

This option is rare in practice — it creates an awkward post-exit
relationship — but is recorded for transparency.

### Option B — Dilute

The exiting owner's share of the shared floor is **absorbed by the
remaining owners**, distributed in proportion to their existing equity.
With three owners, two remaining each absorb half the exiting owner's
share. The remaining owners' shared-floor stake increases; the exiting
owner releases their stake.

This is more common and cleaner — the remaining owners now have full
shared-floor rights between themselves.

### How the election is recorded

On the `EXIT` event's `metadata`:

```json
{
  "shared_floor_election": "dilute"   // or "retain"
}
```

### Important caveat

> **The election has no automatic legal effect.** The application records
> the election as a written record between owners. Any actual transfer of
> shared-floor rights is a legal action — recorded in the property deed,
> registered with the appropriate authority, and executed by lawyers.

The app's job is to capture the agreed election clearly and durably. It
does not encode any legal authority.

---

## What the exit calculator does **NOT** do

Worth being explicit, because future contributors will be tempted to add
features here that should not be features:

| It does not | Because |
|-------------|---------|
| Initiate any legal transfer. | Legal action requires a lawyer, a registered deed, and authority filings. The app is not a legal instrument. |
| Adjust `owners.equity_pct` automatically. | Equity is frozen post-setup; the exit doesn't redistribute it within the app's data model. |
| Account for capital gains tax. | Tax is jurisdiction-, person-, and holding-period-specific. The CPA decides. |
| Account for TDS / withholding on the buyout. | Same as above — depends on residency status, payment route, etc. |
| Account for stamp duty on transfer. | A registration / legal cost, not a ledger event. |
| Calculate a "fair" number. | "Fair" is what the owners agree on. The app surfaces three formulas; humans interpret. |
| Negotiate or split the difference. | Out of scope for software. |
| Track the **completion** of the buyout. | The actual transfer of the buyout amount is recorded as separate `CONTRIBUTION` / `SETTLEMENT` events. The `EXIT` event records the agreed number; subsequent events record its settlement. |

---

## Worked example

> **Setup at exit (2030-09-01):**
>
> - Three owners: V, P, S, each at 33.33% equity since 2026 setup.
> - S decides to exit.
> - Property's current market value: **₹18,000,000** (entered as a
>   `market_value_snapshot`).
> - Outstanding bank loan principal across all property loans:
>   **₹3,000,000.**
> - S's CapEx contributions over the years (sum of CONTRIBUTIONs +
>   EMI_PAYMENT principal portions + BULK_PREPAYMENTs, each at their
>   event's `inr_landed`): **₹4,200,000 nominal.**
> - Inflation-adjusted to 2030 using India's CPI series:
>   **₹5,100,000.**
> - S's net inter-personal position: S is owed ₹150,000 by P; S owes
>   ₹0 to V. **Net credit: +₹150,000.**
>
> **Computations:**
>
> - **Buyout 1 (net contribution, inflation-adjusted):**
>   ```
>   = ₹5,100,000 + ₹150,000  =  ₹5,250,000
>   ```
> - **Buyout 2 (market value share):**
>   ```
>   share_of_value      = ₹18,000,000 * 33.33%  ≈  ₹5,999,400
>   share_of_loan_debt  = ₹3,000,000  * 33.33%  ≈  ₹999,900
>   buyout_2            = ₹5,999,400 − ₹999,900 =  ₹4,999,500
>   ```
> - **Buyout 3 (50/50 blend):**
>   ```
>   = (₹5,250,000 + ₹4,999,500) / 2  ≈  ₹5,124,750
>   ```
>
> The app shows: **₹5,250,000 / ₹4,999,500 / ₹5,124,750.**
> S, V, and P negotiate. They agree on ₹5,100,000. The
> `EXIT` event records `buyout_formula: "negotiated"`,
> `buyout_amount: 5100000`, `shared_floor_election: "dilute"`. The
> actual transfer is recorded as separate `CONTRIBUTION` events from
> V and P to S over the following months.

---

## Implementation note

The exit calculator is **Session 6** work. It depends on:

- `get_owner_contributions(...)` — Session 3.
- `get_loan_balance(...)` for each property loan — Session 3.
- `get_interpersonal_balance(...)` for each pair the exiting owner is in — Session 3.
- A CPI-adjustment helper using a configurable index series.

This document is the contract that Session 6's implementation must satisfy.
