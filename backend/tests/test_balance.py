"""
Unit tests for backend/core/balance.py.

These tests use the FakeConnection mock from `_fakes.py` to drive the
projection functions without a real database. The functional tests in
`backend/tests/functional/` exercise the same code paths against a live
Postgres schema.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from _fakes import FakeConnection, event_to_row

from core.balance import (
    get_interpersonal_balance,
    get_interpersonal_balance_with_interest,
    get_loan_balance,
    get_owner_contributions,
    project_exit_scenario,
)
from core.events import EventType

# -----------------------------------------------------------------------------
# get_interpersonal_balance
# -----------------------------------------------------------------------------


async def test_balance_is_zero_with_no_events():
    """No events for the pair → balance is exactly Decimal('0')."""
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [])
    result = await get_interpersonal_balance(uuid.uuid4(), uuid.uuid4(), date(2026, 5, 1), db)
    assert result == Decimal("0")


async def test_single_disbursement_creates_positive_balance(make_event):
    """One DISBURSEMENT lender→borrower → positive balance equal to amount."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    e = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("50000.00"),
        metadata={},
    )
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(e)])
    assert await get_interpersonal_balance(lender, borrower, date(2026, 6, 1), db) == Decimal(
        "50000.00"
    )


async def test_repayment_reduces_balance(make_event):
    """A repayment (actor=borrower, target=lender) reduces the running total."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("50000"),
        metadata={},
    )
    repay = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_REPAYMENT,
        actor_owner_id=borrower,
        target_owner_id=lender,
        amount_property_currency=Decimal("20000"),
        metadata={},
    )
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb), event_to_row(repay)])
    assert await get_interpersonal_balance(lender, borrower, date(2026, 9, 1), db) == Decimal(
        "30000"
    )


async def test_full_repayment_reaches_zero(make_event):
    """Disbursement and equal repayment net to zero exactly."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("50000"),
        metadata={},
    )
    repay = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_REPAYMENT,
        actor_owner_id=borrower,
        target_owner_id=lender,
        amount_property_currency=Decimal("50000"),
        metadata={},
    )
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb), event_to_row(repay)])
    assert await get_interpersonal_balance(lender, borrower, date(2026, 9, 1), db) == Decimal("0")


async def test_over_repayment_returns_negative_balance(make_event):
    """Over-repayment flips the sign — the named lender is now the debtor."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("10000"),
        metadata={},
    )
    repay = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_REPAYMENT,
        actor_owner_id=borrower,
        target_owner_id=lender,
        amount_property_currency=Decimal("12000"),
        metadata={},
    )
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb), event_to_row(repay)])
    assert await get_interpersonal_balance(lender, borrower, date(2026, 9, 1), db) == Decimal(
        "-2000"
    )


async def test_settlement_reduces_balance_same_as_repayment(make_event):
    """A SETTLEMENT (in-kind transfer from borrower to lender) behaves like a repayment."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("10000"),
        metadata={},
    )
    settle = make_event(
        event_type=EventType.SETTLEMENT,
        actor_owner_id=borrower,
        target_owner_id=lender,
        amount_property_currency=Decimal("4000"),
        metadata={"method": "dinner"},
    )
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb), event_to_row(settle)])
    assert await get_interpersonal_balance(lender, borrower, date(2026, 9, 1), db) == Decimal(
        "6000"
    )


async def test_compensating_entry_negates_disbursement(make_event):
    """A COMPENSATING_ENTRY routed through DISBURSEMENT framing exactly cancels the original."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("10000"),
        metadata={},
    )
    comp = make_event(
        event_type=EventType.COMPENSATING_ENTRY,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("-10000"),
        reverses_event_id=disb.id,
        metadata={
            "original_event_type": "INTERPERSONAL_LOAN_DISBURSEMENT",
            "reverses_original_event": str(disb.id),
        },
    )
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb), event_to_row(comp)])
    assert await get_interpersonal_balance(lender, borrower, date(2026, 9, 1), db) == Decimal("0")


async def test_as_of_date_excludes_future_events(make_event):
    """
    Future events (effective_date > as_of_date) must not affect the result.
    Enforced by the SQL filter; this test confirms the function honors what
    the DB feeds it: passing only the in-window rows produces only the
    in-window balance.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb_in_window = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("5000"),
        effective_date=date(2026, 5, 1),
        metadata={},
    )
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb_in_window)])
    # Asking for as_of_date before the future event — DB returns only the in-window row.
    assert await get_interpersonal_balance(lender, borrower, date(2026, 6, 1), db) == Decimal(
        "5000"
    )


