"""
Unit tests for backend/core/interest.py.

These tests pin the actual/365 simple-interest contract documented in
`docs/business-logic/interpersonal-loans.md`. The leap-year test is a
deliberate guard against silent drift to actual/actual; the rate-change
tests guard against retroactive application.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from _fakes import FakeConnection, event_to_row

from core.events import EventType
from core.interest import calculate_accrued_interest, generate_fy_statement

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _disbursement(make_event, *, lender, borrower, amount, on: date):
    return make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal(amount),
        effective_date=on,
        recorded_at=datetime(on.year, on.month, on.day, 12, 0, tzinfo=UTC),
        metadata={},
    )


def _repayment(make_event, *, lender, borrower, amount, on: date):
    return make_event(
        event_type=EventType.INTERPERSONAL_LOAN_REPAYMENT,
        actor_owner_id=borrower,
        target_owner_id=lender,
        amount_property_currency=Decimal(amount),
        effective_date=on,
        recorded_at=datetime(on.year, on.month, on.day, 12, 0, tzinfo=UTC),
        metadata={},
    )


def _rate_change(make_event, *, lender, borrower, new_rate_pct, on: date):
    return make_event(
        event_type=EventType.INTERPERSONAL_RATE_CHANGE,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=None,
        effective_date=on,
        recorded_at=datetime(on.year, on.month, on.day, 12, 0, tzinfo=UTC),
        metadata={"new_rate_pct": Decimal(str(new_rate_pct))},
    )


# -----------------------------------------------------------------------------
# calculate_accrued_interest
# -----------------------------------------------------------------------------

async def test_zero_rate_returns_zero_interest(make_event):
    """Default 0% rate means no interest, regardless of days elapsed."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb)])
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 12, 31), db
    )
    assert result == Decimal("0")


async def test_simple_interest_single_period(make_event):
    """₹100,000 at 6% for 91 days = 100,000 * 6/100 * 91/365 (exact Decimal)."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    rate = _rate_change(make_event, lender=lender, borrower=borrower,
                        new_rate_pct="6.0", on=date(2026, 1, 1))
    db = FakeConnection()
    # The rate change is recorded same day with later recorded_at. Order: disb, rate.
    rate_row = event_to_row(rate)
    rate_row["recorded_at"] = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb), rate_row])
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 4, 2), db
    )
    # Jan 1 → Apr 2 = 91 days at 6% on ₹100,000.
    expected = (
        Decimal("100000") * Decimal("6") / Decimal("100") * Decimal("91") / Decimal("365")
    )
    assert result == expected


async def test_interest_does_not_accrue_on_day_of_disbursement(make_event):
    """Disbursement on day D, queried as of day D → zero interest."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 4, 1))
    rate = _rate_change(make_event, lender=lender, borrower=borrower,
                        new_rate_pct="6.0", on=date(2026, 1, 1))
    db = FakeConnection()
    rate_row = event_to_row(rate)
    rate_row["recorded_at"] = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    db.on_fetch("target_owner_id IS NOT NULL", [rate_row, event_to_row(disb)])
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 4, 1), date(2026, 4, 1), db
    )
    assert result == Decimal("0")


async def test_interest_accrues_from_day_after_disbursement(make_event):
    """One day post-disbursement → exactly one day of accrual, not two."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    rate = _rate_change(make_event, lender=lender, borrower=borrower,
                        new_rate_pct="6.0", on=date(2026, 1, 1))
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 4, 1))
    rate_row = event_to_row(rate)
    rate_row["recorded_at"] = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [rate_row, event_to_row(disb)])
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 4, 1), date(2026, 4, 2), db
    )
    expected = Decimal("100000") * Decimal("6") / Decimal("100") * Decimal("1") / Decimal("365")
    assert result == expected


async def test_rate_change_applies_forward_only(make_event):
    """Pre-rate-change days at the old (zero) rate must contribute no interest."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    rate = _rate_change(make_event, lender=lender, borrower=borrower,
                        new_rate_pct="6.0", on=date(2026, 4, 1))
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb), event_to_row(rate)])
    # From Jan 1 to Mar 31: 0% (no interest). From Apr 1 to Jul 1: 6% over 91 days.
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 7, 1), db
    )
    expected = (
        Decimal("100000") * Decimal("6") / Decimal("100") * Decimal("91") / Decimal("365")
    )
    assert result == expected


