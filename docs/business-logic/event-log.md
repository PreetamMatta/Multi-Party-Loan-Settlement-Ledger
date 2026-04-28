# The Event Log

The `events` table is the heart of this system. Every financial fact about
the property — every contribution, every wire, every settled dinner, every
correction — is recorded as a single immutable row in this table. The current
state of the world is *not* stored anywhere; it is computed by replaying the
event log.

This document explains the rationale, the taxonomy of event types, the
tamper-evidence mechanism, and the rules for correcting mistakes.

---

## Why append-only?

> **An event is an immutable record of something that happened in the
> financial relationship. It is not a representation of current state. It is
> a fact about the past. Once written, it cannot be changed — only negated by
> a compensating entry.**

The decision to make the ledger append-only — never `UPDATE`, never `DELETE`
— is the most important architectural decision in the entire system. The
reasons:

1. **Tamper-evidence.** If the row "P contributed ₹500,000 on March 3rd" is
   silently edited to ₹50,000 a year later, no one will know. With an
   append-only log plus HMAC signatures, *any* alteration to a row is
   detectable on re-verification.

2. **Time-travel queries are free.** "What did P owe V on 2031-03-15?" is
   answerable by replaying the log filtered to events with
   `effective_date <= 2031-03-15`. There is no "what did the balance column
   read at the time?" — there is no balance column.

3. **Auditability across 20+ years.** A tax authority, a lawyer, or a
   suspicious co-owner can trace every rupee from inception. Each event row
   carries who recorded it, when, on whose authority, and the original raw
   amounts before any aggregation.

4. **Eliminates "who changed what" disputes.** With three or more co-owners
   over a multi-decade horizon, the question "who edited this?" *will* arise.
   With this design, the answer is always "no one — here's the original event
   and here's the compensating entry."

5. **Trust mechanism.** This is not just a technical choice. It is the
   contract between co-owners. The fact that nobody — not even the
   application's authors — can secretly mutate history is what makes the
   ledger trustworthy.

---

## The full event type taxonomy

There are **13** event types. Every one is enumerated below with:

- What it represents in plain English
- Who the `actor_owner_id` is and who the `target_owner_id` is (if applicable)
- What financial effect it has on which balances
- Which fields are expected to be populated
- A concrete worked example using example owners V, P, S

### `CONTRIBUTION`

A payment from an owner toward the property. This is the generic "money in"
event for equity-building flows: downpayment components, top-ups, ad-hoc
deposits used to fund the property.

- **`actor_owner_id`** — the owner who paid.
- **`target_owner_id`** — usually `NULL` (paid to the property, not to a person).
- **Financial effect:** Increases the actor's CapEx contribution total. Does
  *not* shift equity (equity is fixed; over-contribution becomes an
  inter-personal loan). Does *not* directly reduce a bank loan unless the
  contribution is specifically routed to one — that's an `EMI_PAYMENT` or
  `BULK_PREPAYMENT`.
- **Expected fields:** `amount_source_currency`, `source_currency`,
  `amount_property_currency`, `fx_rate_actual`, `fx_rate_reference`,
  `fee_source_currency`, `inr_landed`, `effective_date`, `description`.
- **Example:** *"V wires $5,000 USD to the property's INR account on
  2026-05-01. The bank applies 83.20 INR/USD; mid-market reference is 83.45.
  Wire fee is $25. Recorded as a CONTRIBUTION with
  amount_source_currency=$5,000, fee_source_currency=$25, fx_rate_actual=83.20,
  fx_rate_reference=83.45, inr_landed=$4,975 × 83.20 = ₹413,920."*

### `EMI_PAYMENT`

A scheduled EMI paid to a specific bank loan. Distinct from `CONTRIBUTION`
because it is tied to a known schedule row and reduces a bank loan's
outstanding principal (by the principal component of that EMI only).

