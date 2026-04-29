# Balances and Equity

This document explains the most-confused pair of concepts in the system:
**equity** vs **balance.** They sound similar, both involve money, both
relate to ownership — but they answer different questions and are governed
by entirely different rules. Conflating them is the single most common
mistake new contributors make.

---

## The fundamental distinction

| | Equity | Balance |
|---|--------|---------|
| **Question it answers** | "What share of the asset do you own?" | "What money obligations exist between owners and bank loans, today (or on date X)?" |
| **When it's set** | At property setup (t=0). | Continuously, derived from event replay. |
| **Mutability** | Frozen after t=0 (with one exception — see below). | Changes with every event. |
| **Stored in** | `owners.equity_pct` (a column). | Nowhere. Always projected from events. |
| **Affected by contributions** | **No.** | Yes. |
| **Affected by inter-personal loans** | **No.** | Yes. |
| **Used in exit calculation** | Yes — drives the market-value-share buyout. | Yes — drives the net-contribution buyout. |

### A scenario that makes the distinction concrete

> Three owners: V, P, S. Equity 33.33% / 33.33% / 33.34% by setup
> agreement. The property cost ₹15,000,000 with a ₹6,000,000 bank
> loan.
>
> At purchase, V fronts the entire ₹9,000,000 downpayment because P and
> S are short on liquidity that month. P and S each agreed to repay V
> their share over time.
>
> **State the day after purchase:**
> - V's equity: 33.33% (unchanged — he did not buy more of the asset)
> - V's balance vs P: P owes V ₹3,000,000
> - V's balance vs S: S owes V ₹3,000,000
>
> Three years later, P has paid back ₹2,500,000 of the ₹3,000,000:
> - V's equity: still 33.33%
> - V's balance vs P: P owes V ₹500,000 (plus any accrued interest)
> - V's balance vs S: depends on S's repayments, but still independent of equity
>
> P paying V back **never** changes equity. It only reduces the
> inter-personal balance.

This is the answer to "but V paid more, so doesn't V own more?" — no. V
**lent** more. Lending is not buying.

---

## What "balance" means in each context

The word "balance" is overloaded. The application uses it for several
distinct quantities; understanding which one is which prevents accidental
mixing.

### Inter-personal balance

The net amount one owner owes another, summed across all inter-personal
loans, settlements, and OpEx splits between them. Per **lender↔borrower
pair**, computed by replaying events.

- Type: `Decimal`, in property currency.
- Sign: positive = borrower owes lender; zero = settled; negative = direction
  is reversed (UI swaps labels).
- Source: see [interpersonal-loans.md — balance computation](interpersonal-loans.md#balance-computation).

### Bank loan balance

The outstanding **principal** on a single bank loan as of a given date.
Computed by:

```
balance = original_principal
       − sum of EMI principal components paid (effective_date <= as_of_date)
       − sum of BULK_PREPAYMENT amounts (effective_date <= as_of_date)
       − applicable COMPENSATING_ENTRY adjustments
```

Note: the **interest portion** of EMI payments is *not* deducted from
the balance — interest is the bank's revenue, not principal repayment.
Each EMI carries a `principal_component` and an `interest_component`
in `emi_schedule`; only the principal reduces the balance.

### Contribution total (per owner)

How much an owner has contributed to the property up to a given date,
expressed in property currency equivalent. Split by purpose:

- **CapEx contributions:** money used to acquire or pay down the asset
  itself — downpayment, EMI principal payments, bulk prepayments. These
  build long-term ownership value (though, again, do not shift equity —
  they reduce the bank loan balance and reduce inter-personal debts).
- **OpEx contributions:** ongoing carrying costs an owner has paid —
  property tax, HOA, shared utilities, common-area furnishing. These do
  not build equity; they keep the asset operational.

The split matters because:

- The exit calculator's **net-contribution buyout** uses CapEx (which
  represents asset-building outlay, adjusted for inflation), not OpEx.
