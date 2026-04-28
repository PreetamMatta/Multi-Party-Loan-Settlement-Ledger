# FX & Wire Transfers

This system tracks money flowing between currencies — typically USD owners
sending money to fund an INR-denominated property. Every cross-currency
movement is **dual-rate stamped**: we capture both the rate the bank
actually applied and the mid-market reference rate on the same date. This
document explains why two rates are needed and the math each one drives.

---

## The dual-rate system

Every USD↔INR (or other cross-currency) event in the ledger records two
exchange rates:

| Field | Meaning | Used for |
|-------|---------|----------|
| `fx_rate_actual` | The rate the owner's bank actually applied to the wire — **including the bank's spread**. | Balance math. This is what converts USD-sent into the INR amount that hits the recipient's account. |
| `fx_rate_reference` | The **mid-market reference rate** on the same date (RBI / `exchangerate.host`). A neutral, spread-free benchmark. | FX gain/loss reporting. Compared against `fx_rate_actual` to surface the cost of the bank's spread plus fees. |

### Why two rates?

The actual rate includes the bank's spread — the silent fee built into the
exchange rate that the bank quotes the customer. A wire of $1,000 at an
"actual rate" of 83.20 INR/USD might correspond to a mid-market rate of
83.45 — the 0.25 difference is the bank's profit on the trade.

That difference is a real cost, borne entirely by the sender. We need to
record it so:

1. The **balance math** uses real money that actually moved (actual rate).
2. The **FX gain/loss report** can surface the spread + fee cost — the
   sender's real economic impact — without socializing it.

Hiding the spread by using only one rate would be dishonest accounting:
either we'd inflate the credit (use mid-market and pretend the sender was
fully reimbursed), or we'd lose the audit trail of what the bank charged.

---

## The math

For every cross-currency event:

```
inr_landed                = (amount_source - fee_source) * fx_rate_actual
inr_reference_equivalent  = amount_source * fx_rate_reference
fx_gain_loss_inr          = inr_landed - inr_reference_equivalent
```

A few things to note:

### `inr_landed` uses `(amount_source - fee_source)`, not `amount_source`

The wire fee is taken out before the conversion happens. From the bank's
perspective, only the post-fee amount is converted and sent. So the
recipient's account sees `(amount_source - fee_source) * fx_rate_actual`,
which is exactly what we credit.

### `inr_reference_equivalent` uses `amount_source`, **not** `(amount_source - fee_source)`

This is deliberate. `inr_reference_equivalent` represents "what the gross
amount the sender intended to send *would have been worth* at the
mid-market rate, before any fees or spread." The delta between
`inr_landed` and `inr_reference_equivalent` therefore captures **both** the
bank's spread **and** the wire fee in a single number — which is what the
FX gain/loss report wants to surface.

If we subtracted the fee from both sides, the FX gain/loss number would
hide the fee and only show the spread. That would be technically a "pure
FX gain/loss" number but it would not reflect the sender's actual
economic impact.

### `fx_gain_loss_inr` is signed

- **Positive:** the sender's bank gave a *better* deal than mid-market
  (rare — usually only happens when the spread + fee are unusually
  favorable). The sender effectively gained vs. the benchmark.
- **Negative:** the sender's bank gave a *worse* deal than mid-market
  (typical). The sender's spread + fees are the FX loss.
- **Zero:** rate matched mid-market and the fee was zero (essentially never
  happens with retail wires).

---

## Why credit equals `inr_landed`, not `amount_source × rate`

This is a subtle but critical accounting choice. Consider a wire:

> V sends $5,000 USD with a $25 wire fee. The bank applies an actual rate
> of 83.20 INR/USD. So **$4,975 × 83.20 = ₹413,920** lands in the
> recipient's account.

The naive accounting move would be: "V sent $5,000, so credit V with
$5,000 × 83.20 = ₹416,000."

We do **not** do this. We credit V with `₹413,920` — the amount that
actually arrived. The $25 wire fee (₹2,080 at the actual rate) was V's
cost of moving money. It is not money that funded the property and it is
not money that anyone owes back to V.

If we credited the gross amount, V would be over-credited by the wire fee
— the math would show V having contributed more than was actually received.
That over-credit would propagate forever: it would show up in V's CapEx
total, in inter-personal balance computations, in the buyout calculation
when V exits.

**Wire fees are the sender's cost of moving money. Always. No exceptions.**

---

## The FX gain/loss report

The app surfaces a per-owner, per-period FX gain/loss number computed by
summing `fx_gain_loss_inr` across all that owner's cross-currency events
in the period.

### What it tells the owner

> "In FY2026, V sent $42,000 across eight wires. The bank's spread and
> fees cost V ₹54,300 in FX losses vs the mid-market reference rate."

### What it does **not** tell the owner

- It is not tax advice. The user should consult a CPA / CA.
- It does not include foreign-account holding gains/losses (e.g., if V
  bought USD at one rate and held it for a year before wiring).
- It does not adjust for inflation or for the fact that FX rates are
  themselves a moving target.

### A note on US tax treatment

In the US, "personal transactions in foreign currency" can have reportable
FX gain/loss when those transactions exceed a threshold per occurrence
(IRS §988 has the technical detail). The numbers this app surfaces are
useful inputs for that filing, but the app **does not file taxes** and
makes no warranty about completeness. Owners should hand the CSV export to
their CPA.

### A note on Indian tax treatment