async def test_rate_change_does_not_retroactively_change_accrued_interest(make_event):
    """
    Compute accrual up to the rate change vs after. Querying just the pre-period
    yields zero (old 0% rate) regardless of whatever rate exists later.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    rate = _rate_change(make_event, lender=lender, borrower=borrower,
                        new_rate_pct="9.0", on=date(2026, 4, 1))
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb), event_to_row(rate)])
    # Querying only the pre-rate-change window: result is zero, even though
    # a 9% rate later exists in the stream.
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 3, 31), db
    )
    assert result == Decimal("0")


async def test_repayment_reduces_principal_for_future_accrual(make_event):
    """
    Repayment with zero accrued interest reduces principal directly. The
    smaller principal then earns less interest going forward.

    Scenario chosen so accrued = 0 at the moment of repayment: the rate is
    0% from Jan 1 to Apr 1 (no interest accumulates), the repayment is
    recorded earlier in the day on Apr 1 than the rate change to 12%, so
    the engine sees: zero-rate period → repay → rate change → 12% period.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    # Repayment recorded at 09:00 — before the rate change.
    repay = _repayment(make_event, lender=lender, borrower=borrower,
                       amount="50000", on=date(2026, 4, 1))
    repay_row = event_to_row(repay)
    repay_row["recorded_at"] = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
    # Rate change recorded at 10:00 — after the repayment.
    rate = _rate_change(make_event, lender=lender, borrower=borrower,
                        new_rate_pct="12.0", on=date(2026, 4, 1))
    rate_row = event_to_row(rate)
    rate_row["recorded_at"] = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
    db = FakeConnection()
    db.on_fetch(
        "target_owner_id IS NOT NULL",
        [event_to_row(disb), repay_row, rate_row],
    )
    # Jan 1 → Apr 1: 0% → 0 interest, accrued stays at 0.
    # Apr 1: repay 50k → all to principal (no accrued). Principal = 50,000.
    # Apr 1: rate → 12%.
    # Apr 1 → Dec 31: 274 days at 12% on 50,000 = 50000 * 12/100 * 274/365.
    expected = (
        Decimal("50000") * Decimal("12") / Decimal("100") * Decimal("274") / Decimal("365")
    )
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 12, 31), db
    )
    assert result == expected


async def test_repayment_applied_interest_first_then_principal(make_event):
    """
    When accrued interest is positive at the moment of repayment, the
    repayment is applied to interest FIRST, then the remainder reduces
    principal. Future accrual then runs on the (still-larger) principal.

    Scenario:
      - Rate set to 12% on Jan 1, 100k disbursed same day (rate before disb).
      - 181 days later (Jul 1) interest accrued = 100k * 12/100 * 181/365 = ₹5,950.68
      - Repayment of ₹10,000 on Jul 1: ₹5,950.68 absorbs interest, ₹4,049.32
        reduces principal → principal becomes ₹95,950.68.
      - Window ends Jul 2: tail accrual is 1 day at 12% on the REDUCED principal.

    If the engine wrongly applied principal-first: principal would be ₹90k
    and the tail interest would differ. The assert detects either error.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    rate = _rate_change(make_event, lender=lender, borrower=borrower,
                        new_rate_pct="12.0", on=date(2026, 1, 1))
    rate_row = event_to_row(rate)
    rate_row["recorded_at"] = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    disb_row = event_to_row(disb)
    disb_row["recorded_at"] = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    repay = _repayment(make_event, lender=lender, borrower=borrower,
                       amount="10000", on=date(2026, 7, 1))
    db = FakeConnection()
    db.on_fetch(
        "target_owner_id IS NOT NULL",
        [rate_row, disb_row, event_to_row(repay)],
    )
    # First period: 181 days on 100k at 12%.
    first = (
        Decimal("100000") * Decimal("12") / Decimal("100") * Decimal("181") / Decimal("365")
    )
    # Repay of 10k absorbs `first` of interest, remainder reduces principal:
    principal_after = Decimal("100000") - (Decimal("10000") - first)
    # Tail: 1 day at 12% on principal_after.
    tail = principal_after * Decimal("12") / Decimal("100") * Decimal("1") / Decimal("365")
    expected = first + tail
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 7, 2), db
    )
    assert result == expected


async def test_partial_period_uses_actual_over_365(make_event):
    """A 45-day window must produce 45/365 of the annual rate, exactly."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    rate = _rate_change(make_event, lender=lender, borrower=borrower,
                        new_rate_pct="6.0", on=date(2026, 1, 1))
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    rate_row = event_to_row(rate)
    rate_row["recorded_at"] = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [rate_row, event_to_row(disb)])
    # 2026-01-01 (disb day, no interest) → 2026-02-15 (45 days later)
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 2, 15), db
    )
    expected = Decimal("100000") * Decimal("6") / Decimal("100") * Decimal("45") / Decimal("365")
    assert result == expected


