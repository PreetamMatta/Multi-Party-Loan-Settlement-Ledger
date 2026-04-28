"""
Tests for backend/core/events.py.

These tests are the verification layer for the contract documented in
docs/business-logic/event-log.md. Every test docstring states what is being
tested and why it matters in business terms — if a test fails, the docstring
should make clear what real-world correctness property has been broken.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from core.events import (
    EventType,
    build_compensating_entry,
    get_financial_effect,
    sign_event,
    validate_event_fields,
    verify_event,
)


# ---------------------------------------------------------------------------
# HMAC signing and verification
# ---------------------------------------------------------------------------
class TestSigning:
    def test_sign_event_produces_deterministic_signature(self, make_event, secret_key):
        """
        Signing the same event twice with the same key must produce the
        same hex digest. If signatures were non-deterministic, verification
        would be impossible — every read would think the row had been
        tampered.
        """
        evt = make_event()
        sig1 = sign_event(evt, secret_key)
        sig2 = sign_event(evt, secret_key)
        assert sig1 == sig2

    def test_sign_event_different_keys_produce_different_signatures(
        self, make_event, secret_key, alt_secret_key
    ):
        """
        Two different secret keys must produce different signatures over the
        same event. This is the core assumption that key rotation will
        invalidate every existing signature — and the reason key rotation
        is flagged as a future migration concern.
        """
        evt = make_event()
        sig_a = sign_event(evt, secret_key)
        sig_b = sign_event(evt, alt_secret_key)
        assert sig_a != sig_b

    def test_verify_event_returns_true_for_valid_signature(self, make_event, secret_key):
        """
        A freshly signed event must verify cleanly. This is the happy path —
        if it fails, every event ever written is suspect.
        """
        evt = make_event()
        evt.hmac_signature = sign_event(evt, secret_key)
        assert verify_event(evt, secret_key) is True

    def test_verify_event_returns_false_if_amount_tampered(self, make_event, secret_key):
        """
        Mutating amount_property_currency after signing must cause
        verification to fail. This is the most important tamper-evidence
        guarantee — silent edits to amounts are the worst-case data
        integrity failure.
        """
        evt = make_event()
        evt.hmac_signature = sign_event(evt, secret_key)
        evt.amount_property_currency = Decimal("999999.00")
        assert verify_event(evt, secret_key) is False

    def test_verify_event_returns_false_if_date_tampered(self, make_event, secret_key):
        """
        Mutating effective_date after signing must cause verification to
        fail. Back-dating an event silently would let someone reorder
        history.
        """
        evt = make_event()
        evt.hmac_signature = sign_event(evt, secret_key)
        evt.effective_date = date(2020, 1, 1)
        assert verify_event(evt, secret_key) is False

    def test_verify_event_returns_false_if_actor_tampered(self, make_event, secret_key):
        """
        Mutating actor_owner_id after signing must cause verification to
        fail. Reattributing payments would shift balance impacts to the
        wrong owner.
        """
        evt = make_event()
        evt.hmac_signature = sign_event(evt, secret_key)
        evt.actor_owner_id = uuid.uuid4()
        assert verify_event(evt, secret_key) is False

    def test_verify_event_returns_false_when_signature_missing(self, make_event, secret_key):
        """
        An unsigned event must not verify. The `verify_event` function is
        the gatekeeper — it must refuse to validate rows that were never
        signed in the first place.
        """
        evt = make_event()  # hmac_signature defaults to None
        assert verify_event(evt, secret_key) is False

    def test_verify_event_returns_false_for_garbage_signature(self, make_event, secret_key):
        """
        A clearly-corrupt signature must verify to False, not raise. The
        contract is: any failure mode returns False so the caller can
        decide what to do (alert, quarantine, etc.). Raising would crash
        the audit pass.
        """
        evt = make_event()
        evt.hmac_signature = "not-a-real-signature"
        assert verify_event(evt, secret_key) is False

    def test_signing_normalizes_naive_recorded_at_to_utc(self, make_event, secret_key):
        """
        recorded_at is part of the canonical signature input. Naive
        datetimes must be coerced to UTC before signing — otherwise a
        naive timestamp could produce a different signature than the
        same instant expressed with explicit UTC, which would silently
        invalidate verification when the row is read back.
        """
        evt_aware = make_event()
        evt_aware.hmac_signature = sign_event(evt_aware, secret_key)
        # Mutate to a naive equivalent of the same instant.
        evt_aware.recorded_at = evt_aware.recorded_at.replace(tzinfo=None)
        # The naive form must verify against the originally-signed aware form.
        assert verify_event(evt_aware, secret_key) is True


# ---------------------------------------------------------------------------
# Compensating entries
# ---------------------------------------------------------------------------
class TestCompensatingEntry:
    def test_compensating_entry_links_to_original_event(self, make_event, secret_key):
        """
        A compensating entry must set reverses_event_id to the original's id.
        This is the link that lets the audit UI pair them up and lets balance
        replay correlate the negation with what it negates.
        """
        original = make_event()
        original.hmac_signature = sign_event(original, secret_key)
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Reverses the bad event",
            secret_key=secret_key,
        )
        assert comp.reverses_event_id == original.id

    def test_compensating_entry_negates_amounts(self, make_event, secret_key):
        """
        Every signed monetary field on the compensating entry must be the
        negation of the original. Replay sums original + compensating and
        must arrive at zero.
        """
        original = make_event()
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Negation",
            secret_key=secret_key,
        )
        assert comp.amount_property_currency == -original.amount_property_currency
        assert comp.amount_source_currency == -original.amount_source_currency
        assert comp.inr_landed == -original.inr_landed

    def test_compensating_entry_preserves_fx_rates(self, make_event, secret_key):
        """
        FX rates on the compensating entry must equal the original's. The
        correction happens in the same FX context as the original, not at
        today's rate — otherwise the negation would not be exact.
        """
        original = make_event()
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Negation",
            secret_key=secret_key,
        )
        assert comp.fx_rate_actual == original.fx_rate_actual
        assert comp.fx_rate_reference == original.fx_rate_reference
        # The fee is also preserved (it's a sunk cost — not negated).
        assert comp.fee_source_currency == original.fee_source_currency

    def test_compensating_entry_is_itself_signed(self, make_event, secret_key):
        """
        The compensating entry must be returned with a populated HMAC
        signature, ready for persistence. It is itself an event in the log
        and must be tamper-evident.
        """
        original = make_event()
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Negation",
            secret_key=secret_key,
        )
        assert comp.hmac_signature is not None
        assert verify_event(comp, secret_key) is True

    def test_compensating_entry_event_type_is_correct(self, make_event, secret_key):
        """
        The event_type on the compensating row must be COMPENSATING_ENTRY.
        This is what the audit UI keys off and what balance.py uses to
        identify reversals during replay.
        """
        original = make_event()
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Negation",
            secret_key=secret_key,
        )
        assert comp.event_type is EventType.COMPENSATING_ENTRY

    def test_compensating_entry_preserves_effective_date(self, make_event, secret_key):
        """
        The compensating entry's effective_date must equal the original's.
        The correction takes effect on the original business date, not on
        the day the correction was logged. Otherwise historical balance
        queries between then and now would not reflect the correction.
        """
        original = make_event()
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Negation",
            secret_key=secret_key,
        )
        assert comp.effective_date == original.effective_date


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------
class TestValidateEventFields:
    def test_contribution_event_requires_amount_and_fx_rate(self, make_event):
        """
        A CONTRIBUTION without amount_property_currency or fx_rate_actual
        must be flagged. Cross-currency contributions without the dual-rate
        stamp would skip the FX gain/loss reporting path.
        """
        evt = make_event(
            amount_property_currency=None,
            fx_rate_actual=None,
            amount_source_currency=None,
        )
        errors = validate_event_fields(evt)
        assert any("amount_property_currency" in e for e in errors)
        assert any("fx_rate_actual" in e for e in errors)
        assert any("amount_source_currency" in e for e in errors)

    def test_settlement_event_requires_target_owner(self, make_event):
        """
        A SETTLEMENT without a target_owner_id has no counterparty and
        cannot affect any inter-personal balance — it is structurally
        meaningless.
        """
        evt = make_event(
            event_type=EventType.SETTLEMENT,
            target_owner_id=None,
            description="Bad settlement",
        )
        errors = validate_event_fields(evt)
        assert any("target_owner_id" in e for e in errors)

    def test_settlement_target_must_differ_from_actor(self, make_event):
        """
        A SETTLEMENT where actor and target are the same owner is a no-op
        and almost certainly indicates user error. Reject it loudly.
        """
        actor_id = uuid.uuid4()
        evt = make_event(
            event_type=EventType.SETTLEMENT,
            actor_owner_id=actor_id,
            target_owner_id=actor_id,
        )
        errors = validate_event_fields(evt)
        assert any("must differ" in e for e in errors)

    def test_interpersonal_rate_change_requires_new_rate_in_metadata(self, make_event):
        """
        INTERPERSONAL_RATE_CHANGE without metadata.new_rate_pct is incomplete.
        The replay engine cannot apply forward-only interest accrual without
        knowing what the new rate is.
        """
        evt = make_event(
            event_type=EventType.INTERPERSONAL_RATE_CHANGE,
            target_owner_id=uuid.uuid4(),
            amount_property_currency=None,
            metadata={},  # missing new_rate_pct
        )
        errors = validate_event_fields(evt)
        assert any("new_rate_pct" in e for e in errors)

    def test_compensating_entry_requires_reverses_event_id(self, make_event):
        """
        A COMPENSATING_ENTRY without reverses_event_id is unlinked — the
        audit UI cannot pair it with what it reverses, defeating the
        correction mechanism.
        """
        evt = make_event(
            event_type=EventType.COMPENSATING_ENTRY,
            amount_property_currency=Decimal("-100"),
            reverses_event_id=None,
        )
        errors = validate_event_fields(evt)
        assert any("reverses_event_id" in e for e in errors)

    def test_emi_payment_requires_loan_id(self, make_event):
        """
        EMI_PAYMENT without loan_id cannot be routed to a specific bank loan,
        making it indistinguishable from a generic CONTRIBUTION.
        """
        evt = make_event(event_type=EventType.EMI_PAYMENT, loan_id=None)
        errors = validate_event_fields(evt)
        assert any("loan_id" in e for e in errors)

    def test_valid_contribution_has_no_errors(self, make_event):
        """
        The default fixture event is a valid CONTRIBUTION. validate_event_fields
        must return an empty list — false positives on valid events would
        block legitimate writes.
        """
        evt = make_event()
        assert validate_event_fields(evt) == []

    def test_fx_snapshot_requires_pair_and_rate_in_metadata(self, make_event):
        """
        FX_SNAPSHOT carries its data in metadata; missing currency_pair or
        reference_rate makes the snapshot useless for FX reporting.
        """
        evt = make_event(
            event_type=EventType.FX_SNAPSHOT,
            amount_property_currency=None,
            metadata={},
        )
        errors = validate_event_fields(evt)
        assert any("currency_pair" in e for e in errors)
        assert any("reference_rate" in e for e in errors)

    def test_exit_requires_buyout_metadata(self, make_event):
        """
        EXIT without buyout_formula and buyout_amount in metadata is an
        incomplete record of the exit decision — the most important event
        in an owner's lifecycle in this system must carry the negotiated
        terms.
        """
        evt = make_event(
            event_type=EventType.EXIT,
            amount_property_currency=None,
            metadata={},
        )
        errors = validate_event_fields(evt)
        assert any("buyout_formula" in e for e in errors)
        assert any("buyout_amount" in e for e in errors)


# ---------------------------------------------------------------------------
# get_financial_effect — the routing contract for the balance projection
# ---------------------------------------------------------------------------
class TestFinancialEffect:
    def test_contribution_effect_credits_actor_capex(self, make_event):
        """
        A CONTRIBUTION event must credit the actor's CapEx contribution by
        inr_landed (the dual-rate rule). This is what the balance engine
        consumes to produce the per-owner contribution total.
        """
        evt = make_event()  # has inr_landed = 413925
        effect = get_financial_effect(evt)
        assert "owner_capex" in effect
        assert effect["owner_capex"]["owner_id"] == evt.actor_owner_id
        assert effect["owner_capex"]["delta"] == Decimal("413925.00")

    def test_interpersonal_disbursement_creates_debt(self, make_event):
        """
        INTERPERSONAL_LOAN_DISBURSEMENT must produce an interpersonal effect
        with positive delta from the lender (actor) toward the borrower
        (target). Negative delta would invert the debt direction.
        """
        evt = make_event(
            event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
            target_owner_id=uuid.uuid4(),
            amount_property_currency=Decimal("300000"),
            inr_landed=None,
        )
        effect = get_financial_effect(evt)
        assert effect["interpersonal"]["lender"] == evt.actor_owner_id
        assert effect["interpersonal"]["borrower"] == evt.target_owner_id
        assert effect["interpersonal"]["delta"] == Decimal("300000")

    def test_interpersonal_repayment_reduces_debt(self, make_event):
        """
        INTERPERSONAL_LOAN_REPAYMENT must reduce the lender↔borrower balance.
        The effect must still be framed as lender→borrower (so the projection
        engine has a single normal form), with a negative delta.
        """
        lender_id = uuid.uuid4()
        borrower_id = uuid.uuid4()
        evt = make_event(
            event_type=EventType.INTERPERSONAL_LOAN_REPAYMENT,
            actor_owner_id=borrower_id,
            target_owner_id=lender_id,
            amount_property_currency=Decimal("100000"),
            inr_landed=None,
        )
        effect = get_financial_effect(evt)
        assert effect["interpersonal"]["lender"] == lender_id
        assert effect["interpersonal"]["borrower"] == borrower_id
        assert effect["interpersonal"]["delta"] == Decimal("-100000")

    def test_settlement_reduces_interpersonal_balance(self, make_event):
        """
        SETTLEMENT (e.g., Zelle, dinner-paid-for) must reduce the payer's
        debt to the recipient. The framing is the same as a repayment —
        same direction in the projection.
        """
        payer_id = uuid.uuid4()
        recipient_id = uuid.uuid4()
        evt = make_event(
            event_type=EventType.SETTLEMENT,
            actor_owner_id=payer_id,
            target_owner_id=recipient_id,
            amount_property_currency=Decimal("5000"),
            inr_landed=None,
        )
        effect = get_financial_effect(evt)
        assert effect["interpersonal"]["lender"] == recipient_id
        assert effect["interpersonal"]["borrower"] == payer_id
        assert effect["interpersonal"]["delta"] == Decimal("-5000")

    def test_emi_payment_reduces_loan_principal(self, make_event):
        """
        EMI_PAYMENT must reduce the loan's principal by the gross amount
        and credit the payer's CapEx. (The principal/interest split is
        applied in metadata downstream; here we model the gross movement.)
        """
        loan_id = uuid.uuid4()
        evt = make_event(
            event_type=EventType.EMI_PAYMENT,
            loan_id=loan_id,
            amount_property_currency=Decimal("50000"),
            inr_landed=None,
        )
        effect = get_financial_effect(evt)
        assert effect["bank_loan"]["loan_id"] == loan_id
        assert effect["bank_loan"]["delta"] == Decimal("-50000")
        assert effect["owner_capex"]["delta"] == Decimal("50000")

    def test_compensating_entry_negates_parent_effect(self, make_event, secret_key):
        """
        The financial effect of a COMPENSATING_ENTRY must mirror its parent's
        routing with a negated amount. Replay sums all events; the parent
        and its compensating entry must arrive at net zero.
        """
        lender_id = uuid.uuid4()
        borrower_id = uuid.uuid4()
        original = make_event(
            event_type=EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
            actor_owner_id=lender_id,
            target_owner_id=borrower_id,
            amount_property_currency=Decimal("300000"),
            inr_landed=None,
        )
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Negation",
            secret_key=secret_key,
        )
        original_effect = get_financial_effect(original)
        comp_effect = get_financial_effect(comp)
        assert original_effect["interpersonal"]["delta"] == Decimal("300000")
        assert comp_effect["interpersonal"]["delta"] == Decimal("-300000")
        # Same lender/borrower framing.
        assert original_effect["interpersonal"]["lender"] == comp_effect["interpersonal"]["lender"]
        assert (
            original_effect["interpersonal"]["borrower"] == comp_effect["interpersonal"]["borrower"]
        )

    def test_fx_snapshot_has_no_financial_effect(self, make_event):
        """
        FX_SNAPSHOT is metadata, not a financial transaction. Its effect
        dict must be empty so the projection engine does not double-count
        or mis-attribute any balance change.
        """
        evt = make_event(
            event_type=EventType.FX_SNAPSHOT,
            amount_property_currency=None,
            inr_landed=None,
            metadata={"currency_pair": "USD_INR", "reference_rate": Decimal("83.45")},
        )
        effect = get_financial_effect(evt)
        assert effect == {}

    def test_rate_change_has_no_financial_effect(self, make_event):
        """
        INTERPERSONAL_RATE_CHANGE is forward-only and has no immediate
        principal effect. The projection engine accrues interest from this
        date forward; the change event itself does not move the balance.
        """
        evt = make_event(
            event_type=EventType.INTERPERSONAL_RATE_CHANGE,
            target_owner_id=uuid.uuid4(),
            amount_property_currency=None,
            inr_landed=None,
            metadata={"new_rate_pct": Decimal("3.0")},
        )
        effect = get_financial_effect(evt)
        assert effect == {}

    def test_opex_split_for_payer_has_no_interpersonal_effect(self, make_event):
        """
        An OPEX_SPLIT row where actor == target represents the payer's own
        share of the expense — the payer cannot owe themselves money. The
        interpersonal effect must be absent for that row, while the
        owner_opex contribution is still recorded.
        """
        owner_id = uuid.uuid4()
        evt = make_event(
            event_type=EventType.OPEX_SPLIT,
            actor_owner_id=owner_id,
            target_owner_id=owner_id,
            amount_property_currency=Decimal("40000"),
            inr_landed=None,
        )
        effect = get_financial_effect(evt)
        assert "interpersonal" not in effect
        assert effect["owner_opex"]["owner_id"] == owner_id
        assert effect["owner_opex"]["delta"] == Decimal("40000")


# ---------------------------------------------------------------------------
# Compensating-entry routing — coverage for every parent event type.
# ---------------------------------------------------------------------------
# Each test pairs an original event with its compensating entry and asserts
# that summing both effect dicts yields zero on the affected balance(s).
# These tests guard against the four routing bugs found in PR review:
#   1. bank_loan delta sign for EMI_PAYMENT/BULK_PREPAYMENT compensations
#   2. inr_landed vs amount_property_currency drift on cross-currency
#      CONTRIBUTION compensations
#   3. lender/borrower inversion on REPAYMENT/SETTLEMENT compensations
#   4. lender/borrower inversion on OPEX_SPLIT compensations
class TestCompensatingEntryRouting:
    def test_emi_payment_compensation_cancels_bank_loan_delta(self, make_event, secret_key):
        """
        Compensating an EMI_PAYMENT must produce a bank_loan delta that
        exactly cancels the original. The original reduces principal
        (negative delta); the compensating entry must INCREASE principal
        (positive delta). Without this, a corrected EMI double-reduces the
        loan balance and the projection diverges silently.
        """
        loan_id = uuid.uuid4()
        original = make_event(
            event_type=EventType.EMI_PAYMENT,
            loan_id=loan_id,
            amount_property_currency=Decimal("50000"),
            inr_landed=None,
        )
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="EMI was double-recorded",
            secret_key=secret_key,
        )
        original_effect = get_financial_effect(original)
        comp_effect = get_financial_effect(comp)
        # Bank loan deltas must sum to zero.
        assert original_effect["bank_loan"]["delta"] == Decimal("-50000")
        assert comp_effect["bank_loan"]["delta"] == Decimal("50000")
        assert original_effect["bank_loan"]["delta"] + comp_effect["bank_loan"]["delta"] == Decimal(
            "0"
        )
        # CapEx deltas must sum to zero too.
        assert original_effect["owner_capex"]["delta"] + comp_effect["owner_capex"][
            "delta"
        ] == Decimal("0")
        # Same loan_id and same owner_id on both sides.
        assert comp_effect["bank_loan"]["loan_id"] == loan_id
        assert comp_effect["owner_capex"]["owner_id"] == original.actor_owner_id

    def test_bulk_prepayment_compensation_cancels_bank_loan_delta(self, make_event, secret_key):
        """
        Same contract as EMI_PAYMENT: a compensating BULK_PREPAYMENT must
        cancel the original's loan-balance reduction.
        """
        loan_id = uuid.uuid4()
        original = make_event(
            event_type=EventType.BULK_PREPAYMENT,
            loan_id=loan_id,
            amount_property_currency=Decimal("250000"),
            inr_landed=None,
        )
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Prepayment was duplicated",
            secret_key=secret_key,
        )
        original_effect = get_financial_effect(original)
        comp_effect = get_financial_effect(comp)
        assert original_effect["bank_loan"]["delta"] + comp_effect["bank_loan"]["delta"] == Decimal(
            "0"
        )

    def test_cross_currency_contribution_compensation_cancels_inr_landed_credit(
        self, make_event, secret_key
    ):
        """
        Cross-currency CONTRIBUTION events credit owner_capex by inr_landed
        (the dual-rate rule), NOT by amount_property_currency. Their
        compensating entries must follow the same rule — otherwise the wire
        fee delta (amount_property_currency - inr_landed) survives the
        correction and accumulates over the 20-year ledger lifetime.
        """
        # Use the fixture defaults: amount_property_currency=415000,
        # inr_landed=413925. The two differ; a buggy implementation would
        # leave a -1075 residue.
        original = make_event()
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Wire amount entered wrong",
            secret_key=secret_key,
        )
        original_effect = get_financial_effect(original)
        comp_effect = get_financial_effect(comp)
        # The capex deltas must sum to exactly zero — no residue.
        assert original_effect["owner_capex"]["delta"] == Decimal("413925.00")
        assert comp_effect["owner_capex"]["delta"] == Decimal("-413925.00")
        assert original_effect["owner_capex"]["delta"] + comp_effect["owner_capex"][
            "delta"
        ] == Decimal("0")

    def test_repayment_compensation_preserves_pair_framing(self, make_event, secret_key):
        """
        A compensating INTERPERSONAL_LOAN_REPAYMENT must net-zero on the
        SAME (lender, borrower) pair as the original. The previous bug:
        the compensating entry preserved actor=borrower/target=lender from
        the original, but then routed actor as lender — silently flipping
        the pair direction and leaving the original repayment effect
        un-canceled.
        """
        lender_id = uuid.uuid4()
        borrower_id = uuid.uuid4()
        original = make_event(
            event_type=EventType.INTERPERSONAL_LOAN_REPAYMENT,
            actor_owner_id=borrower_id,
            target_owner_id=lender_id,
            amount_property_currency=Decimal("100000"),
            inr_landed=None,
        )
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Repayment was double-counted",
            secret_key=secret_key,
        )
        original_effect = get_financial_effect(original)
        comp_effect = get_financial_effect(comp)
        # Same pair framing on both sides.
        assert original_effect["interpersonal"]["lender"] == lender_id
        assert original_effect["interpersonal"]["borrower"] == borrower_id
        assert comp_effect["interpersonal"]["lender"] == lender_id
        assert comp_effect["interpersonal"]["borrower"] == borrower_id
        # Net-zero on the pair.
        assert original_effect["interpersonal"]["delta"] + comp_effect["interpersonal"][
            "delta"
        ] == Decimal("0")

    def test_settlement_compensation_preserves_pair_framing(self, make_event, secret_key):
        """
        A compensating SETTLEMENT must net-zero on the same (recipient,
        payer) pair as the original. Same class of bug as REPAYMENT —
        actor=payer/target=recipient is preserved on the compensating
        entry, but the original's routing makes recipient the lender and
        payer the borrower.
        """
        payer_id = uuid.uuid4()
        recipient_id = uuid.uuid4()
        original = make_event(
            event_type=EventType.SETTLEMENT,
            actor_owner_id=payer_id,
            target_owner_id=recipient_id,
            amount_property_currency=Decimal("5000"),
            inr_landed=None,
        )
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Settlement was duplicated",
            secret_key=secret_key,
        )
        original_effect = get_financial_effect(original)
        comp_effect = get_financial_effect(comp)
        assert original_effect["interpersonal"]["lender"] == recipient_id
        assert original_effect["interpersonal"]["borrower"] == payer_id
        assert comp_effect["interpersonal"]["lender"] == recipient_id
        assert comp_effect["interpersonal"]["borrower"] == payer_id
        assert original_effect["interpersonal"]["delta"] + comp_effect["interpersonal"][
            "delta"
        ] == Decimal("0")

    def test_opex_split_compensation_preserves_pair_framing(self, make_event, secret_key):
        """
        A compensating OPEX_SPLIT must net-zero on the same (payer,
        share-owner) pair as the original. The original makes payer=lender
        and share-owner=borrower; the compensating entry must keep that
        framing.
        """
        payer_id = uuid.uuid4()
        share_owner_id = uuid.uuid4()
        original = make_event(
            event_type=EventType.OPEX_SPLIT,
            actor_owner_id=share_owner_id,
            target_owner_id=payer_id,
            amount_property_currency=Decimal("40000"),
            inr_landed=None,
        )
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="OpEx split row was wrong",
            secret_key=secret_key,
        )
        original_effect = get_financial_effect(original)
        comp_effect = get_financial_effect(comp)
        assert original_effect["interpersonal"]["lender"] == payer_id
        assert original_effect["interpersonal"]["borrower"] == share_owner_id
        assert comp_effect["interpersonal"]["lender"] == payer_id
        assert comp_effect["interpersonal"]["borrower"] == share_owner_id
        assert original_effect["interpersonal"]["delta"] + comp_effect["interpersonal"][
            "delta"
        ] == Decimal("0")
        # owner_opex must also net to zero.
        assert original_effect["owner_opex"]["delta"] + comp_effect["owner_opex"][
            "delta"
        ] == Decimal("0")

    def test_compensating_entry_with_missing_metadata_returns_empty_effect(self, make_event):
        """
        A COMPENSATING_ENTRY without `original_event_type` in metadata
        cannot be routed. The function must return an empty effect dict
        rather than guessing — the caller (audit UI / projection engine)
        flags such rows for human review.
        """
        evt = make_event(
            event_type=EventType.COMPENSATING_ENTRY,
            amount_property_currency=Decimal("-100"),
            reverses_event_id=uuid.uuid4(),
            metadata={"reverses_original_event": "some-uuid"},  # missing original_event_type
        )
        assert get_financial_effect(evt) == {}


# ---------------------------------------------------------------------------
# Validation edge cases — same-currency CONTRIBUTION and OPEX_SPLIT actor==target
# ---------------------------------------------------------------------------
class TestValidateEdgeCases:
    def test_same_currency_contribution_does_not_require_fx_rate(self, make_event):
        """
        A CONTRIBUTION where source_currency == property_currency is a
        same-currency contribution (e.g., an INR-based owner contributing
        directly to an INR-denominated property). It has no FX conversion
        and therefore no fx_rate_actual or amount_source_currency. The
        validator must accept it — otherwise the API would reject every
        valid same-currency contribution at the boundary.
        """
        evt = make_event(
            source_currency="INR",
            property_currency="INR",
            fx_rate_actual=None,
            fx_rate_reference=None,
            amount_source_currency=None,
            fee_source_currency=None,
            inr_landed=None,
            amount_property_currency=Decimal("100000"),
        )
        assert validate_event_fields(evt) == []

    def test_cross_currency_contribution_still_requires_fx_rate(self, make_event):
        """
        Cross-currency CONTRIBUTIONs continue to require fx_rate_actual.
        The conditional fix must not weaken the cross-currency contract.
        """
        evt = make_event(
            source_currency="USD",
            property_currency="INR",
            fx_rate_actual=None,
            amount_source_currency=None,
            inr_landed=None,
            amount_property_currency=Decimal("100000"),
        )
        errors = validate_event_fields(evt)
        assert any("fx_rate_actual" in e for e in errors)
        assert any("amount_source_currency" in e for e in errors)

    def test_opex_split_payer_own_share_validates_ok(self, make_event):
        """
        An OPEX_SPLIT row where actor == target represents the payer's own
        share — explicitly documented as valid in event-log.md. The
        validator must accept it; rejecting it would block recording a
        complete set of OpEx splits.
        """
        owner_id = uuid.uuid4()
        evt = make_event(
            event_type=EventType.OPEX_SPLIT,
            actor_owner_id=owner_id,
            target_owner_id=owner_id,
            amount_property_currency=Decimal("40000"),
        )
        assert validate_event_fields(evt) == []

    def test_settlement_actor_target_must_still_differ(self, make_event):
        """
        Loosening the actor!=target check for OPEX_SPLIT must NOT weaken it
        for SETTLEMENT (or REPAYMENT, DISBURSEMENT, RATE_CHANGE). A
        settlement with actor==target is still a no-op and a likely user
        error.
        """
        actor_id = uuid.uuid4()
        evt = make_event(
            event_type=EventType.SETTLEMENT,
            actor_owner_id=actor_id,
            target_owner_id=actor_id,
            amount_property_currency=Decimal("5000"),
        )
        errors = validate_event_fields(evt)
        assert any("must differ" in e for e in errors)

    def test_equity_adjustment_requires_new_equity_pct(self, make_event):
        """
        An EQUITY_ADJUSTMENT without metadata.new_equity_pct cannot record
        the new equity stake — the entire point of the event. Validation
        must reject it so it never reaches the database.
        """
        evt = make_event(
            event_type=EventType.EQUITY_ADJUSTMENT,
            amount_property_currency=None,
            metadata={},
        )
        errors = validate_event_fields(evt)
        assert any("new_equity_pct" in e for e in errors)

    def test_interpersonal_rate_change_missing_target_owner(self, make_event):
        """
        INTERPERSONAL_RATE_CHANGE must have a target_owner_id (the borrower
        whose pair the rate change applies to). Without it the rate change
        is unrouteable — we wouldn't know which pair's accrual schedule to
        update.
        """
        evt = make_event(
            event_type=EventType.INTERPERSONAL_RATE_CHANGE,
            target_owner_id=None,
            amount_property_currency=None,
            inr_landed=None,
            metadata={"new_rate_pct": Decimal("3.0")},
        )
        errors = validate_event_fields(evt)
        assert any("target_owner_id" in e for e in errors)


# ---------------------------------------------------------------------------
# LedgerEvent.check_is_signed — defense-in-depth pre-persist check
# ---------------------------------------------------------------------------
class TestCheckIsSigned:
    def test_check_is_signed_raises_when_unsigned(self, make_event):
        """
        check_is_signed must raise on an event that has not been signed.
        Persisting an unsigned event would silently bypass the
        tamper-evidence layer — this guard catches the mistake at the
        persistence boundary.
        """
        import pytest as _pytest

        evt = make_event()  # hmac_signature defaults to None
        with _pytest.raises(ValueError, match="must be signed"):
            evt.check_is_signed()

    def test_check_is_signed_passes_when_signed(self, make_event, secret_key):
        """
        check_is_signed must NOT raise on a properly signed event. False
        positives here would block legitimate writes.
        """
        evt = make_event()
        evt.hmac_signature = sign_event(evt, secret_key)
        evt.check_is_signed()  # must not raise


# ---------------------------------------------------------------------------
# build_compensating_entry — None-amount handling and edge cases
# ---------------------------------------------------------------------------
class TestBuildCompensatingEntryEdges:
    def test_compensates_event_with_no_monetary_amounts(self, make_event, secret_key):
        """
        Some event types legitimately have None amount fields (e.g., a bad
        INTERPERSONAL_RATE_CHANGE that needs reversing). build_compensating_entry
        must handle None gracefully — passing None through rather than
        attempting to negate it (which would raise).
        """
        original = make_event(
            event_type=EventType.INTERPERSONAL_RATE_CHANGE,
            target_owner_id=uuid.uuid4(),
            amount_source_currency=None,
            amount_property_currency=None,
            inr_landed=None,
            metadata={"new_rate_pct": Decimal("5.0")},
        )
        comp = build_compensating_entry(
            original=original,
            actor_email="fixer@example.com",
            description="Reverse the rate change",
            secret_key=secret_key,
        )
        assert comp.amount_source_currency is None
        assert comp.amount_property_currency is None
        assert comp.inr_landed is None
        assert comp.reverses_event_id == original.id

    def test_compensating_entry_with_invalid_parent_type_returns_empty_effect(self, make_event):
        """
        A COMPENSATING_ENTRY whose metadata.original_event_type is not a
        valid EventType (e.g., schema corruption, hand-edited row) must
        return an empty effect dict rather than raising. The caller flags
        the row for review.
        """
        evt = make_event(
            event_type=EventType.COMPENSATING_ENTRY,
            amount_property_currency=Decimal("-100"),
            reverses_event_id=uuid.uuid4(),
            metadata={
                "reverses_original_event": "some-uuid",
                "original_event_type": "NOT_A_REAL_TYPE",
            },
        )
        assert get_financial_effect(evt) == {}

    def test_compensating_entry_with_compensating_parent_returns_empty(self, make_event):
        """
        A COMPENSATING_ENTRY claiming another COMPENSATING_ENTRY as its
        parent is nonsensical (you don't compensate a compensation —
        you'd write a fresh corrective event). The recursion guard returns
        empty so the projection engine doesn't loop or produce a
        spurious effect.
        """
        evt = make_event(
            event_type=EventType.COMPENSATING_ENTRY,
            amount_property_currency=Decimal("-100"),
            reverses_event_id=uuid.uuid4(),
            metadata={
                "reverses_original_event": "some-uuid",
                "original_event_type": EventType.COMPENSATING_ENTRY.value,
            },
        )
        assert get_financial_effect(evt) == {}

    def test_compensating_entry_with_no_op_parent_returns_empty(self, make_event):
        """
        A COMPENSATING_ENTRY whose original was a no-op type (FX_SNAPSHOT,
        EQUITY_ADJUSTMENT, etc.) has nothing to negate financially. The
        guard returns empty so we don't accidentally produce ghost balance
        effects from a metadata-only correction.
        """
        evt = make_event(
            event_type=EventType.COMPENSATING_ENTRY,
            amount_property_currency=None,
            reverses_event_id=uuid.uuid4(),
            metadata={
                "reverses_original_event": "some-uuid",
                "original_event_type": EventType.FX_SNAPSHOT.value,
            },
        )
        assert get_financial_effect(evt) == {}
