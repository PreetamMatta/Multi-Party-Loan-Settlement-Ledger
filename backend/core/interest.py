"""
interest.py — Inter-personal loan interest accrual.

Spec: docs/business-logic/interpersonal-loans.md

Day-count: actual/365 simple interest. The divisor is always 365 — leap
years included. See `docs/business-logic/interpersonal-loans.md#leap-years-still-use-365`.

First-day rule: interest accrues from the day AFTER disbursement. Implemented
naturally by `(t2 - t1).days` arithmetic. See
`docs/business-logic/interpersonal-loans.md#first-day-rule-no-interest-on-day-of-disbursement`.

Repayment ordering: repayments apply against accrued interest first, then
principal. See `docs/business-logic/interpersonal-loans.md#when-accrued-interest-is-paid`.

COMPENSATING_ENTRY waterfall: when a compensating entry reverses a repayment
or settlement, the restoration applies the inverse of the original waterfall —
accrued interest is restored first (up to the amount that existed before the
original repayment), then principal. This preserves the correct running
principal for future accrual. If the original repayment event is not in the
query window, the restoration falls back to pure principal (same as a
disbursement-equivalent).

This module never modifies the events table. It only reads.
"""

from __future__ import annotations

import calendar as _calendar
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from core._db import _EVENT_COLUMNS, _events_to_pair_balance, _row_to_event
from core.events import EventType, LedgerEvent, get_financial_effect

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
_DAYS_PER_YEAR = Decimal("365")  # actual/365 — leap years still use 365

# Event types that trigger the interest-first waterfall on reversal.
_REPAYMENT_TYPES = frozenset(("INTERPERSONAL_LOAN_REPAYMENT", "SETTLEMENT"))


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------
def _signed_pair_delta(
    event: LedgerEvent,
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
) -> Decimal:
    """
    Return the principal delta this event applies in the (lender, borrower)
    direction. Positive = principal grows; negative = principal shrinks
    (before the interest-first waterfall is applied).
    """
    effect = get_financial_effect(event)
    ip = effect.get("interpersonal")
    if ip is None:
        return Decimal("0")
    if ip["lender"] == lender_id and ip["borrower"] == borrower_id:
        return Decimal(ip["delta"])
    if ip["lender"] == borrower_id and ip["borrower"] == lender_id:
        return -Decimal(ip["delta"])
    return Decimal("0")


def _accrue(
    principal: Decimal,
    rate_pct: Decimal,
    days: int,
) -> Decimal:
    """principal × (rate_pct / 100) × days / 365 — in pure Decimal."""
    if days <= 0 or rate_pct == 0 or principal <= 0:
        return Decimal("0")
    return principal * rate_pct / Decimal("100") * Decimal(days) / _DAYS_PER_YEAR


def _new_rate_from_event(event: LedgerEvent) -> Decimal | None:
    """Pull `metadata.new_rate_pct` off a RATE_CHANGE event, coerced to Decimal."""
    raw = event.metadata.get("new_rate_pct")
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    return Decimal(str(raw))


