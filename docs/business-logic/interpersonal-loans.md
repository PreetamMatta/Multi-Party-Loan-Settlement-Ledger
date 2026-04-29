# Inter-Personal Loans

When one owner contributes more than their equity share calls for, that
overpayment is not a gift, and it does not buy them more equity. It is a
**loan from that owner to the others.** This document explains how
inter-personal loans are modeled, how interest accrues, and how per-financial-year
statements are produced for tax filing.

---

## What an inter-personal loan is

> When one owner pays more than their equity share requires — fronting the
> downpayment, making a bulk prepayment, covering another owner's EMI —
> they are not gaining equity. They are making a loan to the other
> owner(s). The loan is tracked **per lender↔borrower pair.**

Inter-personal loans arise naturally because owners contribute unevenly:

- The biggest earner tends to front the downpayment when the property is
  purchased — their cash flow allows it. The other owners owe their share
  back over time.
- During financial windfalls (bonus, business profit, savings), one owner
  may make a bulk prepayment that covers more than their pro-rata share —
  the excess is owed back by the others.
- One owner may pay another owner's EMI when the latter is short on cash
  for a month — that creates a same-month debt to be settled later.

Each of these events generates **one or more** `INTERPERSONAL_LOAN_DISBURSEMENT`
events plus the underlying movement event (CONTRIBUTION, EMI_PAYMENT, etc.).

### Per-pair, not per-property

A loan exists between **two specific owners** — a lender and a borrower.
Three owners produce up to six pair relationships (V→P, V→S, P→V, P→S,
S→V, S→P), each tracked independently.

**Net balances are not collapsed across pairs.** If V owes P ₹10,000 and
P owes V ₹15,000, those are two separate balances of ₹10,000 and
₹15,000 respectively. Periodically, owners may agree to net them
manually by recording a `SETTLEMENT` event in each direction — the app
does not auto-net, because each direction may have different interest
rates, different histories, and tax-relevant per-direction interest
accruals.

### Equity is unaffected

This is the most important point and the one that will be questioned
repeatedly: **lending money does not increase your equity.** A wealthy
owner who funds 90% of the property's purchase price still holds their
agreed equity percentage (e.g., 33.3%). The other 56.7% they fronted is a
loan, not an ownership interest.

The reverse is also true: a borrowing owner's equity is unchanged by
having a debt. They still own their full agreed share of the asset, but
they owe their co-owner(s) money on top of that.

---

## The interest model

Each lender↔borrower pair has a **configurable annualized interest rate**
stored on `interpersonal_loans.current_rate_pct`. The default is **0%**.

Owners may agree to change the rate at any time — for example, the lender
may want compensation for opportunity cost as the loan ages, or family
circumstances may change. Rate changes are recorded as
`INTERPERSONAL_RATE_CHANGE` events.

### Forward-only, never retroactive

Rate changes apply **from the change's `effective_date` forward.** They
never accrue retroactively on the existing principal. This is a hard rule:

- It matches how most informal family lending agreements actually work.
- It prevents lender-favorable revisions from being weaponized later.
- It produces a clean audit trail: each interest period has a clearly
  defined rate.

### Worked example of forward-only accrual

> **Setup:** V disburses ₹500,000 to P on 2026-01-01 with no interest
> agreed (rate = 0%). On 2026-06-01, V and P agree (in writing, recorded
> as an `INTERPERSONAL_RATE_CHANGE` event) that interest will accrue at
> 3% p.a. going forward.
>
> **From 2026-01-01 through 2026-05-31:** No interest accrues. P owes
> ₹500,000 flat.
>
> **From 2026-06-01 onward:** Interest accrues at 3% p.a. on the
> outstanding balance:
> ```
> daily_interest_per_₹100k = ₹100,000 * 3% / 365 = ~₹8.22 per day per ₹100k
> ```
> If no repayments are made between 2026-06-01 and 2026-12-31 (214
> days), accrued interest is approximately:
> ```
> ₹500,000 * 3% * 214 / 365 = ₹8,795.62
> ```
> The original principal of ₹500,000 is **not** retroactively charged
> for the Jan–May period — only the post-June period accrues.