- Tax treatment differs (in many jurisdictions, OpEx may be deductible
  against rental income; CapEx is part of cost basis for capital gains).
- Owners want to know "how much of my money is in the building" vs "how
  much have I burned on property tax."

The credit amount used for both is `inr_landed` (the dual-rate FX rule —
see [fx-and-wire-transfers.md](fx-and-wire-transfers.md)).

---

## The projection model

> **Balances are never stored as columns in the database. They are
> computed on read by replaying the event log filtered by `effective_date`.**

This is one of the non-negotiable architectural rules of the system. The
implications:

### Time-travel queries are free

Any query of the form "balance as of date X" is the same code path as
"balance today" — you just pass a different `as_of_date`. The replay
engine doesn't care; it filters events on `effective_date <= as_of_date`
and sums.

This is enormously powerful for:

- **Auditing:** "What did P owe V on the day P claimed to have repaid in
  full?" — answerable directly.
- **Tax reporting:** "What was the loan balance on March 31, 2027?" —
  answerable directly.
- **Dispute resolution:** "On the day S exited, what were the
  inter-personal balances?" — answerable directly.

A traditional schema with a stored balance column would force you to
maintain a separate balance-snapshot table, double-write on every event,
and pray they stay consistent. We don't have that problem.

### There is no stale cache to invalidate

If a `COMPENSATING_ENTRY` is written today for an event from 2 years
ago, all historical balance queries for any date between then and now
**immediately** reflect the correction. No cache needs to be busted, no
index needs to be rebuilt — the next replay just sees the new row.

This is the second-order benefit of append-only + projections: corrections
propagate everywhere automatically.

### The cost: every read is a replay

The cost is that every balance read does an `O(events for this scope)`
scan. At this application's scale, that is acceptable:

- A single property generates thousands of events over 20+ years, not
  millions.
- The query is a simple aggregation that is trivially indexed (the
  schema's `idx_events_property_effective` and `idx_events_pair`
  indexes cover the common access patterns).
- If performance ever becomes a concern, a per-(scope, as_of_date)
  snapshot can be materialized as an optimization — without changing the
  source of truth.

We do **not** materialize snapshots in v1. Premature optimization is the
root of all sorts of bugs in financial software.

---

## Projection contract

These four rules are the contract every projection function in
`backend/core/balance.py` honors. They are written here so that any
re-implementation, optimization, or alternative read path obeys the same
semantics.

### 1. Event replay order

Events are replayed in `effective_date ASC, recorded_at ASC` order. The
`recorded_at` tiebreaker matters when two events share the same business
date: the order of insertion is the order they apply, so a same-day
disbursement followed by same-day repayment nets correctly.

### 2. `as_of_date` uses `effective_date`

A query for `as_of_date = X` replays all events where
`effective_date <= X`. Events with `effective_date > X` are excluded
even if `recorded_at <= X`. The reverse — `recorded_at` filtering — is
**not** a balance contract; it is an audit-trail filter only.

### 3. `COMPENSATING_ENTRY` semantics

A compensating entry's financial effect is the negation of the original
event's effect. The projection engine applies it at the **compensating
entry's own `effective_date`**, not the original's. (In practice the two
dates are usually equal — see the rules in `event-log.md` — but a
correction made on a different effective date applies on that date.)
Both rows live in the log forever; balances simply sum them.

### 4. Currency normalization

All balances are reported in the property currency (`property_currency`
on `properties`, e.g. `INR`). Cross-currency events are credited at
`inr_landed` (the dual-rate rule); same-currency events are credited at
`amount_property_currency`. The projection engine never multiplies
`amount_source_currency × fx_rate_actual` — that quantity excludes wire
fees and would over-credit the sender.

---

## Python API

Implemented in `backend/core/balance.py` — all functions are async and
read-only.