# -----------------------------------------------------------------------------
# Pure accrual engine (no DB access)
# -----------------------------------------------------------------------------
def _accrue_interest_from_events(
    events: list[LedgerEvent],
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> Decimal:
    """
    Compute interest accrued within [period_start, period_end] from a
    pre-fetched, chronologically sorted event list. No DB access.

    Algorithm:
      - Walk events maintaining (running_principal, current_rate,
        running_accrued_outstanding).
      - On each interval boundary, compute interest for elapsed days; if the
        interval overlaps [period_start, period_end], add the overlap share.
      - DISBURSEMENT-direction delta (+): principal grows.
      - REPAYMENT-direction delta (−): interest-first waterfall. State before
        each waterfall is recorded keyed by event.id for use below.
      - COMPENSATING_ENTRY reversing a repayment/settlement (+): inverse
        waterfall — restore accrued interest first (up to what was there
        before the original repayment), then principal. If the original event
        is not in the recorded-state map, falls back to treating the full
        amount as principal (same as the current v1 caveat).
      - INTERPERSONAL_RATE_CHANGE: switch rate forward-only.
    """
    # Keyed by event.id: running_accrued just BEFORE the repayment waterfall.
    # Used to correctly split a compensating-entry restoration.
    pre_repayment_accrued: dict[Any, Decimal] = {}

    running_principal = Decimal("0")
    running_accrued = Decimal("0")
    current_rate = Decimal("0")
    last_date: date | None = None
    interest_in_period = Decimal("0")

    for event in events:
        # Step 1: accrue from last_date to this event's effective_date.
        if last_date is not None and event.effective_date > last_date:
            interval_days = (event.effective_date - last_date).days
            full_interest = _accrue(running_principal, current_rate, interval_days)
            running_accrued += full_interest
            overlap_start = max(last_date, period_start)
            overlap_end = min(event.effective_date, period_end)
            if overlap_end > overlap_start:
                window_days = (overlap_end - overlap_start).days
                interest_in_period += _accrue(running_principal, current_rate, window_days)

        # Step 2: apply the event's effect.
        if event.event_type is EventType.INTERPERSONAL_RATE_CHANGE:
            new_rate = _new_rate_from_event(event)
            if new_rate is not None:
                current_rate = new_rate
        else:
            delta = _signed_pair_delta(event, lender_id, borrower_id)
            if delta > 0:
                # Determine whether this is a compensating entry reversing a
                # repayment — if so, apply the inverse waterfall.
                if (
                    event.event_type is EventType.COMPENSATING_ENTRY
                    and event.metadata.get("original_event_type") in _REPAYMENT_TYPES
                    and event.reverses_event_id is not None
                    and event.reverses_event_id in pre_repayment_accrued
                ):
                    # Inverse waterfall: restore accrued interest first (up to what
                    # was accrued before the original repayment), then principal.
                    orig_accrued = pre_repayment_accrued[event.reverses_event_id]
                    accrued_restored = min(delta, orig_accrued)
                    running_accrued += accrued_restored
                    running_principal += delta - accrued_restored
                else:
                    # Normal disbursement-equivalent: pure principal growth.
                    running_principal += delta
            elif delta < 0:
                # Repayment-equivalent: interest-first waterfall.
                # Record state BEFORE the waterfall for inverse-waterfall lookup.
                pre_repayment_accrued[event.id] = running_accrued
                payment = -delta
                if payment <= running_accrued:
                    running_accrued -= payment
                else:
                    payment -= running_accrued
                    running_accrued = Decimal("0")
                    running_principal -= payment

        last_date = event.effective_date

    # Step 3: tail accrual to period_end.
    if last_date is not None and last_date < period_end:
        overlap_start = max(last_date, period_start)
        if period_end > overlap_start:
            tail_days = (period_end - overlap_start).days
            interest_in_period += _accrue(running_principal, current_rate, tail_days)

    return interest_in_period


# -----------------------------------------------------------------------------
# Spec: docs/business-logic/interpersonal-loans.md#accrual-math---chosen-approach
# -----------------------------------------------------------------------------
async def calculate_accrued_interest(
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
    period_start: date,
    period_end: date,
    db: Any,
) -> Decimal:
    """
    Total simple interest accrued on the (lender, borrower) loan within
    `[period_start, period_end]` (both inclusive at the day level).

    Spec: docs/business-logic/interpersonal-loans.md

    Fetches all pair events up to `period_end` and delegates to the pure
    `_accrue_interest_from_events` engine.
    """
    rows = await db.fetch(
        f"""
        SELECT {_EVENT_COLUMNS}
          FROM events
         WHERE effective_date <= $3
           AND target_owner_id IS NOT NULL
           AND (
                (actor_owner_id = $1 AND target_owner_id = $2)
             OR (actor_owner_id = $2 AND target_owner_id = $1)
           )
         ORDER BY effective_date ASC, recorded_at ASC
        """,
        lender_id,
        borrower_id,
        period_end,
    )
    events = [_row_to_event(row) for row in rows]
    return _accrue_interest_from_events(events, lender_id, borrower_id, period_start, period_end)


# -----------------------------------------------------------------------------
# Spec: docs/business-logic/interpersonal-loans.md#per-financial-year-statements
# -----------------------------------------------------------------------------
def _fy_bounds(financial_year: int, calendar: str) -> tuple[date, date]:
    """
    Resolve the (start, end) bounds of the financial year for the chosen
    calendar. End is the last day of the FY (inclusive).
    """
    if calendar == "IN":
        return date(financial_year, 4, 1), date(financial_year + 1, 3, 31)
    if calendar == "US":
        return date(financial_year, 1, 1), date(financial_year, 12, 31)
    raise ValueError(f"Unsupported calendar: {calendar!r}. Expected 'IN' or 'US'.")


def _add_one_month(d: date) -> date:
    """First day of the month after `d`. Used to enumerate FY months."""
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _last_day_of_month(d: date) -> date:
    last = _calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, last)


