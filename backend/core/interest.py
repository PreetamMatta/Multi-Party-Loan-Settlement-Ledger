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

This module never modifies the events table. It only reads.
"""

from __future__ import annotations

import calendar as _calendar
import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from core.events import EventType, LedgerEvent, get_financial_effect

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
_DAYS_PER_YEAR = Decimal("365")  # actual/365 — leap years still use 365


_EVENT_COLUMNS = (
    "id, property_id, event_type, actor_owner_id, target_owner_id, loan_id, "
    "amount_source_currency, source_currency, amount_property_currency, "
    "property_currency, fx_rate_actual, fx_rate_reference, fee_source_currency, "
    "inr_landed, description, metadata, reverses_event_id, hmac_signature, "
    "recorded_by, recorded_at, effective_date"
)


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------
def _row_to_event(row: Any) -> LedgerEvent:
    """Mirror of balance._row_to_event — kept local to avoid a cross-module import."""
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata) if metadata else {}
    elif metadata is None:
        metadata = {}

    recorded_at = row["recorded_at"]
    if isinstance(recorded_at, str):
        recorded_at = datetime.fromisoformat(recorded_at)

    return LedgerEvent(
        id=row["id"],
        property_id=row["property_id"],
        event_type=EventType(row["event_type"]),
        actor_owner_id=row["actor_owner_id"],
        target_owner_id=row["target_owner_id"],
        loan_id=row["loan_id"],
        amount_source_currency=row["amount_source_currency"],
        source_currency=row["source_currency"],
        amount_property_currency=row["amount_property_currency"],
        property_currency=row["property_currency"],
        fx_rate_actual=row["fx_rate_actual"],
        fx_rate_reference=row["fx_rate_reference"],
        fee_source_currency=row["fee_source_currency"],
        inr_landed=row["inr_landed"],
        description=row["description"],
        metadata=metadata,
        reverses_event_id=row["reverses_event_id"],
        hmac_signature=row["hmac_signature"],
        recorded_by=row["recorded_by"],
        recorded_at=recorded_at,
        effective_date=row["effective_date"],
    )


def _signed_pair_delta(
    event: LedgerEvent,
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
) -> Decimal:
    """
    Return the principal delta this event applies in the (lender, borrower)
    direction. Positive = principal grows (a fresh disbursement-equivalent);
    negative = principal shrinks (a repayment-equivalent before the
    interest-first waterfall is applied).
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

    Algorithm:
      1. Pull every event touching the pair, in either direction, with
         `effective_date <= period_end`. Order by (effective_date,
         recorded_at) ASC — the same ordering balance computation uses.
      2. Walk events maintaining (running_principal, current_rate,
         running_accrued_outstanding). On each interval boundary, compute
         interest at the current rate on the current principal for the
         elapsed days; if the interval overlaps `[period_start, period_end]`,
         add the overlap-share to the returned total.
      3. Apply each event in turn:
           - DISBURSEMENT-direction delta (+): principal grows
           - REPAYMENT-direction delta (−): apply interest-first waterfall:
               first reduce running_accrued_outstanding, remainder reduces
               principal
           - INTERPERSONAL_RATE_CHANGE: switch the rate going forward
           - COMPENSATING_ENTRY: routed through the parent's framing by
             `get_financial_effect`; treated as a principal delta (we do
             NOT reverse historical accrual — see module docstring caveat)
      4. After the last event, accrue forward to `period_end`.

    Zero-rate periods contribute `Decimal('0')`.
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
            # Window overlap: only count the days that fall in [period_start, period_end].
            overlap_start = max(last_date, period_start)
            overlap_end = min(event.effective_date, period_end)
            if overlap_end > overlap_start:
                window_days = (overlap_end - overlap_start).days
                # The boundary-day exclusion of last_date is built into .days arithmetic.
                interest_in_period += _accrue(running_principal, current_rate, window_days)

        # Step 2: apply the event's effect.
        if event.event_type is EventType.INTERPERSONAL_RATE_CHANGE:
            new_rate = _new_rate_from_event(event)
            if new_rate is not None:
                current_rate = new_rate
        else:
            delta = _signed_pair_delta(event, lender_id, borrower_id)
            if delta > 0:
                # Disbursement-equivalent: pure principal growth.
                running_principal += delta
            elif delta < 0:
                # Repayment-equivalent: interest-first waterfall.
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
    """
    # Local import to avoid a cyclic top-level import (balance imports interest).
    from core.balance import get_interpersonal_balance

    fy_start, fy_end = _fy_bounds(financial_year, calendar)
    day_before_start = fy_start - timedelta(days=1)

    opening_balance = await get_interpersonal_balance(
        lender_id, borrower_id, day_before_start, db
    )
    closing_balance = await get_interpersonal_balance(
        lender_id, borrower_id, fy_end, db
    )

    # Pull every event in the FY for the audit list and per-month grouping.
    rows = await db.fetch(
        f"""
        SELECT {_EVENT_COLUMNS}
          FROM events
         WHERE effective_date BETWEEN $3 AND $4
           AND target_owner_id IS NOT NULL
           AND (
                (actor_owner_id = $1 AND target_owner_id = $2)
             OR (actor_owner_id = $2 AND target_owner_id = $1)
           )
         ORDER BY effective_date ASC, recorded_at ASC
        """,
        lender_id,
        borrower_id,
        fy_start,
        fy_end,
    )
    events = [_row_to_event(row) for row in rows]

    total_disbursed = Decimal("0")
    total_repaid = Decimal("0")
    audit_events: list[dict[str, Any]] = []
    for event in events:
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

    total_interest = await calculate_accrued_interest(
        lender_id, borrower_id, fy_start, fy_end, db
    )

    # Monthly breakdown: enumerate every month in the FY, even zero-activity ones.
    monthly: list[dict[str, Any]] = []
    cursor = fy_start
    prev_close = opening_balance
    while cursor <= fy_end:
        month_end = min(_last_day_of_month(cursor), fy_end)
        # Per-month disbursements / repayments by walking events in the window.
        month_disbursed = Decimal("0")
        month_repaid = Decimal("0")
        for event in events:
            if cursor <= event.effective_date <= month_end:
                d = _signed_pair_delta(event, lender_id, borrower_id)
                if d > 0:
                    month_disbursed += d
                elif d < 0:
                    month_repaid += -d
        month_interest = await calculate_accrued_interest(
            lender_id, borrower_id, cursor, month_end, db
        )
        month_close = await get_interpersonal_balance(
            lender_id, borrower_id, month_end, db
        )
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