```python
async def get_interpersonal_balance(
    lender_id, borrower_id, as_of_date, db
) -> Decimal
```
Net principal owed by `borrower_id` to `lender_id` as of `as_of_date`.
Positive = borrower owes lender. Principal only — does **not** include
accrued interest (see `get_interpersonal_balance_with_interest` for the
combined view).

```python
async def get_loan_balance(loan_id, as_of_date, db) -> Decimal
```
Outstanding principal on a single bank loan as of `as_of_date`. Floors
at zero to guard against data-entry errors that would otherwise produce
a negative outstanding.

```python
async def get_owner_contributions(
    owner_id, property_id, as_of_date, db
) -> dict
```
Returns `{capex_inr, opex_inr, total_inr, event_count}` for the owner up
to `as_of_date`, in property currency. CapEx aggregates CONTRIBUTION,
EMI principal components, and BULK_PREPAYMENT; OpEx aggregates the
owner's share of OPEX_SPLIT rows.

```python
async def project_exit_scenario(
    owner_id, property_id, market_value_property_currency, db,
    blend_weight_contribution=Decimal("0.5"),
    blend_weight_market=Decimal("0.5"),
    as_of_date=None,
) -> dict
```
Returns the three buyout numbers (net contribution, market-value share,
weighted blend), the inputs used, the equity percentage, blend weights,
and the outstanding loan totals used in Buyout #2. `as_of_date` defaults
to `date.today()`; pass an explicit date for historical queries or
deterministic tests. CPI inflation adjustment for Buyout #1 is reserved
for Session 6.

```python
async def get_interpersonal_balance_with_interest(
    lender_id, borrower_id, as_of_date, db
) -> dict
```
Composite view that calls both `get_interpersonal_balance` and
`calculate_accrued_interest` and returns
`{principal_inr, accrued_interest_inr, total_owed_inr}`. Kept in Python
rather than SQL because rate-change accrual requires procedural logic
(see [computed-views.md](computed-views.md)).

---

## The equity adjustment (one-time, t=0)

The single legitimate exception to "equity is set at setup and frozen" is
the one-time **floor-premium adjustment** at t=0.

### When it's used

If owners bid on preferred units (e.g., the top floor) and the winner
agrees to take a slightly larger equity stake to compensate the others
for forgoing the preferred unit. Example: three cousins, all want the
top floor, V wins; the agreed split is **35% / 32.5% / 32.5%** rather
than equal thirds.

### How it's recorded

A single `EQUITY_ADJUSTMENT` event per affected owner, written **at
property setup only**. The event:

- Updates `owners.equity_pct` to the new value.
- Records `metadata.previous_equity_pct` and `metadata.new_equity_pct`.
- Records `metadata.reason` ("Floor premium for top-floor allocation").
- Has the same HMAC signing as any other event.

After this initial setup, **no further `EQUITY_ADJUSTMENT` events are
allowed** for the property. The application layer enforces this; if a
later session attempts to write one, the API rejects it. This is the
one place where the schema's append-only nature is supplemented by an
application-layer constraint.

### What it is **not** for

- Not for "P contributed more, give P more equity." That's an
  inter-personal loan. It does not change equity.
- Not for "V wants to buy out S's share." That's an exit, not an
  adjustment.
- Not for "we miscounted at setup." That should be done as a
  `COMPENSATING_ENTRY` against the bad `EQUITY_ADJUSTMENT` plus a new
  correct one — within the t=0 window.

### Why it's frozen

Allowing equity to drift over time would:

- Defeat the audit trail (someone could shift equity in their favor by
  recording a string of small adjustments).
- Make exit calculations incoherent (the market-value-share number
  depends on a stable equity %).
- Open arguments about every contribution: "I paid more this year, my
  equity should go up" — which would defeat the whole point of using
  an inter-personal loan model.

The fixed-equity contract is a **feature** of this system, not a
limitation. If owners want to truly restructure ownership, that is a
legal restructure outside the app — the app records the result via an
`EXIT` and a re-setup of the property entity.