---

## Accrual math — chosen approach

> **The chosen accrual model is: simple interest, daily, on the
> outstanding principal, day-count convention `actual/365`.**

This is a deliberate choice. Alternatives considered and rejected:

| Approach | Rejected because |
|----------|------------------|
| Compound (daily) | Adds complexity. For 0–5% rates over short periods (the typical case here), the compounding effect is negligible. Family lending norms expect simple interest. |
| Compound (monthly) | Same as above — and now the monthly boundary becomes a bookkeeping artifact. |
| Period-based simple (per month) | Simpler to compute by hand, but introduces edge cases at month boundaries (a rate change mid-month requires pro-rating the period anyway). |
| Actual/360 | A bank convention that doesn't match family lending. Actual/365 is more intuitive (a year is a year). |

### The math

For each contiguous interval `[t1, t2]` between rate-change events, with
constant rate `r` and an outstanding principal `P` at the start of the
interval:

```
days_in_interval = (t2 - t1).days
interest         = P * r * days_in_interval / 365
```

When a `INTERPERSONAL_LOAN_DISBURSEMENT` or
`INTERPERSONAL_LOAN_REPAYMENT` (or `SETTLEMENT`) event occurs **within**
an interval, the interval is split at that event's `effective_date`,
the principal is updated, and the formula is applied again to each
sub-interval.

Accrued interest is **summed** but not capitalized. It does not become new
principal that itself earns interest. This is the simple-interest
contract.

### First-day rule (no interest on day of disbursement)

Interest accrues from the day **after** disbursement, not the day of.
The formula `(t2 - t1).days` enforces this naturally: a disbursement
on day D and a query on day D+1 produces `1` day of interest, not `2`.
A disbursement on day D queried on the same day D produces `0` days of
interest. Practical consequence:

> ₹100,000 disbursed on 2026-04-01 at 6% p.a., queried as of
> 2026-04-01, accrues `Decimal('0')` interest. Queried as of
> 2026-04-02, accrues `100000 * 0.06 * 1 / 365` ≈ `Decimal('16.44')`.