async def test_multiple_disbursements_accumulate(make_event):
    """Repeated disbursements stack additively without rounding drift."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    rows = []
    for amount in ("100.50", "200.25", "300.10"):
        e = make_event(
            event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
            actor_owner_id=lender,
            target_owner_id=borrower,
            amount_property_currency=Decimal(amount),
            metadata={},
        )
        rows.append(event_to_row(e))
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", rows)
    assert await get_interpersonal_balance(lender, borrower, date(2026, 9, 1), db) == Decimal(
        "600.85"
    )


async def test_balance_is_principal_only_no_interest(make_event):
    """
    `get_interpersonal_balance` must NOT include accrued interest, even when
    a RATE_CHANGE event appears in the stream. RATE_CHANGE has no principal
    effect.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("100000"),
        metadata={},
    )
    rate = make_event(
        event_type=EventType.INTERPERSONAL_RATE_CHANGE,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=None,
        metadata={"new_rate_pct": Decimal("6.0")},
    )
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb), event_to_row(rate)])
    # No interest layered in — balance equals principal exactly.
    assert await get_interpersonal_balance(lender, borrower, date(2027, 9, 1), db) == Decimal(
        "100000"
    )


# -----------------------------------------------------------------------------
# get_loan_balance
# -----------------------------------------------------------------------------


async def test_loan_balance_equals_principal_before_any_payment():
    """Fresh loan, no payments → outstanding equals original principal."""
    loan_id = uuid.uuid4()
    db = FakeConnection()
    db.on_fetchval("FROM bank_loans WHERE id", Decimal("1000000"))
    db.on_fetch("WHERE loan_id = $1", [])
    assert await get_loan_balance(loan_id, date(2026, 6, 1), db) == Decimal("1000000")


async def test_paid_emi_reduces_balance_by_principal_component(make_event):
    """An EMI_PAYMENT event reduces principal by metadata.principal_component, not the gross."""
    loan_id = uuid.uuid4()
    payer = uuid.uuid4()
    emi = make_event(
        event_type=EventType.EMI_PAYMENT,
        actor_owner_id=payer,
        target_owner_id=None,
        loan_id=loan_id,
        amount_property_currency=Decimal("50000"),  # principal + interest gross
        metadata={
            "principal_component": Decimal("35000"),
            "interest_component": Decimal("15000"),
            "emi_schedule_id": str(uuid.uuid4()),
        },
    )
    db = FakeConnection()
    db.on_fetchval("FROM bank_loans WHERE id", Decimal("1000000"))
    db.on_fetch("WHERE loan_id = $1", [event_to_row(emi)])
    # Only the principal component reduces outstanding.
    assert await get_loan_balance(loan_id, date(2026, 6, 1), db) == Decimal("965000")


async def test_bulk_prepayment_reduces_balance(make_event):
    """A BULK_PREPAYMENT reduces principal by the full event amount."""
    loan_id = uuid.uuid4()
    payer = uuid.uuid4()
    prepay = make_event(
        event_type=EventType.BULK_PREPAYMENT,
        actor_owner_id=payer,
        target_owner_id=None,
        loan_id=loan_id,
        amount_property_currency=Decimal("200000"),
        metadata={},
    )
    db = FakeConnection()
    db.on_fetchval("FROM bank_loans WHERE id", Decimal("1000000"))
    db.on_fetch("WHERE loan_id = $1", [event_to_row(prepay)])
    assert await get_loan_balance(loan_id, date(2026, 6, 1), db) == Decimal("800000")


async def test_loan_balance_never_goes_below_zero(make_event):
    """If a data-entry error would underflow, the function floors at zero."""
    loan_id = uuid.uuid4()
    payer = uuid.uuid4()
    overpay = make_event(
        event_type=EventType.BULK_PREPAYMENT,
        actor_owner_id=payer,
        target_owner_id=None,
        loan_id=loan_id,
        amount_property_currency=Decimal("999999999"),  # absurdly large
        metadata={},
    )
    db = FakeConnection()
    db.on_fetchval("FROM bank_loans WHERE id", Decimal("100000"))
    db.on_fetch("WHERE loan_id = $1", [event_to_row(overpay)])
    assert await get_loan_balance(loan_id, date(2026, 6, 1), db) == Decimal("0")