- **`actor_owner_id`** — the owner who paid the EMI.
- **`target_owner_id`** — `NULL` (paid to the bank).
- **`loan_id`** — required: which bank loan this EMI services.
- **Financial effect:** Reduces the loan's outstanding principal by the EMI's
  principal component (the interest component does not reduce principal —
  that goes to the bank as carrying cost). Increases the actor's CapEx
  contribution total by the principal portion.
- **Expected fields:** `loan_id`, `amount_property_currency`,
  `fx_rate_actual` (if cross-currency), `effective_date`, plus a `metadata`
  payload identifying which `emi_schedule.id` row was paid.
- **Example:** *"P pays the May EMI of ₹50,000 (₹35,000 principal + ₹15,000
  interest) directly to HDFC for Loan #1 on 2026-05-15. Recorded as
  EMI_PAYMENT, loan_id=Loan#1, amount_property_currency=₹50,000, with
  metadata containing the principal/interest split and the
  emi_schedule.id."*

### `BULK_PREPAYMENT`

An above-EMI principal payment made to reduce a bank loan faster. Behaves
like `EMI_PAYMENT` but is unscheduled and goes 100% to principal.

- **`actor_owner_id`** — the owner making the prepayment.
- **`target_owner_id`** — `NULL`.
- **`loan_id`** — required.
- **Financial effect:** Reduces the loan's outstanding principal by the full
  amount. Increases the actor's CapEx contribution total by the full amount.
