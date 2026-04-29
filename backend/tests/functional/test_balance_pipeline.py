"""
End-to-end functional tests for the projection pipeline. Each test inserts
real events into a real schema, then queries through the projection
functions and asserts on the result.

Skipped unless `TEST_DATABASE_URL` is set — see conftest.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from core.balance import (
    get_interpersonal_balance,
    get_loan_balance,
    get_owner_contributions,
)
from core.events import EventType, build_compensating_entry
from core.interest import calculate_accrued_interest

from .conftest import (
    DATABASE_AVAILABLE,
    TEST_SECRET,
    event,
    insert_event,
    make_bank_loan,
    make_owner,
    make_property,
)

pytestmark = pytest.mark.skipif(
    not DATABASE_AVAILABLE,
    reason="TEST_DATABASE_URL not set — see backend/tests/functional/README.md",
)


async def test_scenario_v_fronts_emi_for_p(db):
    """
    V pays one full EMI of ₹15k on behalf of P:
      → CONTRIBUTION (V, ₹15k) credits V's CapEx
      → INTERPERSONAL_LOAN_DISBURSEMENT (V→P, ₹15k) creates the debt
    Both projection paths must reflect the result.
    """
    prop = await make_property(db)
    v = await make_owner(db, prop, name="V", email="v@example.com", equity_pct=Decimal("50"))
    p = await make_owner(db, prop, name="P", email="p@example.com", equity_pct=Decimal("50"))

    contrib = event(
        property_id=prop,
        event_type=EventType.CONTRIBUTION,
        actor=v,
        amount=Decimal("15000"),
        effective_date=date(2026, 5, 15),
        description="V's contribution to fund May EMI",
    )
    await insert_event(db, contrib)

    disb = event(
        property_id=prop,
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor=v,
        target=p,
        amount=Decimal("15000"),
        effective_date=date(2026, 5, 15),
        description="V fronted P's share of May EMI",
        recorded_at=datetime(2026, 5, 15, 13, tzinfo=UTC),
    )
    await insert_event(db, disb)

    assert await get_interpersonal_balance(v, p, date(2026, 6, 1), db) == Decimal("15000")
    contributions = await get_owner_contributions(v, prop, date(2026, 6, 1), db)
    assert contributions["capex_inr"] == Decimal("15000")


async def test_scenario_partial_repayment_and_settlement(db):
    """
    V disbursed ₹50k → P repays ₹20k via wire → P settles ₹5k via dinner.
    Net debt P→V is ₹25k.
    """
    prop = await make_property(db)
    v = await make_owner(db, prop, name="V", email="v@example.com", equity_pct=Decimal("50"))
    p = await make_owner(db, prop, name="P", email="p@example.com", equity_pct=Decimal("50"))

    await insert_event(
        db,
        event(
            property_id=prop,
            event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
            actor=v,
            target=p,
            amount=Decimal("50000"),
            effective_date=date(2026, 5, 1),
        ),
    )
    await insert_event(
        db,
        event(
            property_id=prop,
            event_type=EventType.INTERPERSONAL_LOAN_REPAYMENT,
            actor=p,
            target=v,
            amount=Decimal("20000"),
            effective_date=date(2026, 7, 1),
        ),
    )
    await insert_event(
        db,
        event(
            property_id=prop,
            event_type=EventType.SETTLEMENT,
            actor=p,
            target=v,
            amount=Decimal("5000"),
            effective_date=date(2026, 8, 1),
            description="P covered V's dinner in Mumbai",
            metadata={"method": "dinner", "city": "Mumbai"},
        ),
    )

    assert await get_interpersonal_balance(v, p, date(2026, 9, 1), db) == Decimal("25000")


async def test_scenario_compensating_entry_corrects_error(db):
    """
    V records ₹10,000 (data-entry slip — should be ₹1,000). The fix is a
    compensating entry plus a corrected disbursement. Both rows persist;
    time-travel queries before and after the correction return the right
    balance.
    """
    prop = await make_property(db)
    v = await make_owner(db, prop, name="V", email="v@example.com", equity_pct=Decimal("50"))
    p = await make_owner(db, prop, name="P", email="p@example.com", equity_pct=Decimal("50"))

    bad = event(
        property_id=prop,
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor=v,
        target=p,
        amount=Decimal("10000"),
        effective_date=date(2026, 6, 15),
        description="Bad: should have been ₹1,000",
    )
    bad_id = await insert_event(db, bad)

    # Fast-forward "today": before the correction, balance reads ₹10,000.
    assert await get_interpersonal_balance(v, p, date(2026, 6, 18), db) == Decimal("10000")

    # Correction lands on 2026-06-19 — but uses the original effective_date
    # so historical queries also reflect the fix.
    comp = build_compensating_entry(
        bad,
        actor_email="corrector@example.com",
        description="Reverses bad ₹10,000 entry — actual was ₹1,000.",
        secret_key=TEST_SECRET,
    )
    comp.recorded_at = datetime(2026, 6, 19, 9, tzinfo=UTC)
    await insert_event(db, comp)

    corrected = event(
        property_id=prop,
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor=v,
        target=p,
        amount=Decimal("1000"),
        effective_date=date(2026, 6, 15),  # original effective_date
        description="Correct disbursement — re-issued after compensating bad row.",
        recorded_at=datetime(2026, 6, 19, 9, 5, tzinfo=UTC),
    )
    await insert_event(db, corrected)

    # After the correction, all historical queries see ₹1,000 (because both
    # bad and comp share the same effective_date).
    assert await get_interpersonal_balance(v, p, date(2026, 6, 20), db) == Decimal("1000")
    # The bad and compensating rows BOTH still exist in the events table.
    surviving = await db.fetchval(
        "SELECT COUNT(*) FROM events WHERE id IN ($1, $2)",
        bad_id,
        comp.id,
    )
    assert surviving == 2


async def test_scenario_as_of_date_time_travel(db):
    """
    Point-in-time queries: before any events, balance is 0. As repayments
    land, the balance steps down through the timeline.
    """
    prop = await make_property(db)
    v = await make_owner(db, prop, name="V", email="v@example.com", equity_pct=Decimal("50"))
    p = await make_owner(db, prop, name="P", email="p@example.com", equity_pct=Decimal("50"))

    await insert_event(
        db,
        event(
            property_id=prop,
            event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
            actor=v,
            target=p,
            amount=Decimal("30000"),
            effective_date=date(2026, 1, 1),
        ),
    )
    await insert_event(
        db,
        event(
            property_id=prop,
            event_type=EventType.INTERPERSONAL_LOAN_REPAYMENT,
            actor=p,
            target=v,
            amount=Decimal("10000"),
            effective_date=date(2026, 2, 1),
        ),
    )
    await insert_event(
        db,
        event(
            property_id=prop,
            event_type=EventType.INTERPERSONAL_LOAN_REPAYMENT,
            actor=p,
            target=v,
            amount=Decimal("10000"),
            effective_date=date(2026, 3, 1),
        ),
    )

    assert await get_interpersonal_balance(v, p, date(2025, 12, 31), db) == Decimal("0")
    assert await get_interpersonal_balance(v, p, date(2026, 1, 31), db) == Decimal("30000")
    assert await get_interpersonal_balance(v, p, date(2026, 2, 28), db) == Decimal("20000")
    assert await get_interpersonal_balance(v, p, date(2026, 3, 31), db) == Decimal("10000")


async def test_scenario_interest_accrual_end_to_end(db):
    """
    V disburses ₹100,000 on Jan 1 at the default 0% rate. On Apr 1 the rate
    changes to 6%. Querying [Jan 1, Jul 1] returns 91 days of interest at
    6% on ₹100,000, and zero for the Jan-Mar zero-rate period.
    """
    prop = await make_property(db)
    v = await make_owner(db, prop, name="V", email="v@example.com", equity_pct=Decimal("50"))
    p = await make_owner(db, prop, name="P", email="p@example.com", equity_pct=Decimal("50"))

    await insert_event(
        db,
        event(
            property_id=prop,
            event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
            actor=v,
            target=p,
            amount=Decimal("100000"),
            effective_date=date(2026, 1, 1),
        ),
    )
    await insert_event(
        db,
        event(
            property_id=prop,
            event_type=EventType.INTERPERSONAL_RATE_CHANGE,
            actor=v,
            target=p,
            effective_date=date(2026, 4, 1),
            metadata={"new_rate_pct": "6.0", "previous_rate_pct": "0.0"},
            description="Switch to 6% p.a.",
        ),
    )

    # 91 days from Apr 1 to Jul 1, on ₹100,000 at 6%.
    expected = Decimal("100000") * Decimal("6") / Decimal("100") * Decimal("91") / Decimal("365")
    result = await calculate_accrued_interest(v, p, date(2026, 1, 1), date(2026, 7, 1), db)
    assert result == expected

    # The pre-rate-change window contributes nothing.
    pre_only = await calculate_accrued_interest(v, p, date(2026, 1, 1), date(2026, 3, 31), db)
    assert pre_only == Decimal("0")


async def test_scenario_loan_balance_decreases_via_emi_principal(db):
    """
    Original loan ₹1,000,000. One EMI paid (₹35k principal + ₹15k interest).
    Outstanding equals 1,000,000 − 35,000 (interest doesn't reduce principal).
    """
    prop = await make_property(db)
    payer = await make_owner(db, prop, name="V", email="v@example.com", equity_pct=Decimal("100"))
    loan = await make_bank_loan(db, prop, principal=Decimal("1000000"))

    # Insert an emi_schedule row marked paid — what the projection sees as
    # the canonical record of "this EMI has been paid."
    await db.execute(
        """
        INSERT INTO emi_schedule (
            loan_id, due_date, principal_component, interest_component,
            total_emi, status, paid_at, paid_by_owner_id
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        loan,
        date(2026, 5, 15),
        Decimal("35000"),
        Decimal("15000"),
        Decimal("50000"),
        "paid",
        datetime(2026, 5, 15, 12, tzinfo=UTC),
        payer,
    )
    # Plus the corresponding event.
    await insert_event(
        db,
        event(
            property_id=prop,
            event_type=EventType.EMI_PAYMENT,
            actor=payer,
            loan_id=loan,
            amount=Decimal("50000"),
            effective_date=date(2026, 5, 15),
            metadata={
                "principal_component": "35000",
                "interest_component": "15000",
                "emi_schedule_id": "00000000-0000-0000-0000-000000000000",
            },
        ),
    )

    assert await get_loan_balance(loan, date(2026, 6, 1), db) == Decimal("965000")