async def test_fully_repaid_loan_returns_zero(make_event):
    """Sum of payments equals principal exactly → outstanding is exactly 0."""
    loan_id = uuid.uuid4()
    payer = uuid.uuid4()
    prepay = make_event(
        event_type=EventType.BULK_PREPAYMENT,
        actor_owner_id=payer,
        target_owner_id=None,
        loan_id=loan_id,
        amount_property_currency=Decimal("500000"),
        metadata={},
    )
    db = FakeConnection()
    db.on_fetchval("FROM bank_loans WHERE id", Decimal("500000"))
    db.on_fetch("WHERE loan_id = $1", [event_to_row(prepay)])
    assert await get_loan_balance(loan_id, date(2026, 6, 1), db) == Decimal("0")


# -----------------------------------------------------------------------------
# get_owner_contributions
# -----------------------------------------------------------------------------


async def test_contribution_event_counted_in_capex(make_event):
    """A same-currency CONTRIBUTION credits CapEx by amount_property_currency."""
    owner_id, property_id = uuid.uuid4(), uuid.uuid4()
    contrib = make_event(
        event_type=EventType.CONTRIBUTION,
        actor_owner_id=owner_id,
        property_id=property_id,
        amount_property_currency=Decimal("100000"),
        inr_landed=None,
        fx_rate_actual=None,
        source_currency="INR",
        property_currency="INR",
        metadata={},
    )
    db = FakeConnection()
    db.on_fetch("AND actor_owner_id = $2", [event_to_row(contrib)])
    result = await get_owner_contributions(owner_id, property_id, date(2026, 6, 1), db)
    assert result["capex_inr"] == Decimal("100000")
    assert result["opex_inr"] == Decimal("0")
    assert result["total_inr"] == Decimal("100000")
    assert result["event_count"] == 1


async def test_opex_split_counted_in_opex(make_event):
    """An OPEX_SPLIT row where actor=this-owner credits OpEx, not CapEx."""
    owner_id, payer_id, property_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    split = make_event(
        event_type=EventType.OPEX_SPLIT,
        actor_owner_id=owner_id,
        target_owner_id=payer_id,
        property_id=property_id,
        amount_property_currency=Decimal("40000"),
        metadata={"share_pct": Decimal("33.3333")},
    )
    db = FakeConnection()
    db.on_fetch("AND actor_owner_id = $2", [event_to_row(split)])
    result = await get_owner_contributions(owner_id, property_id, date(2026, 6, 1), db)
    assert result["capex_inr"] == Decimal("0")
    assert result["opex_inr"] == Decimal("40000")
    assert result["total_inr"] == Decimal("40000")


async def test_fx_event_uses_inr_landed_not_usd_times_rate(make_event):
    """
    The dual-rate rule: cross-currency contributions credit `inr_landed`
    (post-fee). amount_source_currency × fx_rate_actual would over-credit.
    """
    owner_id, property_id = uuid.uuid4(), uuid.uuid4()
    contrib = make_event(
        event_type=EventType.CONTRIBUTION,
        actor_owner_id=owner_id,
        property_id=property_id,
        amount_source_currency=Decimal("5000.00"),
        source_currency="USD",
        amount_property_currency=Decimal("416000.00"),
        property_currency="INR",
        fx_rate_actual=Decimal("83.20"),
        fx_rate_reference=Decimal("83.45"),
        fee_source_currency=Decimal("25.00"),
        inr_landed=Decimal("413920.00"),
        metadata={},
    )
    db = FakeConnection()
    db.on_fetch("AND actor_owner_id = $2", [event_to_row(contrib)])
    result = await get_owner_contributions(owner_id, property_id, date(2026, 6, 1), db)
    # Must equal inr_landed (₹413,920), NOT amount_source × rate (₹416,000)
    # nor amount_property_currency (which is the gross before fees).
    assert result["capex_inr"] == Decimal("413920.00")


async def test_future_events_excluded_by_as_of_date(make_event):
    """
    The SQL filter excludes events past `as_of_date`. We model that by only
    handing the function in-window rows: if the function added anything else
    it would be wrong.
    """
    owner_id, property_id = uuid.uuid4(), uuid.uuid4()
    in_window = make_event(
        event_type=EventType.CONTRIBUTION,
        actor_owner_id=owner_id,
        property_id=property_id,
        amount_property_currency=Decimal("100000"),
        inr_landed=None,
        effective_date=date(2026, 5, 1),
        source_currency="INR",
        property_currency="INR",
        fx_rate_actual=None,
        metadata={},
    )
    db = FakeConnection()
    db.on_fetch("AND actor_owner_id = $2", [event_to_row(in_window)])
    result = await get_owner_contributions(owner_id, property_id, date(2026, 5, 31), db)
    assert result["capex_inr"] == Decimal("100000")