async def test_leap_year_uses_365_not_366(make_event):
    """
    `actual/365` is the contract. 2024 is a leap year (366 days). A loan
    held for the full leap year accrues slightly more than its nominal annual
    rate, because the divisor is still 365. If the engine ever switches to
    actual/actual, this test will fail and force a doc + decision update.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    rate = _rate_change(make_event, lender=lender, borrower=borrower,
                        new_rate_pct="6.0", on=date(2024, 1, 1))
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2024, 1, 1))
    rate_row = event_to_row(rate)
    rate_row["recorded_at"] = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [rate_row, event_to_row(disb)])
    # 2024-01-01 to 2025-01-01 spans 366 days. With actual/365, the result
    # is (100000 * 6/100 * 366) / 365 = 6016.43... — slightly more than the
    # nominal ₹6,000 of "one year of interest."
    result = await calculate_accrued_interest(
        lender, borrower, date(2024, 1, 1), date(2025, 1, 1), db
    )
    expected_365 = (
        Decimal("100000") * Decimal("6") / Decimal("100") * Decimal("366") / Decimal("365")
    )
    expected_actual_actual = Decimal("6000")
    assert result == expected_365
    assert result != expected_actual_actual


# -----------------------------------------------------------------------------
# generate_fy_statement
# -----------------------------------------------------------------------------

async def test_fy_statement_IN_calendar_correct_date_range(make_event):
    """Indian FY 2026 = April 1 2026 → March 31 2027."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    db = FakeConnection()
    # All projection calls return empty / zero — this test only checks date math.
    db.on_fetch("target_owner_id IS NOT NULL", [])
    result = await generate_fy_statement(lender, borrower, 2026, db, calendar="IN")
    assert result["fy_start"] == date(2026, 4, 1)
    assert result["fy_end"] == date(2027, 3, 31)


async def test_fy_statement_US_calendar_correct_date_range(make_event):
    """US FY 2026 = January 1 2026 → December 31 2026."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [])
    result = await generate_fy_statement(lender, borrower, 2026, db, calendar="US")
    assert result["fy_start"] == date(2026, 1, 1)
    assert result["fy_end"] == date(2026, 12, 31)


async def test_fy_statement_opening_balance_matches_prior_period(make_event):
    """
    Opening balance is principal as of `fy_start - 1 day`. A disbursement
    a year earlier shows up as the opening balance.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    earlier = _disbursement(make_event, lender=lender, borrower=borrower,
                            amount="50000", on=date(2025, 6, 1))
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(earlier)])
    result = await generate_fy_statement(lender, borrower, 2026, db, calendar="IN")
    assert result["opening_balance_inr"] == Decimal("50000")


