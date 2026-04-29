# Computed Views

The ledger has **no stored balance columns** — see
[balances-and-equity.md](balances-and-equity.md). Every read-time
question ("what does P owe V?", "how much is left on Loan #1?", "what
have I contributed?") is answered by a projection over the event log.

To keep call sites simple and prevent ad-hoc raw queries against the
events table, the system exposes a fixed read interface:

- **Four SQL views** in `backend/db/schema.sql` for snapshot reads with
  no procedural logic.
- **One Python function** in `backend/core/balance.py` for the one
  read that requires procedural logic (interest-aware balances).

Consumers — the API in Session 4, the MCP tools in Session 5, the
frontend in Session 7 — are expected to go through these. Adding a new
read pattern means adding a new view, not building a new ad-hoc query.

---

## Why the interest-aware balance is Python, not SQL

`v_interpersonal_balances` (SQL) is principal only. Layering accrued
interest onto it requires walking events in `effective_date` order,
maintaining a running rate from `INTERPERSONAL_RATE_CHANGE` events,
splitting accrual intervals at every event boundary, and applying the
interest-first repayment ordering. That is procedural — straightforward
in Python, contortionist in SQL.

So the function `get_interpersonal_balance_with_interest(lender_id,
borrower_id, as_of_date, db)` lives in `backend/core/balance.py`. It
returns the same shape that a SQL view would, just one (lender,
borrower) pair at a time.

If a future iteration needs this as a SQL surface (e.g., for read
replicas or BI tooling), the answer is a materialized view refreshed by
a cron job that calls the Python function — not a rewrite of the
accrual logic in PL/pgSQL.

---

## `v_interpersonal_balances`

**Purpose:** principal balance per (lender, borrower) pair on a property,
as of "now" (no parameterized date — date filtering is applied by the
Python caller via `get_interpersonal_balance` when time-travel is
needed).

**Aggregates:** events with one of these types where `target_owner_id
IS NOT NULL`:

- `INTERPERSONAL_LOAN_DISBURSEMENT` (actor=lender, target=borrower,
  positive sign)
- `INTERPERSONAL_LOAN_REPAYMENT` (actor=borrower, target=lender,
  negative sign)
- `SETTLEMENT` (actor=payer, target=recipient, negative sign in payer
  direction)
- `OPEX_SPLIT` (actor=owner-of-share, target=payer, positive sign in
  payer direction)
- `COMPENSATING_ENTRY` rows that point at any of the above

**Filter conditions:** none in the view itself. Callers filter by
`property_id`, `lender_owner_id`, or `borrower_owner_id` on read.

**Returned columns:**
- `property_id` — the property the pair is scoped to
- `lender_owner_id` — the owner who is owed
- `borrower_owner_id` — the owner who owes
- `principal_balance_inr` — net `Decimal` in property currency
- `last_event_date` — most recent `effective_date` for this pair

**Excludes accrued interest.** The Python function
`get_interpersonal_balance_with_interest` layers that on. Kept separate
because tax / audit consumers often want principal alone.

**Performance:** the existing `idx_events_pair` index on
`(actor_owner_id, target_owner_id, effective_date)` covers the common
filter. For a property with thousands of events this is a sub-millisecond
scan.

**Implementation status:** live in `backend/db/schema.sql` (Session 3).

---

## `v_bank_loan_balances`

**Purpose:** outstanding principal per bank loan, derived from the
loan's original principal minus the principal components of all
amortizing and bulk payments made.

**Aggregates:**
- `bank_loans.principal_inr` (the starting value)
- `emi_schedule.principal_component` for rows where `status IN ('paid',
  'prepaid')`
- `BULK_PREPAYMENT` events against this loan (`amount_property_currency`)
- `COMPENSATING_ENTRY` rows pointing at any of the above

The interest component of EMIs **does not** reduce principal — it is
the bank's revenue. Including it would systematically under-state the
remaining balance.

**Filter conditions:** none in the view; callers filter by `loan_id`.

**Returned columns:**
- `loan_id`
- `lender_name` — joined from `bank_loans`
- `original_principal_inr`
- `total_paid_principal_inr` — sum of paid EMI principal components +
  bulk prepayments
- `outstanding_principal_inr` — clamped at `0` if the math underflows
- `last_payment_date` — most recent `paid_at::date` for this loan

**Performance:** trivial join — `bank_loans` is small, the
`idx_emi_schedule_status` index covers the EMI filter.

**Implementation status:** live in `backend/db/schema.sql` (Session 3).

---

## `v_owner_contributions`

**Purpose:** per-owner totals split by purpose (CapEx vs OpEx), in
property currency equivalent, for any property the owner is on.

**Aggregates:**
- **CapEx:** `CONTRIBUTION`, `EMI_PAYMENT` (principal component only),
  `BULK_PREPAYMENT` events where `actor_owner_id = owner`
- **OpEx:** `opex_splits.amount_owed_property_currency` joined to the
  parent OPEX_EXPENSE event, filtered to `owner_id = owner`
- For cross-currency events, `inr_landed` is used; for same-currency
  events, `amount_property_currency` is used. The view uses
  `COALESCE(inr_landed, amount_property_currency)` to apply this rule
  uniformly.

**Filter conditions:** none in the view; callers filter by
`(owner_id, property_id)`.

**Returned columns:**
- `owner_id`
- `property_id`
- `capex_inr`
- `opex_inr`
- `total_inr` — `capex_inr + opex_inr`

**Performance:** scans events by `actor_owner_id` (covered by
`idx_events_actor`) and `opex_splits` by `owner_id` (covered by
`idx_opex_splits_owner`).

**Implementation status:** live in `backend/db/schema.sql` (Session 3).

---

## `v_emi_upcoming`

**Purpose:** next pending EMIs across all active loans for the dashboard
"what's due next" tile.

**Aggregates:** `emi_schedule` rows where `status = 'pending'`, joined
to `bank_loans` for `lender_name`.

**Filter conditions:** none in the view; callers usually `ORDER BY
due_date ASC LIMIT N`.

**Returned columns:**
- `loan_id`
- `lender_name`
- `due_date`
- `principal_component`, `interest_component`, `total_emi_inr`
- `paid_by_owner_id` — usually `NULL` for pending rows; populated when
  a future-dated row has been pre-assigned to an owner

**Performance:** the `idx_emi_schedule_pending` partial index covers
the filter exactly.

**Implementation status:** live in `backend/db/schema.sql` (Session 3).

---

## `get_interpersonal_balance_with_interest` (Python)

**Purpose:** the interest-aware variant of `v_interpersonal_balances`.
Returns principal + accrued interest as of an arbitrary date.

**Signature:**
```python
async def get_interpersonal_balance_with_interest(
    lender_id, borrower_id, as_of_date, db,
) -> dict
```

**Returns:**
```python
{
    "lender_id": UUID,
    "borrower_id": UUID,
    "as_of_date": date,
    "principal_inr": Decimal,
    "accrued_interest_inr": Decimal,
    "total_owed_inr": Decimal,
}
```

**Why this is Python, not SQL:** the accrual engine has to walk events
in order, maintain a running principal and rate, split intervals at
every event, apply the interest-first repayment waterfall, and account
for `COMPENSATING_ENTRY` negations. That is straightforward Python and
unreasonable PL/pgSQL.

**Implementation status:** live in `backend/core/balance.py`
(Session 3).