async def test_contributions_only_counted_for_actor_owner(make_event):
    """
    The SQL filter scopes by actor_owner_id. If the DB hands back a row for a
    different actor, the function must not count it. We model this by giving
    the function rows for the requested owner only.
    """
    owner_id, other_owner, property_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    own_contrib = make_event(
        event_type=EventType.CONTRIBUTION,
        actor_owner_id=owner_id,
        property_id=property_id,
        amount_property_currency=Decimal("100000"),
        inr_landed=None,
        source_currency="INR",
        property_currency="INR",
        fx_rate_actual=None,
        metadata={},
    )
    # The DB only returns rows where actor_owner_id matches the query arg —
    # other_owner's contributions never reach the projection.
    db = FakeConnection()
    db.on_fetch("AND actor_owner_id = $2", [event_to_row(own_contrib)])
    result = await get_owner_contributions(owner_id, property_id, date(2026, 6, 1), db)
    assert result["capex_inr"] == Decimal("100000")
    assert result["event_count"] == 1
    # Sanity: the variable exists but wasn't used in the response.
    _ = other_owner


# -----------------------------------------------------------------------------
# project_exit_scenario
# -----------------------------------------------------------------------------


async def test_buyout_2_equals_equity_pct_of_market_value(monkeypatch):
    """Market-value buyout = market_value × (equity_pct / 100), to the cent."""
    owner_id, property_id = uuid.uuid4(), uuid.uuid4()

    async def _zero_capex(*_a, **_k):
        return {
            "capex_inr": Decimal("0"),
            "opex_inr": Decimal("0"),
            "total_inr": Decimal("0"),
            "event_count": 0,
        }

    monkeypatch.setattr("core.balance.get_owner_contributions", _zero_capex)

    db = FakeConnection()
    db.on_fetchval("FROM owners WHERE id", Decimal("33.3333"))
    db.on_fetch("DISTINCT counterparty", [])
    result = await project_exit_scenario(owner_id, property_id, Decimal("15000000"), db)
    expected = Decimal("15000000") * Decimal("33.3333") / Decimal("100")
    assert result["buyout_market_value_share"] == expected


async def test_buyout_3_is_equal_weight_blend_by_default(monkeypatch):
    """Default 50/50 weights → buyout_3 is the simple average of #1 and #2."""
    owner_id, property_id = uuid.uuid4(), uuid.uuid4()

    async def _capex_500k(*_a, **_k):
        return {
            "capex_inr": Decimal("500000"),
            "opex_inr": Decimal("0"),
            "total_inr": Decimal("500000"),
            "event_count": 1,
        }

    monkeypatch.setattr("core.balance.get_owner_contributions", _capex_500k)

    db = FakeConnection()
    db.on_fetchval("FROM owners WHERE id", Decimal("50"))
    db.on_fetch("DISTINCT counterparty", [])
    result = await project_exit_scenario(owner_id, property_id, Decimal("2000000"), db)
    assert result["buyout_net_contribution"] == Decimal("500000")
    assert result["buyout_market_value_share"] == Decimal("1000000")
    assert result["buyout_weighted_blend"] == Decimal("750000")


async def test_custom_blend_weights_applied_correctly(monkeypatch):
    """A 30/70 blend on (100, 1000) → 0.3*100 + 0.7*1000 = 730."""
    owner_id, property_id = uuid.uuid4(), uuid.uuid4()

    async def _capex_100(*_a, **_k):
        return {
            "capex_inr": Decimal("100"),
            "opex_inr": Decimal("0"),
            "total_inr": Decimal("100"),
            "event_count": 1,
        }

    monkeypatch.setattr("core.balance.get_owner_contributions", _capex_100)

    db = FakeConnection()
    db.on_fetchval("FROM owners WHERE id", Decimal("100"))  # full ownership
    db.on_fetch("DISTINCT counterparty", [])
    result = await project_exit_scenario(
        owner_id,
        property_id,
        Decimal("1000"),
        db,
        blend_weight_contribution=Decimal("0.3"),
        blend_weight_market=Decimal("0.7"),
    )
    assert result["buyout_weighted_blend"] == Decimal("730")
    assert result["blend_weights"] == {"contribution": Decimal("0.3"), "market": Decimal("0.7")}


