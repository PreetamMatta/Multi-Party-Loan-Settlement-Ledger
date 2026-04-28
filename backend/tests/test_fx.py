"""
Tests for backend/core/fx.py — the dual-rate FX stamping math.

These tests pin the contract documented in
docs/business-logic/fx-and-wire-transfers.md. They use Decimal throughout —
floats are forbidden in financial math, and these tests assert that.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.fx import stamp_fx_event


class TestStampFXEvent:
    def test_stamp_fx_event_calculates_inr_landed_correctly(self):
        """
        inr_landed must equal (amount_source - fee_source) * fx_rate_actual.
        The fee is taken out of the source amount BEFORE conversion — the
        recipient's account only sees the post-fee converted amount.
        """
        stamp = stamp_fx_event(
            amount_source=Decimal("5000"),
            fee_source=Decimal("25"),
            rate_actual=Decimal("83.20"),
            rate_reference=Decimal("83.45"),
        )
        # (5000 - 25) * 83.20 = 4975 * 83.20 = 413,920
        assert stamp.inr_landed == Decimal("413920.00")

    def test_stamp_fx_event_reference_equivalent_uses_gross(self):
        """
        inr_reference_equivalent must use the GROSS amount_source, not
        net of fee. Otherwise the FX gain/loss number would hide the fee.
        The delta between landed and reference equivalent is the sender's
        total cost (spread + fees).
        """
        stamp = stamp_fx_event(
            amount_source=Decimal("5000"),
            fee_source=Decimal("25"),
            rate_actual=Decimal("83.20"),
            rate_reference=Decimal("83.45"),
        )
        # 5000 * 83.45 = 417,250
        assert stamp.inr_reference_equivalent == Decimal("417250.00")

    def test_stamp_fx_event_calculates_fx_gain_loss(self):
        """
        fx_gain_loss_inr is inr_landed - inr_reference_equivalent. Negative
        means the bank's spread+fees cost the sender money vs mid-market;
        positive (rare) means the bank gave a better deal than mid-market.
        """
        stamp = stamp_fx_event(
            amount_source=Decimal("5000"),
            fee_source=Decimal("25"),
            rate_actual=Decimal("83.20"),
            rate_reference=Decimal("83.45"),
        )
        # 413,920 - 417,250 = -3,330
        assert stamp.fx_gain_loss_inr == Decimal("-3330.00")

    def test_stamp_fx_event_positive_gain_when_actual_better_than_reference(self):
        """
        On the rare occasion the bank gives a better rate than mid-market
        (and the fee is small enough), fx_gain_loss_inr must be positive.
        """
        # actual rate higher than reference => more INR per USD => sender gains
        stamp = stamp_fx_event(
            amount_source=Decimal("1000"),
            fee_source=Decimal("0"),
            rate_actual=Decimal("84.00"),
            rate_reference=Decimal("83.50"),
        )
        # landed = 1000 * 84.00 = 84,000
        # reference = 1000 * 83.50 = 83,500
        # gain = +500
        assert stamp.fx_gain_loss_inr == Decimal("500.00")

    def test_stamp_fx_event_zero_fee_case(self):
        """
        With zero fee, inr_landed is amount_source * rate_actual. This is
        the straight-conversion case — useful as a sanity check that the
        fee-handling path doesn't introduce drift on the no-fee path.
        """
        stamp = stamp_fx_event(
            amount_source=Decimal("1000"),
            fee_source=Decimal("0"),
            rate_actual=Decimal("83.00"),
            rate_reference=Decimal("83.10"),
        )
        assert stamp.inr_landed == Decimal("83000.00")
        assert stamp.fx_gain_loss_inr == Decimal("-100.00")

    def test_stamp_fx_event_uses_decimal_not_float(self):
        """
        Every numeric field on the returned FXStamp must be a Decimal.
        Floats in financial code introduce silent precision drift over
        time — this test pins that contract. If anything in the chain
        returns a float, this test fails.
        """
        stamp = stamp_fx_event(
            amount_source=Decimal("5000"),
            fee_source=Decimal("25"),
            rate_actual=Decimal("83.20"),
            rate_reference=Decimal("83.45"),
        )
        assert isinstance(stamp.amount_source_currency, Decimal)
        assert isinstance(stamp.fee_source_currency, Decimal)
        assert isinstance(stamp.fx_rate_actual, Decimal)
        assert isinstance(stamp.fx_rate_reference, Decimal)
        assert isinstance(stamp.inr_landed, Decimal)
        assert isinstance(stamp.inr_reference_equivalent, Decimal)
        assert isinstance(stamp.fx_gain_loss_inr, Decimal)

    def test_stamp_fx_event_large_wire_precision(self):
        """
        A large wire ($50,000) must compute exactly with no float drift.
        The Decimal arithmetic must produce a result that is stable to the
        last paisa — at scale, even tiny per-event drift accumulates into
        reconciliation failures.
        """
        stamp = stamp_fx_event(
            amount_source=Decimal("50000.00"),
            fee_source=Decimal("45.00"),
            rate_actual=Decimal("83.123456"),
            rate_reference=Decimal("83.456789"),
        )
        # (50000 - 45) * 83.123456 = 49955 * 83.123456 = 4,152,481.24...
        # Compute exactly and pin it:
        expected_landed = Decimal("49955") * Decimal("83.123456")
        expected_ref = Decimal("50000") * Decimal("83.456789")
        assert stamp.inr_landed == expected_landed
        assert stamp.inr_reference_equivalent == expected_ref
        assert stamp.fx_gain_loss_inr == expected_landed - expected_ref

    def test_inr_landed_is_credit_amount_not_full_usd(self):
        """
        Critical accounting rule: the credit to the sender's balance must
        equal inr_landed — NOT amount_source * fx_rate_actual. Crediting
        the gross amount would over-credit the sender by the fee, which
        propagates forever through balance computations.
        """
        amount_source = Decimal("5000")
        fee = Decimal("25")
        rate_actual = Decimal("83.20")
        stamp = stamp_fx_event(
            amount_source=amount_source,
            fee_source=fee,
            rate_actual=rate_actual,
            rate_reference=Decimal("83.45"),
        )
        gross_credit_wrong = amount_source * rate_actual            # 416,000
        net_credit_correct = (amount_source - fee) * rate_actual    # 413,920
        assert stamp.inr_landed != gross_credit_wrong
        assert stamp.inr_landed == net_credit_correct

    def test_stamp_fx_event_returns_frozen_dataclass(self):
        """
        FXStamp is a frozen dataclass — once created, fields cannot be
        mutated. This guards against later code accidentally writing back
        to a stamp that has already been used to populate an event row.
        """
        stamp = stamp_fx_event(
            amount_source=Decimal("100"),
            fee_source=Decimal("0"),
            rate_actual=Decimal("83.00"),
            rate_reference=Decimal("83.00"),
        )
        with pytest.raises((AttributeError, Exception)):
            stamp.inr_landed = Decimal("999")  # type: ignore[misc]