In India, FX losses on remittances for property purchase are generally not
deductible (they are treated as part of cost of acquisition). The numbers
are useful for record-keeping but rarely useful as a deduction. Confirm
with a CA.

---

## Wire fees in detail

| Fact | Implication |
|------|-------------|
| Wire fees are the **sender's cost**. | Never socialized. The recipient is not on the hook for them. |
| Stored in `fee_source_currency`. | The fee is recorded in the source currency (USD), not in INR. |
| Excluded from `inr_landed`. | The recipient's credit is computed *after* the fee is removed. |
| Included in the FX gain/loss delta (implicitly). | Because `inr_reference_equivalent` uses the gross amount, the fee shows up as part of the negative delta. |

### Why fees aren't socialized

If V pays $25 to wire money on the group's behalf, one might argue "V is
doing the group a favor — surely the group should reimburse V's $25 fee."
That argument is appealing but practically toxic:

- It creates micro-balances that fluctuate with every wire (each EMI
  transfer would generate three tiny inter-personal credits).
- It creates a perverse incentive: an owner with an expensive wire service
  would routinely over-bill the group vs. an owner with a cheaper service.
- It adds noise to the per-FY interest statement — small fees create
  reciprocal debts that need to be tracked.

The convention is simpler and fairer: each owner picks how they move their
money. They bear the cost of that choice. The app's job is to faithfully
record what arrived.

---

## The reference rate fetch process

### How daily reference rates are populated

A daily automated job hits `exchangerate.host` for `USD_INR` (and any
other configured pairs) and writes a row to the `fx_rates` table. This is
the source of truth for `fx_rate_reference` on any given date. The job
runs once per day in the property's home timezone.

### How actual wire rates are populated

The actual rate is **manually entered** by the owner at the time of
logging the wire event. The owner reads it off their bank's transfer
confirmation and types it into the app. There is no automated way to
discover the actual rate — it is an artifact of the specific transaction
the bank executed.

### When the reference rate is missing

If a cross-currency event is logged for a date for which no `fx_rates` row
exists (e.g., the daily snapshot job failed, or the date is in the future
of the last snapshot, or the date is a holiday and the FX provider didn't
publish a rate):

1. The application falls back to the **most recent available reference
   rate** for that currency pair (the latest `rate_date` ≤ requested
   date).
2. A `WARNING` log entry is emitted with structured fields:
   ```json
   {
     "event": "fx_rate_fallback",
     "requested_date": "...",
     "fallback_date":  "...",
     "pair":           "USD_INR"
   }
   ```
3. The event is written using the fallback rate as `fx_rate_reference`.
   The fallback is acceptable because:
   - The reference rate moves slowly day-to-day.
   - The reference rate drives FX gain/loss reporting only — not balance math.
   - Auditors can see exactly which date's rate was substituted.

If **no** prior reference rate exists at all (typically only at first-ever
setup), the application raises `FXRateNotFoundError` and refuses to write
the event. The user must either provide a manual reference rate or wait
for the daily snapshot to populate.

The app **does not silently fail.** Either it succeeds with a logged
warning, or it raises a clear error. Quiet incorrectness in financial
software is unacceptable.

---

## Worked example, end-to-end

> **Setup:** P (a USD-based owner) is contributing $3,000 toward Loan #1's
> June EMI payment. P's bank charges $20 to wire INR. The bank applies an
> actual rate of 82.95 INR/USD on 2026-06-10. The mid-market reference
> rate for 2026-06-10 (from `exchangerate.host`) is 83.20.
>
> **Computation:**
> ```
> amount_source             = $3,000
> fee_source                = $20
> fx_rate_actual            = 82.95
> fx_rate_reference         = 83.20
>
> inr_landed                = ($3,000 - $20) * 82.95 = $2,980 * 82.95 = ₹247,191
> inr_reference_equivalent  = $3,000 * 83.20 = ₹249,600
> fx_gain_loss_inr          = ₹247,191 - ₹249,600 = -₹2,409
> ```
>
> **What gets recorded:** A single `CONTRIBUTION` (or `EMI_PAYMENT`) event
> with `amount_source_currency = $3,000`, `fee_source_currency = $20`,
> `fx_rate_actual = 82.95`, `fx_rate_reference = 83.20`, and
> `inr_landed = ₹247,191`.
>
> **What balance math sees:** P contributed ₹247,191 (the credit). Loan
> #1's balance is reduced by ₹247,191 (or by the principal portion
> thereof if this is an EMI). The reference rate is *not* used for
> balance math.
>
> **What the FX gain/loss report sees:** P's FX cost on this wire was
> ₹2,409 — the sum of bank spread + the $20 fee, expressed in INR at the
> reference rate.

---

## Decimal precision — never floats

All FX math is done in `Decimal`, never `float`. This is not optional. The
schema uses `NUMERIC(15,2)` for amounts and `NUMERIC(12,6)` for rates;
the Python core uses `decimal.Decimal` throughout.

The motivation: floating-point arithmetic introduces silent drift
(`0.1 + 0.2 != 0.3` in IEEE 754). On a single $5,000 wire that drift is
imperceptible; over 20 years and thousands of events, it accumulates and
poisons reconciliation. `Decimal` arithmetic is exact and reproducible.

When converting external API responses to internal types, always go
through `str()` first:

```python
rate = Decimal(str(api_response["rate"]))   # OK
rate = Decimal(api_response["rate"])         # WRONG if response is a float
```

The first form parses the API's textual representation directly. The
second form converts an already-imprecise float to a `Decimal` carrying
that imprecision forward.