async def generate_fy_statement(
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
    financial_year: int,
    db: Any,
    calendar: str = "IN",
) -> dict[str, Any]:
    """
    Per-(lender, borrower)-pair, per-financial-year statement.

    Spec: docs/business-logic/interpersonal-loans.md#per-financial-year-statements

    `financial_year` is the *starting* year. For `calendar="IN"`,
    FY 2026 covers 2026-04-01 → 2027-03-31. For `calendar="US"`,
    FY 2026 covers the calendar year 2026.

    Opening balance is the principal owed by `borrower_id` to `lender_id` as
    of `fy_start - 1 day` (no interest layered in — opening reflects pure
    principal). Monthly breakdown rows include zero-activity months so the
    rendered statement always has 12 rows.

    Implementation note: a single DB fetch retrieves ALL pair events up to
    `fy_end`. Opening/closing balances, per-month accrual, and per-month
    closing balances are then computed in-memory — no per-month DB round
    trips. This keeps the function at O(1) DB calls regardless of the number
    of months.
    """
    fy_start, fy_end = _fy_bounds(financial_year, calendar)
    day_before_start = fy_start - timedelta(days=1)

    # Single fetch: all pair events from inception up to fy_end.
    all_rows = await db.fetch(
        f"""
        SELECT {_EVENT_COLUMNS}
          FROM events
         WHERE effective_date <= $3
           AND target_owner_id IS NOT NULL
           AND (
                (actor_owner_id = $1 AND target_owner_id = $2)
             OR (actor_owner_id = $2 AND target_owner_id = $1)
           )
         ORDER BY effective_date ASC, recorded_at ASC
        """,
        lender_id,
        borrower_id,
        fy_end,
    )
    all_events = [_row_to_event(row) for row in all_rows]

    # Partition: events inside the FY window for audit list and totals.
    fy_events = [e for e in all_events if fy_start <= e.effective_date <= fy_end]

    # Opening and closing balances (principal only, computed in-memory).
    opening_balance = _events_to_pair_balance(
        [e for e in all_events if e.effective_date <= day_before_start],
        lender_id,
        borrower_id,
    )
    closing_balance = _events_to_pair_balance(all_events, lender_id, borrower_id)

    total_disbursed = Decimal("0")
    total_repaid = Decimal("0")
    audit_events: list[dict[str, Any]] = []
    for event in fy_events:
        delta = _signed_pair_delta(event, lender_id, borrower_id)
        if delta > 0:
            total_disbursed += delta
        elif delta < 0:
            total_repaid += -delta
        audit_events.append(
            {
                "effective_date": event.effective_date,
                "event_type": event.event_type.value,
                "amount_inr": event.amount_property_currency or Decimal("0"),
                "description": event.description,
            }
        )

    total_interest = _accrue_interest_from_events(
        all_events, lender_id, borrower_id, fy_start, fy_end
    )

    # Monthly breakdown: O(N×M) CPU, zero extra DB calls.
    monthly: list[dict[str, Any]] = []
    cursor = fy_start
    prev_close = opening_balance
    while cursor <= fy_end:
        month_end = min(_last_day_of_month(cursor), fy_end)

        # Events within this month (subset of fy_events already in memory).
        month_events = [e for e in fy_events if cursor <= e.effective_date <= month_end]
        month_disbursed = Decimal("0")
        month_repaid = Decimal("0")
        for event in month_events:
            d = _signed_pair_delta(event, lender_id, borrower_id)
            if d > 0:
                month_disbursed += d
            elif d < 0:
                month_repaid += -d

        # Interest and closing balance from pre-fetched events, no extra I/O.
        events_to_month_end = [e for e in all_events if e.effective_date <= month_end]
        month_interest = _accrue_interest_from_events(
            events_to_month_end, lender_id, borrower_id, cursor, month_end
        )
        month_close = _events_to_pair_balance(events_to_month_end, lender_id, borrower_id)

        monthly.append(
            {
                "month": f"{cursor.year:04d}-{cursor.month:02d}",
                "opening_balance": prev_close,
                "disbursements": month_disbursed,
                "repayments": month_repaid,
                "interest_accrued": month_interest,
                "closing_balance": month_close,
            }
        )
        prev_close = month_close
        cursor = _add_one_month(cursor)

    return {
        "lender_id": lender_id,
        "borrower_id": borrower_id,
        "financial_year": financial_year,
        "calendar": calendar,
        "fy_start": fy_start,
        "fy_end": fy_end,
        "opening_balance_inr": opening_balance,
        "closing_balance_inr": closing_balance,
        "total_interest_accrued_inr": total_interest,
        "total_disbursed_inr": total_disbursed,
        "total_repaid_inr": total_repaid,
        "monthly_breakdown": monthly,
        "events": audit_events,
    }