async def test_interpersonal_debt_reduces_buyout_1(monkeypatch):
    """Buyout #1 subtracts what this owner owes other owners."""
    owner_id, property_id, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def _capex_1m(*_a, **_k):
        return {
            "capex_inr": Decimal("1000000"),
            "opex_inr": Decimal("0"),
            "total_inr": Decimal("1000000"),
            "event_count": 1,
        }

    async def _pair_balance(lender, borrower, _as_of, _db):
        # other is lender, owner is borrower → owner owes other ₹200k
        if lender == other and borrower == owner_id:
            return Decimal("200000")
        return Decimal("0")

    monkeypatch.setattr("core.balance.get_owner_contributions", _capex_1m)
    monkeypatch.setattr("core.balance.get_interpersonal_balance", _pair_balance)

    db = FakeConnection()
    db.on_fetchval("FROM owners WHERE id", Decimal("0"))
    db.on_fetch("DISTINCT counterparty", [{"counterparty": other}])
    result = await project_exit_scenario(owner_id, property_id, Decimal("0"), db)
    assert result["buyout_net_contribution"] == Decimal("800000")
    assert result["inputs"]["debts_owed_inr"] == Decimal("200000")


async def test_interpersonal_credit_increases_buyout_1(monkeypatch):
    """Buyout #1 adds what other owners owe this owner."""
    owner_id, property_id, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def _capex_1m(*_a, **_k):
        return {
            "capex_inr": Decimal("1000000"),
            "opex_inr": Decimal("0"),
            "total_inr": Decimal("1000000"),
            "event_count": 1,
        }

    async def _pair_balance(lender, borrower, _as_of, _db):
        # owner is lender, other is borrower → other owes owner ₹350k
        if lender == owner_id and borrower == other:
            return Decimal("350000")
        return Decimal("0")

    monkeypatch.setattr("core.balance.get_owner_contributions", _capex_1m)
    monkeypatch.setattr("core.balance.get_interpersonal_balance", _pair_balance)

    db = FakeConnection()
    db.on_fetchval("FROM owners WHERE id", Decimal("0"))
    db.on_fetch("DISTINCT counterparty", [{"counterparty": other}])
    result = await project_exit_scenario(owner_id, property_id, Decimal("0"), db)
    assert result["buyout_net_contribution"] == Decimal("1350000")
    assert result["inputs"]["credits_due_inr"] == Decimal("350000")


# -----------------------------------------------------------------------------
# Coverage extras: uncommon code paths that production data hits but the
# common-case tests skip past.
# -----------------------------------------------------------------------------


async def test_loan_balance_returns_zero_for_unknown_loan():
    """If the loan does not exist, the function returns Decimal('0') rather than raising."""
    db = FakeConnection()
    # No fetchval handler → fetchval returns None → function short-circuits.
    db.on_fetch("WHERE loan_id = $1", [])
    assert await get_loan_balance(uuid.uuid4(), date(2026, 6, 1), db) == Decimal("0")


async def test_project_exit_warns_when_owner_missing(monkeypatch):
    """If the owner is not on the property, a warning is set and equity defaults to 0."""
    owner_id, property_id = uuid.uuid4(), uuid.uuid4()

    async def _zero_capex(*_a, **_k):
        return {
            "capex_inr": Decimal("0"),
            "opex_inr": Decimal("0"),
            "total_inr": Decimal("0"),
            "event_count": 0,
        }

    monkeypatch.setattr("core.balance.get_owner_contributions", _zero_capex)
    db = FakeConnection()
    # No fetchval handler for owners → returns None → triggers warning path.
    db.on_fetch("DISTINCT counterparty", [])
    result = await project_exit_scenario(owner_id, property_id, Decimal("1000000"), db)
    assert result["equity_pct"] == Decimal("0")
    assert result["warning"] is not None
    assert "Owner not found" in result["warning"]