- **Expected fields:** Same as `EMI_PAYMENT`. No principal/interest split
  needed (it's all principal).
- **Example:** *"V receives a bonus and prepays ₹500,000 on Loan #1 on
  2026-12-01. Recorded as BULK_PREPAYMENT, loan_id=Loan#1,
  amount_property_currency=₹500,000."*

### `INTERPERSONAL_LOAN_DISBURSEMENT`

When one owner fronts money that creates a debt owed by another owner. This
is recorded *in addition to* the underlying movement (e.g., a CONTRIBUTION).
It is the bookkeeping entry that says "V paid ₹300,000 of P's share — P now
owes V ₹300,000."

- **`actor_owner_id`** — the **lender** (the owner who fronted the money).
- **`target_owner_id`** — the **borrower** (the owner whose share was covered).
- **Financial effect:** Increases the inter-personal balance owed by
  `target_owner_id` to `actor_owner_id`.
- **Expected fields:** `actor_owner_id`, `target_owner_id`,
  `amount_property_currency`, `effective_date`, `description`.
- **Example:** *"V wires ₹900,000 toward the downpayment, but each owner's
  share was supposed to be ₹300,000. V covered P's and S's shares. Two
  INTERPERSONAL_LOAN_DISBURSEMENT events are recorded: actor=V, target=P,
  amount=₹300,000; actor=V, target=S, amount=₹300,000."*

### `INTERPERSONAL_LOAN_REPAYMENT`

A repayment that reduces an inter-personal debt.

- **`actor_owner_id`** — the **borrower** (the one paying back).
- **`target_owner_id`** — the **lender** (the one being paid).
- **Financial effect:** Decreases the inter-personal balance owed by
  `actor_owner_id` to `target_owner_id`.
- **Expected fields:** `actor_owner_id`, `target_owner_id`,
  `amount_property_currency`, `effective_date`, `description`.
- **Example:** *"P sends V $1,200 USD via Zelle on 2026-09-01. At
  fx_rate_actual=83.10, ₹99,720 lands. Recorded as
  INTERPERSONAL_LOAN_REPAYMENT, actor=P, target=V,
  amount_property_currency=₹99,720."*

### `INTERPERSONAL_RATE_CHANGE`

A change to the interest rate on a specific lender↔borrower inter-personal
loan pair. **Applies forward only — never retroactively.**

- **`actor_owner_id`** — the lender.
- **`target_owner_id`** — the borrower.
- **Financial effect:** None on the existing principal. From `effective_date`
  forward, interest accrues at the new rate. Past accrual is unchanged.
- **Expected fields:** `actor_owner_id`, `target_owner_id`, `effective_date`,
  `metadata.new_rate_pct` (the new annualized rate as a `Decimal`),
  optionally `metadata.previous_rate_pct`.
- **Example:** *"V and P originally agreed to 0% interest on the
  ₹500,000 loan. On 2027-04-01, V asks P to start paying 3% p.a. going
  forward. Recorded as INTERPERSONAL_RATE_CHANGE, actor=V, target=P,
  effective_date=2027-04-01, metadata={new_rate_pct: 3.0,
  previous_rate_pct: 0.0}. Interest from 2027-04-01 forward is
  ₹500,000 × 3% / 365 per day until further repayments or rate changes."*

### `SETTLEMENT`

An off-ledger value transfer that reduces an inter-personal balance. Examples:
Zelle, paying for someone's flight, covering a dinner, in-kind transfer of
goods. First-class events — they are not "informal" in the ledger; they have
the same dignity as a wire.

- **`actor_owner_id`** — the **payer** (the one giving value).
- **`target_owner_id`** — the **recipient** (the one receiving value).
- **Financial effect:** Decreases the inter-personal balance owed by the
  payer to the recipient (if payer owed recipient), or increases the balance
  owed by recipient to payer (if not). The accounting effect is symmetric
  to a repayment, but the event type is distinct so the audit log can
  distinguish "money wired" from "value transferred in some other way."
- **Expected fields:** `actor_owner_id`, `target_owner_id`,
  `amount_property_currency`, `effective_date`, `description`,
  `metadata.method` (e.g., `"zelle"`, `"flight_purchase"`, `"dinner"`).
- **Example:** *"S covers V's $400 dinner in Mumbai on 2026-08-15. Recorded
  as SETTLEMENT, actor=S, target=V, amount_property_currency=₹33,200 (at
  the day's rate), metadata={method: 'dinner', city: 'Mumbai'}."*

### `OPEX_EXPENSE`

A shared running cost: property tax, HOA / society fees, shared utilities,
common-area maintenance. Recorded as a single expense event, then split
among owners via child `OPEX_SPLIT` rows.

- **`actor_owner_id`** — the owner who paid the expense (often a single
  person fronts the entire amount).
- **`target_owner_id`** — `NULL`.
- **Financial effect:** Records the gross expense. Does **not** by itself
  affect inter-personal balances — that happens via the `OPEX_SPLIT`
  children.
- **Expected fields:** `actor_owner_id`, `amount_property_currency`,
  `effective_date`, `description`, `metadata.expense_category` (e.g.,
  `"property_tax"`, `"hoa"`, `"electricity"`).
- **Example:** *"V pays the annual property tax of ₹120,000 on 2027-04-30.
  Recorded as OPEX_EXPENSE, actor=V, amount=₹120,000, metadata={category:
  'property_tax', period: 'FY2027'}. Three OPEX_SPLIT events follow."*

### `OPEX_SPLIT`

The per-owner portion of an `OPEX_EXPENSE`. One row per owner.

- **`actor_owner_id`** — the owner whose share this represents.
- **`target_owner_id`** — the owner who paid the expense (so the split owes
  the payer).
- **`reverses_event_id`** — `NULL` (this is not a compensating entry).
- **Financial effect:** Increases the inter-personal balance owed by
  `actor_owner_id` to `target_owner_id` by `amount_property_currency`.
  (If the actor *is* the payer, no balance change — it's their own share.)
- **Expected fields:** `actor_owner_id`, `target_owner_id` (the payer),
  `amount_property_currency`, `effective_date`, `metadata.parent_event_id`
  (linking back to the OPEX_EXPENSE), `metadata.share_pct`.
- **Example:** *"Continuing the property tax above: three OPEX_SPLIT events
  are written, each with amount=₹40,000, target=V (the payer), and
  metadata.parent_event_id pointing at the OPEX_EXPENSE row. The split for
  actor=V has no inter-personal effect (V doesn't owe himself)."*

> Implementation note: the canonical split data is stored in the `opex_splits`
> table (one row per (event_id, owner_id) pair) which references the parent
> `OPEX_EXPENSE` event. The `OPEX_SPLIT` event rows in the `events` table
> are the audit trail; the `opex_splits` table is the relational shape.

### `FX_SNAPSHOT`

A daily mid-market reference rate stored for a (date, currency_pair). **This
is not a financial transaction.** It is a data point used by FX gain/loss
reporting.

- **`actor_owner_id`** — the system user / agent that recorded it (commonly
  a service account).
- **`target_owner_id`** — `NULL`.
- **Financial effect:** None.
- **Expected fields:** `effective_date`, `metadata.currency_pair`,
  `metadata.reference_rate`, `metadata.source` (e.g.,
  `"exchangerate.host"`, `"RBI"`).
- **Example:** *"On 2026-05-01 the daily snapshot job records USD/INR =
  83.4521 from exchangerate.host. Recorded as FX_SNAPSHOT, effective_date=
  2026-05-01, metadata={currency_pair: 'USD_INR', reference_rate:
  83.4521, source: 'exchangerate.host'}."*

> Implementation note: the daily reference rates are also written into the
> dedicated `fx_rates` table. The event-log version exists so that the
> *complete* history of the system, including FX context, can be replayed
> from one source.

### `EQUITY_ADJUSTMENT`

A one-time equity offset recorded at property setup (t=0 only). Captures
asymmetric ownership stakes — typically a floor-premium bid where one owner
pays more for a preferred floor and is awarded slightly more equity.

**Constraint:** This event type is allowed *only once per property*, at
setup. After it is recorded, equity percentages are frozen and the
application must reject further `EQUITY_ADJUSTMENT` events for that property.
This is enforced at the application layer.

- **`actor_owner_id`** — the owner whose equity is being adjusted.
- **`target_owner_id`** — `NULL` (the adjustment is property-level).
- **Financial effect:** Updates the owner's `equity_pct` row in `owners`.
  This is the only event type that legitimately writes a non-event-table
  field; it is part of property setup, not steady-state operation.
- **Expected fields:** `actor_owner_id`, `effective_date`, `description`,
  `metadata.new_equity_pct`, `metadata.previous_equity_pct`,
  `metadata.reason`.
- **Example:** *"At property setup all three cousins want the top floor.
  V wins the bid and accepts a 35% / 32.5% / 32.5% equity split. One
  EQUITY_ADJUSTMENT event is recorded for each owner whose equity moves
  from 33.33%, with new_equity_pct={35, 32.5, 32.5} respectively."*

### `EXIT`

Records an owner's exit from the property arrangement. The event is the
audit-trail record of the exit decision; it does not initiate any legal
transfer.

- **`actor_owner_id`** — the exiting owner.
- **`target_owner_id`** — `NULL`.
- **Financial effect:** Sets `owners.exited_at` to `effective_date`. Records
  the buyout election (which formula the owners used), the agreed buyout
  amount, and the shared-floor election. Does **not** automatically transfer
  any money — settlement of the buyout is recorded as separate
  CONTRIBUTION/SETTLEMENT events.
- **Expected fields:** `actor_owner_id`, `effective_date`, `description`,
  `metadata.buyout_formula` (one of `"net_contribution"`,
  `"market_value"`, `"weighted_blend"`), `metadata.buyout_amount`,
  `metadata.shared_floor_election` (`"retain"` or `"dilute"`).
- **Example:** *"S decides to exit on 2030-09-01. Owners agree on the
  weighted blend buyout of ₹4,200,000. S elects to dilute her shared-floor
  share to V and P. Recorded as EXIT, actor=S, effective_date=2030-09-01,
  metadata={buyout_formula: 'weighted_blend', buyout_amount: 4200000,
  shared_floor_election: 'dilute'}."*

### `COMPENSATING_ENTRY`

A reversal that negates the financial effect of a prior event. **Always**
linked via `reverses_event_id` to the original. See the dedicated section
below for the full mechanics.

- **`actor_owner_id`** — typically the same as the original event's actor;
  this preserves audit narrative.
- **`target_owner_id`** — same as the original event's target.
- **`reverses_event_id`** — required: the original event's `id`.
- **Financial effect:** Negates the original. The application sums all
  events with their signs; the original (positive) and the compensating
  (negative) cancel.
- **Expected fields:** Same shape as the original, but all signed amount
  fields are negated. `description` should explain the human reason
  ("EMI was double-recorded; this reverses the duplicate").
- **Example:** See the worked example in the *Compensating entries* section
  below.

---

## HMAC signing — why and how

Every event row carries an `hmac_signature` column. The signature is computed
at write time from a canonical string of the event's most-financially-meaningful
fields, using a server-side secret. At any later moment — a nightly export, an
audit, a forensics task — the signature can be recomputed from the row and
compared. Mismatch = the row was altered after signing.

### What "tamper-evidence" means here

- A bug that accidentally `UPDATE`s a row will be detected.
- A DBA who runs a manual `UPDATE` will be detected.
- A malicious actor who edits the database directly will be detected.
- A migration script that is supposed to be a no-op but isn't will be
  detected.

It does **not** mean "we prevent tampering" (we cannot — anyone with database
access can write). It means "we make tampering detectable, after the fact,
deterministically."

### The canonical string format

```
{id}|{event_type}|{actor_owner_id}|{amount_property_currency}|{effective_date}|{recorded_at}
```

This format is **frozen**. It is the public contract for every signed event
row in the system, including events written years ago. Reordering fields,
adding fields, changing the separator, or normalizing whitespace will break
verification on every existing row.

### Why these specific fields?

These are the fields that, if altered, would change the financial meaning of
the event:

| Field | Why it must be in the signature |
|-------|---------------------------------|
| `id` | Each event is unique; including the id prevents a row from being copy-pasted with a new id and re-signed. |
| `event_type` | Changing the type changes the routing of the financial effect (e.g., from a CONTRIBUTION to a SETTLEMENT). |
| `actor_owner_id` | Changing who acted reroutes the balance impact. |
| `amount_property_currency` | The amount used for balance math. Tampering here directly changes who-owes-whom. |
| `effective_date` | The business date of the event. Determines which historical balance queries see this event. |
| `recorded_at` | The server-side timestamp. Including it prevents back-dating attacks (where someone writes a fake event and changes recorded_at to look older). |

Other fields (description, metadata, FX rates) are *not* in the signature.
This is a deliberate trade-off: descriptions and metadata are sometimes
edited for human readability post-hoc (e.g., correcting a typo in a
description). FX rates are not in the signature because the dual-rate fields
are derived from external state and are not the primary balance-determining
field — the signed `amount_property_currency` is.

### How to render `NULL` and timestamps

- A `NULL` `amount_property_currency` is rendered as the empty string in the
  canonical input. (Some events legitimately have no monetary amount —
  `FX_SNAPSHOT`, `INTERPERSONAL_RATE_CHANGE`, etc.)
- `effective_date` is rendered as ISO 8601 (`YYYY-MM-DD`).
- `recorded_at` is rendered as a timezone-aware ISO 8601 string. Naive
  timestamps must be coerced to UTC before signing.

### What to do when verification fails

A row that fails verification is **not** silently ignored. Specifically:

- The audit UI surfaces failed verifications as a top-level alert.
- Failed rows are **not** auto-corrected. Auto-correcting would defeat the
  point of the signature.
- The investigation flow is: snapshot the row, compare against the latest
  Git-committed nightly export, identify when the divergence happened,
  assess whether it is a bug or a malicious edit, decide on a remediation
  (which itself is recorded as a `COMPENSATING_ENTRY` plus a re-issued
  correct event).

### Key rotation — flagged as a future concern

If `HMAC_SECRET_KEY` ever changes, every event signed with the old key will
fail verification under the new key. The mitigation pattern is **key
versioning**:

- Store a `key_version` column alongside the signature.
- Maintain a small registry of keys: `{1: <old key>, 2: <current key>}`.
- Sign new events with the current key. Verify each event with the key
  matching its `key_version`.
- Old keys are kept indefinitely — they are needed for verification, not for
  signing.

This is **not in scope for v1**. The current schema does not have a
`key_version` column. The first time the team needs to rotate the key, a
migration will add the column (defaulting `key_version=1` for all existing
rows) and the verification path will start branching on version. Until then,
treat `HMAC_SECRET_KEY` as a **secret that must not change**.

---

## Compensating entries — the correction mechanism

### Why you cannot simply edit a bad event

The point of the append-only log is that history is immutable. If you could
edit a bad event, the entire audit trail becomes meaningless — no one can
prove that what they see now is what was originally written. So we don't.

Instead, we record a **new event** that financially negates the bad one. The
original stays. The compensating entry sits beside it. The replay engine
sees both, sums them, and arrives at the correct balance.

### The exact pattern

To correct an erroneous event:

1. Build a new `LedgerEvent` with `event_type = COMPENSATING_ENTRY`.
2. Set `reverses_event_id = <original event id>`.
3. Negate every signed monetary field:
   - `amount_property_currency` → `-original.amount_property_currency`
   - `amount_source_currency` → `-original.amount_source_currency`
   - `inr_landed` → `-original.inr_landed`
4. **Preserve the FX rate fields** (`fx_rate_actual`, `fx_rate_reference`)
   unchanged. The compensating entry happened in the same FX context as the
   original; we are not re-stamping at today's rate.
5. **Preserve `actor_owner_id` and `target_owner_id`** unchanged. The
   compensating entry undoes the same parties' relationship effect.
6. **Preserve `effective_date`** unchanged. The correction takes effect on
   the same business date as the original — historical balance queries
   between then and now will reflect the correction.
7. Add a `description` that explains the human reason ("Original event
   double-counted the May EMI; this reverses it").
8. Sign the compensating entry with the current HMAC key. The compensating
   entry has its **own** signature, just like any other event.

### What "negating" means for each field

| Field | Compensating entry value |
|-------|--------------------------|
| `id` | New, unique. |
| `event_type` | `COMPENSATING_ENTRY` |
| `actor_owner_id` | Same as original. |
| `target_owner_id` | Same as original. |
| `loan_id` | Same as original. |
| `amount_source_currency` | Negated. |
| `source_currency` | Same as original. |
| `amount_property_currency` | Negated. |
| `property_currency` | Same as original. |
| `fx_rate_actual` | Same as original (preserved). |
| `fx_rate_reference` | Same as original (preserved). |
| `fee_source_currency` | Same as original (preserved — fee is not negated; it is a sunk cost). |
| `inr_landed` | Negated. |
| `description` | New: explain the correction in plain English. |
| `metadata` | New, must include `reverses_original_event` pointing at original id. |
| `reverses_event_id` | The original event's id. |
| `recorded_by` | The actor making the correction (may differ from original recorder). |
| `recorded_at` | Now (server time). |
| `effective_date` | Same as original. |
| `hmac_signature` | Computed fresh over the compensating entry's canonical string. |

### Worked example

> P logs a `CONTRIBUTION` event on 2026-06-15: `amount_property_currency =
> ₹100,000`. Three days later P realizes the actual amount was ₹10,000 (a
> decimal slip).
>
> The fix:
>
> 1. Write a `COMPENSATING_ENTRY` referencing the bad event:
>    `amount_property_currency = -₹100,000`, `effective_date =
>    2026-06-15`, `description = "Reverses event <id>: amount entered as
>    ₹100,000 but actual was ₹10,000 (decimal slip)."`
> 2. Write a new `CONTRIBUTION` event for the correct amount:
>    `amount_property_currency = ₹10,000`, `effective_date = 2026-06-15`,
>    `description = "P's actual contribution on 2026-06-15. Re-issued
>    after compensating <bad-event-id>."`
>
> Result: the event log now has three rows — bad (+₹100,000),
> compensating (-₹100,000), corrected (+₹10,000). All historical balance
> queries replay all three and arrive at the correct +₹10,000. The
> ledger UI shows all three rows clearly, the bad and compensating ones
> visually paired with a "corrected" badge, and the correct one with a
> note linking back to the correction.

### What the UI shows

- The original error and the compensating entry are both displayed,
  visually paired.
- The compensating entry is clearly labeled and shows its
  `reverses_event_id` as a link to the original.
- The replacement event (if any) is shown as a sibling with a "re-issued
  correction of <bad-event-id>" note.
- This is by design: hiding the original would defeat the audit purpose.

### What you do **not** do

- You do not delete the original.
- You do not edit the original (not the amount, not the description, not
  the metadata).
- You do not negate the FX rates or the fee — those are not financial
  effects, they are context.
- You do not change `effective_date` (the correction is on the original
  business date).

---

## `effective_date` vs `recorded_at`

These two timestamps are easily confused but mean entirely different things.
Every balance query answer depends on getting them right.

| Field | Meaning | Set by | Mutable? |
|-------|---------|--------|----------|
| `recorded_at` | The wall-clock moment the row was inserted into the database. | The server (`now()`). | Immutable. |
| `effective_date` | The **business date** the transaction actually occurred. | The user / agent recording the event. | Immutable once written, but can be in the past. |

### Why they differ

A wire transfer initiated on March 3rd may not be logged into the ledger
until March 7th — the sender forgot to enter it, or was traveling. The
correct way to record this is:

- `effective_date = 2026-03-03`
- `recorded_at = 2026-03-07T...UTC` (set automatically)

### Which one balance math uses

**All balance queries use `effective_date`.** A query for "balance as of
2026-03-04" replays all events with `effective_date <= 2026-03-04` — and
correctly includes the wire that landed on March 3rd, even though it was not
entered until March 7th.

`recorded_at` is for the audit trail only. It answers "when did this row
appear in the database?" — useful for forensics, useless for balance math.

### Worked example

> Owner V wires $5,000 to the bank on 2026-03-03 to fund the May EMI.
> V forgets to log the wire in the app. On 2026-03-07, V remembers and
> records a `CONTRIBUTION` event with `effective_date = 2026-03-03`. The
> server stamps `recorded_at = 2026-03-07T14:32:00Z`.
>
> Now consider these queries, all run today (2026-04-27):
>
> | Query | Result |
> |-------|--------|
> | "What was V's contribution total on 2026-03-04?" | Includes the wire (effective_date=2026-03-03 <= 2026-03-04). |
> | "What was V's contribution total on 2026-03-02?" | Excludes the wire. |
> | "Show me all events recorded after 2026-03-05." | Includes the wire (recorded_at=2026-03-07). |
> | "Show me all events with effective_date in March." | Includes the wire. |
>
> The two timestamps are independent — both are queryable, but they answer
> different questions.

### A note on back-dated events

Because `effective_date` can be set in the past, a malicious or careless
user could enter a fake event "from a year ago." Mitigations:

- The signature includes `recorded_at`, so back-dating an event after the
  fact is detectable (the row was clearly inserted later).
- Each event has `recorded_by` — the email or agent id that wrote it.
- The audit UI surfaces "events with `effective_date` more than 30 days
  before `recorded_at`" as items worth a human glance.

This is not a hard prevention — it is a defense-in-depth detection layer.
The cultural norm in the group is to record events promptly; the system
makes back-dating visible.