async def test_monthly_breakdown_covers_all_12_months(make_event):
    """Indian FY → 12 monthly rows, even with no events."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [])
    result = await generate_fy_statement(lender, borrower, 2026, db, calendar="IN")
    months = [m["month"] for m in result["monthly_breakdown"]]
    assert len(months) == 12
    assert months[0] == "2026-04"
    assert months[-1] == "2027-03"


async def test_monthly_breakdown_zero_activity_months_included(make_event):
    """A month with no events appears with zeros, not omitted."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    only = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="10000", on=date(2026, 4, 15))
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(only)])
    result = await generate_fy_statement(lender, borrower, 2026, db, calendar="IN")
    # May 2026 has zero activity but must appear.
    may = next(m for m in result["monthly_breakdown"] if m["month"] == "2026-05")
    assert may["disbursements"] == Decimal("0")
    assert may["repayments"] == Decimal("0")
    assert may["interest_accrued"] == Decimal("0")  # rate is still default 0%


async def test_fy_statement_events_list_contains_only_fy_events(make_event):
    """The audit `events` list is bounded by the FY window."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    in_fy = _disbursement(make_event, lender=lender, borrower=borrower,
                          amount="10000", on=date(2026, 6, 1))
    # The fetcher returns all events in the FY range — the function does
    # not double-filter, but it also won't include rows outside the window
    # because the SQL filter uses BETWEEN.
    db = FakeConnection()
    db.on_fetch("BETWEEN $3 AND $4", [event_to_row(in_fy)])
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(in_fy)])
    result = await generate_fy_statement(lender, borrower, 2026, db, calendar="IN")
    assert len(result["events"]) == 1
    assert result["events"][0]["effective_date"] == date(2026, 6, 1)


# -----------------------------------------------------------------------------
# Coverage extras
# -----------------------------------------------------------------------------

async def test_unsupported_calendar_raises():
    """`generate_fy_statement` rejects unknown calendar values explicitly."""
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [])
    with pytest.raises(ValueError, match="Unsupported calendar"):
        await generate_fy_statement(uuid.uuid4(), uuid.uuid4(), 2026, db, calendar="ZZ")


async def test_rate_change_with_decimal_metadata(make_event):
    """A RATE_CHANGE whose new_rate_pct is already a Decimal is consumed as-is."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    rate = make_event(
        event_type=EventType.INTERPERSONAL_RATE_CHANGE,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=None,
        effective_date=date(2026, 1, 1),
        recorded_at=datetime(2026, 1, 1, 9, tzinfo=UTC),
        # Pass an actual Decimal (not str) so _new_rate_from_event hits the
        # Decimal-passthrough branch.
        metadata={"new_rate_pct": Decimal("4.5")},
    )
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    disb_row = event_to_row(disb)
    disb_row["recorded_at"] = datetime(2026, 1, 1, 10, tzinfo=UTC)
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(rate), disb_row])
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 1, 366 if False else 31), db
    )
    expected = (
        Decimal("100000") * Decimal("4.5") / Decimal("100") * Decimal("30") / Decimal("365")
    )
    assert result == expected


async def test_rate_change_with_missing_metadata_is_no_op(make_event):
    """A malformed RATE_CHANGE without new_rate_pct is silently skipped."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    bad_rate = make_event(
        event_type=EventType.INTERPERSONAL_RATE_CHANGE,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=None,
        effective_date=date(2026, 1, 1),
        recorded_at=datetime(2026, 1, 1, 9, tzinfo=UTC),
        metadata={},  # no new_rate_pct key
    )
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    disb_row = event_to_row(disb)
    disb_row["recorded_at"] = datetime(2026, 1, 1, 10, tzinfo=UTC)
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(bad_rate), disb_row])
    # Without a rate, accrual stays at default 0% → zero interest.
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 6, 1), db
    )
    assert result == Decimal("0")


async def test_interest_engine_handles_string_jsonb(make_event):
    """`_row_to_event` inside interest.py coerces str metadata / recorded_at."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    rate = _rate_change(make_event, lender=lender, borrower=borrower,
                        new_rate_pct="6.0", on=date(2026, 1, 1))
    rate_row = event_to_row(rate)
    rate_row["metadata"] = '{"new_rate_pct": "6.0"}'
    rate_row["recorded_at"] = "2026-01-01T09:00:00+00:00"
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    disb_row = event_to_row(disb)
    disb_row["recorded_at"] = "2026-01-01T10:00:00+00:00"
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [rate_row, disb_row])
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 1, 31), db
    )
    expected = Decimal("100000") * Decimal("6") / Decimal("100") * Decimal("30") / Decimal("365")
    assert result == expected