async def test_balance_with_interest_combines_both_engines(monkeypatch):
    """The composite function adds principal + accrued interest to give total_owed_inr."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()

    async def _fake_principal(*_a, **_k):
        return Decimal("100000")

    async def _fake_interest(*_a, **_k):
        return Decimal("1500")

    monkeypatch.setattr("core.balance.get_interpersonal_balance", _fake_principal)
    monkeypatch.setattr("core.balance.calculate_accrued_interest", _fake_interest)
    db = FakeConnection()
    result = await get_interpersonal_balance_with_interest(lender, borrower, date(2026, 6, 1), db)
    assert result["principal_inr"] == Decimal("100000")
    assert result["accrued_interest_inr"] == Decimal("1500")
    assert result["total_owed_inr"] == Decimal("101500")
    assert result["lender_id"] == lender
    assert result["borrower_id"] == borrower


async def test_row_to_event_handles_string_metadata_and_recorded_at(make_event):
    """
    asyncpg returns JSONB as dict and TIMESTAMPTZ as datetime, but a callable
    fixture or a different driver can deliver them as strings — `_row_to_event`
    must coerce. This goes through the public API by feeding such a row.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    e = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("1000"),
        metadata={},
    )
    row = event_to_row(e)
    # Force the str codec paths.
    row["metadata"] = "{}"
    row["recorded_at"] = "2026-05-01T12:00:00+00:00"
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [row])
    assert await get_interpersonal_balance(lender, borrower, date(2026, 6, 1), db) == Decimal(
        "1000"
    )


async def test_balance_query_in_reversed_direction_returns_negative(make_event):
    """
    Asking for `(borrower, lender)` when only `(lender, borrower)` events exist
    returns the negation of the principal balance — exercising the reverse-
    direction branch of the fold.
    """
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    disb = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("70000"),
        metadata={},
    )
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [event_to_row(disb)])
    # Calling with (borrower, lender) — i.e. asking "what does the lender owe the borrower?".
    # The router's effect is normalized to (lender, borrower); we requested the
    # reverse, so the result is -70000.
    swapped = await get_interpersonal_balance(borrower, lender, date(2026, 9, 1), db)
    assert swapped == Decimal("-70000")


async def test_row_to_event_handles_null_metadata(make_event):
    """A NULL metadata column is treated as the empty dict, not as a fatal error."""
    lender, borrower = uuid.uuid4(), uuid.uuid4()
    e = make_event(
        event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        actor_owner_id=lender,
        target_owner_id=borrower,
        amount_property_currency=Decimal("100"),
        metadata={},
    )
    row = event_to_row(e)
    row["metadata"] = None
    db = FakeConnection()
    db.on_fetch("target_owner_id IS NOT NULL", [row])
    assert await get_interpersonal_balance(lender, borrower, date(2026, 6, 1), db) == Decimal("100")


async def test_project_exit_scenario_respects_explicit_as_of_date(monkeypatch):
    """
    project_exit_scenario must honour an explicit `as_of_date` and thread it
    through to get_owner_contributions and get_interpersonal_balance. Passing
    a far-future date and verifying the sub-call receives it confirms the
    time-travel contract (Projection Contract §2) is satisfied — the function
    must never silently use date.today().
    """
    owner_id, property_id = uuid.uuid4(), uuid.uuid4()
    received_as_of: list[date] = []

    async def _capex_recorder(
        _owner_id: uuid.UUID,
        _property_id: uuid.UUID,
        as_of_date: date,
        _db: Any,
    ) -> dict[str, Any]:
        received_as_of.append(as_of_date)
        return {
            "capex_inr": Decimal("0"),
            "opex_inr": Decimal("0"),
            "total_inr": Decimal("0"),
            "event_count": 0,
        }

    monkeypatch.setattr("core.balance.get_owner_contributions", _capex_recorder)

    db = FakeConnection()
    db.on_fetchval("FROM owners WHERE id", Decimal("50"))
    db.on_fetch("DISTINCT counterparty", [])

    target_date = date(2025, 12, 31)
    await project_exit_scenario(
        owner_id, property_id, Decimal("1000000"), db, as_of_date=target_date
    )
    assert received_as_of == [target_date], (
        "as_of_date was not threaded through to get_owner_contributions"
    )


async def test_project_exit_scenario_rejects_blend_weights_not_summing_to_one(monkeypatch):
    """
    blend_weight_contribution + blend_weight_market must equal exactly 1.
    A ValueError is raised immediately so callers cannot produce a silently
    wrong financial answer from mismatched weights.
    """
    owner_id, property_id = uuid.uuid4(), uuid.uuid4()
    db = FakeConnection()

    with pytest.raises(ValueError, match="blend_weight_contribution.*blend_weight_market"):
        await project_exit_scenario(
            owner_id,
            property_id,
            Decimal("1000000"),
            db,
            blend_weight_contribution=Decimal("0.3"),
            blend_weight_market=Decimal("0.3"),  # sums to 0.6, not 1
        )


# Sanity: keep an unused-import warning quiet for fields like recorded_at.
_ = (datetime, UTC, pytest)