This is the conventional banking choice (the lender doesn't earn for a
day the borrower didn't yet have the money) and is what the chosen
arithmetic produces. Changing this rule would change every prior balance.

### Leap years still use 365

The `actual/365` convention deliberately uses `365` regardless of leap
year. A 366-day calendar year still divides by `365`, producing slightly
more than 1.0 years of interest in a leap year — this is the intentional
quirk of the convention. Switching to `actual/actual` (where the divisor
is the actual length of the year) would silently shift every existing
balance.

### Zero-rate periods in FY statements

If `current_rate_pct = 0` for some interval, the FY statement still
emits a row for that interval with `rate_pct: 0.0`, `interest: 0`. It
is **not** omitted. This makes it explicit that the period was reviewed
and the rate was zero (rather than the period being silently skipped),
and it preserves the audit trail when a rate later changes from zero.

### When accrued interest is paid

Accrued interest is a derived quantity. It is **not stored** anywhere. To
"pay it down," the borrower makes a regular `INTERPERSONAL_LOAN_REPAYMENT`
event; balance computation applies the repayment first against accrued
interest, then against principal (per the standard accounting waterfall).

This is implementation policy and lives in `backend/core/balance.py`. If
the team ever wants to change the waterfall (e.g., principal-first), this
document is updated first, then the code.

---

## Per-financial-year statements

The app generates a **per-pair, per-financial-year statement** suitable for
tax filing. Both jurisdictions the system supports are documented:

| Jurisdiction | Financial year | Why this matters |
|--------------|----------------|------------------|
| India | April 1 → March 31 | The Income Tax Act uses this year. Interest income on inter-personal loans is taxable in India even between cousins; though the cousins-as-relatives gift treatment under §56 is favorable for principal transfers, **interest** received is still income. |
| US | January 1 → December 31 | Interest received is taxable income to the US lender; if the recipient ($600+ from any single payer in a calendar year) the lender may need to issue/receive 1099-INT-equivalent disclosures. |

### What's in a statement

For a given (lender, borrower, financial-year), `generate_fy_statement()` returns:

```python
{
  "lender_id":                    UUID,
  "borrower_id":                  UUID,
  "calendar":                     "IN" | "US",   # "IN" = Indian FY (Apr–Mar), "US" = calendar year
  "financial_year":               int,            # the starting year
  "fy_start":                     date,
  "fy_end":                       date,
  "opening_balance_inr":          Decimal,        # principal owed at fy_start (principal only)
  "closing_balance_inr":          Decimal,        # principal owed at fy_end (principal only)
  "total_interest_accrued_inr":   Decimal,
  "total_disbursed_inr":          Decimal,
  "total_repaid_inr":             Decimal,
  "monthly_breakdown":            list[dict],     # one entry per calendar month (always 12 rows)
  # each monthly_breakdown entry:
  # {
  #   "month":            "YYYY-MM",
  #   "opening_balance":  Decimal,     # principal at month start
  #   "disbursements":    Decimal,
  #   "repayments":       Decimal,
  #   "interest_accrued": Decimal,
  #   "closing_balance":  Decimal,     # principal at month end
  # }
  "events":                       list[dict],     # flat audit list of all FY events
  # each events entry:
  # {
  #   "effective_date": date,
  #   "event_type":     str,
  #   "amount_inr":     Decimal,
  #   "description":    str | None,
  # }
}
```

> **Note on `interest_accrued_per_period`:** A per-rate-change-period breakdown
> (showing `{from, to, rate_pct, principal_at_start, interest}` for each
> rate-constant interval) is planned for Session 6. Until then, use
> `monthly_breakdown` for a month-level view and `total_interest_accrued_inr`
> for the FY total. The monthly interest figures are independently verifiable
> from the accrual math in [Accrual math — chosen approach](#accrual-math--chosen-approach).

> **Note on `opening_balance_inr` / `closing_balance_inr`:** these are
> **principal only** — they do not include accrued interest. Use
> `get_interpersonal_balance_with_interest()` for the combined view.

### Who needs this

- **The lender (US tax filing):** Lender reports interest received as
  taxable income on Schedule B. The statement provides the supporting
  computation.
- **The lender (Indian tax filing):** Interest is taxable; the statement
  provides the supporting computation. (For loans within the IT Act's
  definition of "relatives," the *principal* transfer is gift-tax-exempt,
  but **interest is income**.)
- **The borrower:** No tax deduction generally available for personal
  borrowing (unlike mortgage interest), but the statement helps with
  personal record-keeping.
- **A future auditor** asking "what did this loan cost you?" can read the
  statement.

### Form 15CA / 15CB references

When an Indian-resident owner makes a payment from an Indian bank account
to a US-resident owner (e.g., V repays a USD-denominated debt to P), they
may be required to file Form 15CA / 15CB before the bank releases the
remittance. The reference numbers on those forms should be stored in the
event's `metadata` JSONB field:

```json
{
  "form_15ca_ref": "ABCD1234",
  "form_15cb_ca_membership": "FCA-12345",
  "form_15cb_pdf_doc_id":    "<documents.id UUID>"
}
```

The statement includes any such references in the rendered output for
that FY.

---

## Balance computation

The exact algorithm for computing "amount owed by `borrower_id` to
`lender_id` as of `as_of_date`" by replaying the event log:

### Step 1 — pull events for the pair

Find all events satisfying:

```
property_id      = <property_id>
effective_date  <= as_of_date
AND (
   (actor_owner_id = lender_id AND target_owner_id = borrower_id)
   OR
   (actor_owner_id = borrower_id AND target_owner_id = lender_id)
)
```

Order by `effective_date ASC, recorded_at ASC` (stable tie-breaking by
recorded_at means same-day events are applied in the order they were
written).

### Step 2 — apply each event with its sign

| Event type | Direction | Effect on `balance(borrower owes lender)` |
|------------|-----------|-------------------------------------------|
| `INTERPERSONAL_LOAN_DISBURSEMENT` | actor=lender, target=borrower | +amount |
| `INTERPERSONAL_LOAN_REPAYMENT` | actor=borrower, target=lender | −amount |
| `SETTLEMENT` (any non-monetary value transfer) | actor=borrower, target=lender | −amount |
| `SETTLEMENT` (reverse direction) | actor=lender, target=borrower | +amount (lender gave value to borrower with no countervailing transfer; treated as additional disbursement of equivalent value) |
| `OPEX_SPLIT` | actor=borrower, target=lender (where lender paid the OpEx) | +amount |
| `INTERPERSONAL_RATE_CHANGE` | actor=lender, target=borrower | 0 (no principal effect; only future accrual) |
| `COMPENSATING_ENTRY` | reverses one of the above | apply negation as recorded on the compensating entry's amount field |

### Step 3 — interleave interest accrual

Walk the timeline of `INTERPERSONAL_RATE_CHANGE` events for the pair.
Within each rate-constant interval, compute simple interest on the
outstanding principal using actual/365 day-count. Sum across intervals
within `[earliest disbursement effective_date, as_of_date]`.

Add accrued interest to the principal balance as a single line item
(it does not capitalize).

### Step 4 — handle compensating entries

Compensating entries already carry negated amounts (see
[event-log.md — Compensating entries](event-log.md#compensating-entries--the-correction-mechanism)),
so they are summed with their natural sign. No special-case handling is
needed beyond ensuring they are included in step 1.

### Step 5 — return signed result

Return a `Decimal` representing what the borrower owes the lender.

- **Positive:** borrower owes lender.
- **Zero:** settled.
- **Negative:** lender owes borrower (i.e., the labels in the function
  arguments were reversed; the UI should render this as the lender being
  the debtor in this pair).

---

## Python API

Implemented in `backend/core/interest.py` — all functions are async and
read-only against the events log. They never write.

```python
async def calculate_accrued_interest(
    lender_id, borrower_id, period_start, period_end, db
) -> Decimal
```
Total simple interest accrued on the (lender, borrower) loan within
`[period_start, period_end]` (both inclusive). Walks all events for the
pair from inception to `period_end`, maintaining a running principal and
rate history; sums interest only for interval portions that overlap the
requested window. Returns `Decimal('0')` for zero-rate periods.

```python
async def generate_fy_statement(
    lender_id, borrower_id, financial_year, calendar="IN", db=...
) -> dict
```
Per-financial-year statement for tax filing. `calendar="IN"` covers
April 1 → March 31 of the next year; `calendar="US"` covers the
calendar year. Returns opening / closing balance, totals, monthly
breakdown (every month present, including zero-activity months), and a
flat events list for audit.

---

## A note on what gets recorded vs what doesn't

When V wires ₹900,000 toward the downpayment and the equal-share intent
was ₹300,000 each:

- **One** `CONTRIBUTION` event records the wire (the actual money movement).
- **Two** `INTERPERSONAL_LOAN_DISBURSEMENT` events record the resulting
  debts (V→P for ₹300,000, V→S for ₹300,000).

The DISBURSEMENT events are the bookkeeping translation of "V paid more
than his share." They live in the events table at the same status as the
CONTRIBUTION — none is more important than another, all are signed,
all are immutable.

The application layer is responsible for emitting the correct
DISBURSEMENT events when an over-share contribution is recorded. This
logic lives in the API layer (Session 4); the event log is the dumb,
faithful recorder.