async def test_small_repayment_fully_absorbed_by_interest(make_event):
    """
    A repayment SMALLER than current accrued interest never reduces principal —
    it just shrinks the outstanding-interest counter. Future accrual continues
    on the original principal.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    rate = _rate_change(make_event, lender=lender, borrower=borrower,
                        new_rate_pct="12.0", on=date(2026, 1, 1))
    rate_row = event_to_row(rate)
    rate_row["recorded_at"] = datetime(2026, 1, 1, 9, tzinfo=UTC)
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    disb_row = event_to_row(disb)
    disb_row["recorded_at"] = datetime(2026, 1, 1, 10, tzinfo=UTC)
    # 181 days of accrual = ~5950 of accrued interest. Repay only ₹100.
    tiny_repay = _repayment(make_event, lender=lender, borrower=borrower,
                            amount="100", on=date(2026, 7, 1))
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [rate_row, disb_row, event_to_row(tiny_repay)])
    # Tail: 1 day at 12% on UNCHANGED ₹100,000 (repay was absorbed by interest).
    first = (
        Decimal("100000") * Decimal("12") / Decimal("100") * Decimal("181") / Decimal("365")
    )
    tail = (
        Decimal("100000") * Decimal("12") / Decimal("100") * Decimal("1") / Decimal("365")
    )
    expected = first + tail
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 7, 2), db
    )
    assert result == expected


async def test_period_entirely_before_first_event(make_event):
    """
    If `period_end` falls before the first event, no interest can have accrued.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    later = _disbursement(make_event, lender=lender, borrower=borrower,
                          amount="100000", on=date(2026, 6, 1))
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(later)])
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 5, 1), db
    )
    assert result == Decimal("0")


async def test_signed_pair_delta_no_op_event(make_event):
    """
    An event whose router returns no `interpersonal` key (e.g., a malformed
    COMPENSATING_ENTRY missing `original_event_type`) must not crash — it
    contributes Decimal('0') to the principal walk.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    bad_comp = make_event(
        event_type=EventType.COMPENSATING_ENTRY,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("-1000"),
        reverses_event_id=uuid.uuid4(),
        # Missing `original_event_type` → router returns {} → engine treats as no-op.
        metadata={"reverses_original_event": "abc"},
    )
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(bad_comp)])
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 12, 31), db
    )
    assert result == Decimal("0")


async def test_fy_statement_totals_disbursed_and_repaid(make_event):
    """The flat totals at the top of an FY statement reflect the full year's activity."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 5, 1))
    repay = _repayment(make_event, lender=lender, borrower=borrower,
                       amount="30000", on=date(2026, 9, 1))
    db = FakeConnection()
    rows = [event_to_row(disb), event_to_row(repay)]
    # Same handler matches both the FY events query and the per-pair queries
    # the function makes internally.
    db.on_fetch("target_owner_id IS NOT NULL", rows)
    result = await generate_fy_statement(lender, borrower, 2026, db, calendar="IN")
    assert result["total_disbursed_inr"] == Decimal("100000")
    assert result["total_repaid_inr"] == Decimal("30000")
    # The September row in the breakdown reflects the repayment.
    september = next(m for m in result["monthly_breakdown"] if m["month"] == "2026-09")
    assert september["repayments"] == Decimal("30000")


async def test_interest_engine_null_metadata_treated_as_empty(make_event):
    """NULL JSONB metadata is treated as `{}` (consistent with balance.py)."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = _disbursement(make_event, lender=lender, borrower=borrower,
                         amount="100000", on=date(2026, 1, 1))
    disb_row = event_to_row(disb)
    disb_row["metadata"] = None
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [disb_row])
    # No rate ever set → default 0% → zero interest.
    result = await calculate_accrued_interest(
        lender, borrower, date(2026, 1, 1), date(2026, 6, 1), db
    )
    assert result == Decimal("0")


# Sanity: keep an unused-import warning quiet.
_ = pytest
