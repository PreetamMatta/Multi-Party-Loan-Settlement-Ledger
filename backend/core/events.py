"""
events.py — Event log model, HMAC signing, and compensating transaction helpers.

Every write to the ledger must go through this module.

The HMAC signature guarantees tamper-evidence: if any canonical field is altered
after signing, verification will fail. The canonical field order is FROZEN —
future event types must use the same order, or the entire historical event log
will fail re-verification on a key rotation.

See docs/business-logic/event-log.md for the full rationale and worked examples
for each event type. That document is the authoritative source for what every
event type means; this module is its implementation. If the two diverge, the
document is right and this module needs fixing.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# -----------------------------------------------------------------------------
# Event types — must match the documented enum in backend/db/schema.sql
# -----------------------------------------------------------------------------
class EventType(StrEnum):
    CONTRIBUTION = "CONTRIBUTION"
    EMI_PAYMENT = "EMI_PAYMENT"
    BULK_PREPAYMENT = "BULK_PREPAYMENT"
    INTERPERSONAL_LOAN_DISBURSEMENT = "INTERPERSONAL_LOAN_DISBURSEMENT"
    INTERPERSONAL_LOAN_REPAYMENT = "INTERPERSONAL_LOAN_REPAYMENT"
    INTERPERSONAL_RATE_CHANGE = "INTERPERSONAL_RATE_CHANGE"
    SETTLEMENT = "SETTLEMENT"
    OPEX_EXPENSE = "OPEX_EXPENSE"
    OPEX_SPLIT = "OPEX_SPLIT"
    FX_SNAPSHOT = "FX_SNAPSHOT"
    EQUITY_ADJUSTMENT = "EQUITY_ADJUSTMENT"
    EXIT = "EXIT"
    COMPENSATING_ENTRY = "COMPENSATING_ENTRY"


# -----------------------------------------------------------------------------
# LedgerEvent — Pydantic model matching the events table columns exactly.
# -----------------------------------------------------------------------------
class LedgerEvent(BaseModel):
    """
    One row in the append-only events table. Field names and nullability
    mirror backend/db/schema.sql exactly.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    property_id: uuid.UUID
    event_type: EventType
    actor_owner_id: uuid.UUID
    target_owner_id: uuid.UUID | None = None
    loan_id: uuid.UUID | None = None

    amount_source_currency: Decimal | None = None
    source_currency: str | None = None
    amount_property_currency: Decimal | None = None
    property_currency: str | None = None
    fx_rate_actual: Decimal | None = None
    fx_rate_reference: Decimal | None = None
    fee_source_currency: Decimal | None = None
    inr_landed: Decimal | None = None

    description: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    reverses_event_id: uuid.UUID | None = None

    hmac_signature: str | None = None
    recorded_by: str
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    effective_date: date

    def check_is_signed(self) -> None:
        """
        Defense-in-depth: call this immediately before persisting to ensure
        the event has been signed.
        """
        if not self.hmac_signature:
            raise ValueError("LedgerEvent must be signed before persistence.")


# -----------------------------------------------------------------------------
# HMAC signing — canonical field order is FROZEN.
# -----------------------------------------------------------------------------
def _canonical_string(event: LedgerEvent) -> str:
    """
    Build the canonical string used as HMAC input.

    Order (FROZEN — never reorder, never add fields, never change separator):
        {id}|{event_type}|{actor_owner_id}|{amount_property_currency}|{effective_date}|{recorded_at}

    NULL `amount_property_currency` is rendered as the empty string. `recorded_at`
    is rendered in ISO 8601 with explicit timezone. Reordering or normalizing
    these will invalidate every previously signed row in the log.
    """
    amount_str = (
        str(event.amount_property_currency) if event.amount_property_currency is not None else ""
    )
    # Render recorded_at in a stable, timezone-aware ISO format.
    recorded_at = event.recorded_at
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=UTC)
    recorded_at_str = recorded_at.isoformat()

    return (
        f"{event.id}|{event.event_type.value}|{event.actor_owner_id}"
        f"|{amount_str}|{event.effective_date.isoformat()}|{recorded_at_str}"
    )


def sign_event(event: LedgerEvent, secret_key: str) -> str:
    """
    Compute the HMAC-SHA256 signature of an event.

    The caller is expected to assign the returned hex digest to
    `event.hmac_signature` before persisting the row.
    """
    canonical = _canonical_string(event)
    return hmac.new(
        secret_key.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_event(event: LedgerEvent, secret_key: str) -> bool:
    """
    Recompute the signature for `event` and compare to `event.hmac_signature`.

    Returns False on mismatch (or when the event has no signature) — does NOT
    raise. Callers decide what to do with an invalid signature: log it,
    quarantine the row, alert a human, etc.
    """
    if not event.hmac_signature:
        return False
    expected = sign_event(event, secret_key)
    return hmac.compare_digest(expected, event.hmac_signature)


# -----------------------------------------------------------------------------
# Compensating entries — corrections without mutating history.
# -----------------------------------------------------------------------------
def build_compensating_entry(
    original: LedgerEvent,
    actor_email: str,
    description: str,
    secret_key: str,
) -> LedgerEvent:
    """
    Build a signed COMPENSATING_ENTRY event that financially negates `original`.

    The original event is NOT deleted or modified — both rows live in the log
    forever. The compensating entry's `amount_property_currency` is the
    negation of the original's amount; replaying the log with both rows
    yields a net-zero impact for the erroneous entry.

    The new event:
      - has a fresh id
      - has event_type = COMPENSATING_ENTRY
      - sets reverses_event_id = original.id
      - inherits property_id, actor (from `actor_email` lookup is the caller's
        responsibility — here we keep the original's actor unless overridden)
      - signs itself before being returned

    Note: this function intentionally does NOT touch the database. It only
    returns a fully populated, pre-signed `LedgerEvent`. Persisting the row
    is the caller's responsibility (Session 4 endpoints).
    """
    if original.amount_property_currency is None:
        amount_negated: Decimal | None = None
    else:
        amount_negated = -original.amount_property_currency

    if original.amount_source_currency is None:
        source_amount_negated: Decimal | None = None
    else:
        source_amount_negated = -original.amount_source_currency

    if original.inr_landed is None:
        inr_landed_negated: Decimal | None = None
    else:
        inr_landed_negated = -original.inr_landed

    compensating = LedgerEvent(
        property_id=original.property_id,
        event_type=EventType.COMPENSATING_ENTRY,
        actor_owner_id=original.actor_owner_id,
        target_owner_id=original.target_owner_id,
        loan_id=original.loan_id,
        amount_source_currency=source_amount_negated,
        source_currency=original.source_currency,
        amount_property_currency=amount_negated,
        property_currency=original.property_currency,
        fx_rate_actual=original.fx_rate_actual,
        fx_rate_reference=original.fx_rate_reference,
        fee_source_currency=original.fee_source_currency,
        inr_landed=inr_landed_negated,
        description=description,
        metadata={
            "reverses_original_event": str(original.id),
            # Stored so get_financial_effect can dispatch through the parent's
            # routing path: the compensating entry's amounts/inr_landed are
            # already negated, so running them through the parent's logic
            # produces a clean negation in the parent's framing (lender/borrower,
            # bank_loan sign, etc.).
            "original_event_type": original.event_type.value,
        },
        reverses_event_id=original.id,
        recorded_by=actor_email,
        effective_date=original.effective_date,
    )

    compensating.hmac_signature = sign_event(compensating, secret_key)
    return compensating


# -----------------------------------------------------------------------------
# Field validation — per-event-type required-field checks.
# -----------------------------------------------------------------------------
# These checks enforce the field-population contract documented in
# docs/business-logic/event-log.md. They are intended to run at the API boundary
# before persistence, to catch malformed events before they are signed and
# written. Returning a list (vs raising) lets the API surface all problems in
# one response rather than play whack-a-mole.
def validate_event_fields(event: LedgerEvent) -> list[str]:
    """
    Validate that the event's populated fields match the contract for its
    `event_type`. Returns a list of human-readable error strings; empty list
    means the event is valid for persistence.

    This is a structural check, not an arithmetic one. It does NOT verify
    that amounts are non-zero, that FX rates are in a sensible range, or
    that the actor and target are both real owners — those are the API
    layer's job. It only checks "for an event of this type, are the
    fields populated as documented?".
    """
    errors: list[str] = []
    et = event.event_type

    # All financial-money events need amount_property_currency to drive
    # balance math. The non-financial events are FX_SNAPSHOT,
    # INTERPERSONAL_RATE_CHANGE, EQUITY_ADJUSTMENT, and EXIT.
    needs_amount = {
        EventType.CONTRIBUTION,
        EventType.EMI_PAYMENT,
        EventType.BULK_PREPAYMENT,
        EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        EventType.INTERPERSONAL_LOAN_REPAYMENT,
        EventType.SETTLEMENT,
        EventType.OPEX_EXPENSE,
        EventType.OPEX_SPLIT,
    }
    if et in needs_amount and event.amount_property_currency is None:
        errors.append(f"{et.value}: amount_property_currency is required.")

    if et is EventType.COMPENSATING_ENTRY:
        parent_type_str = event.metadata.get("original_event_type")
        if parent_type_str:
            try:
                parent_type = EventType(parent_type_str)
                if parent_type in needs_amount and event.amount_property_currency is None:
                    errors.append(f"{et.value}: amount_property_currency is required.")
            except ValueError:
                pass

    # Cross-currency CONTRIBUTION events require the dual-rate stamp.
    # Same-currency contributions (e.g., an INR-based owner contributing to
    # an INR-denominated property) have no FX conversion and therefore no
    # fx_rate_actual or amount_source_currency. We only require those fields
    # when source_currency != property_currency. See docs/business-logic/
    # event-log.md for the CONTRIBUTION contract.
    if et is EventType.CONTRIBUTION:
        is_cross_currency = (
            event.source_currency is not None
            and event.property_currency is not None
            and event.source_currency != event.property_currency
        )
        if is_cross_currency:
            if event.fx_rate_actual is None:
                errors.append(
                    "CONTRIBUTION: fx_rate_actual is required for cross-currency stamping."
                )
            if event.amount_source_currency is None:
                errors.append("CONTRIBUTION: amount_source_currency is required.")

    # Inter-personal events require a target (the counterparty).
    pair_required = {
        EventType.INTERPERSONAL_LOAN_DISBURSEMENT,
        EventType.INTERPERSONAL_LOAN_REPAYMENT,
        EventType.INTERPERSONAL_RATE_CHANGE,
        EventType.SETTLEMENT,
        EventType.OPEX_SPLIT,
    }
    if et in pair_required and event.target_owner_id is None:
        errors.append(f"{et.value}: target_owner_id is required (counterparty).")

    # actor != target check — but NOT for OPEX_SPLIT. The payer's own share
    # of an OpEx expense is recorded as a split row where actor == target
    # (the payer's share owed to themselves — a no-op for inter-personal
    # balance, but still required so the per-event opex contribution total
    # is complete). See docs/business-logic/event-log.md, OPEX_SPLIT section.
    distinct_pair_required = pair_required - {EventType.OPEX_SPLIT}
    if et in distinct_pair_required and event.target_owner_id == event.actor_owner_id:
        errors.append(f"{et.value}: target_owner_id must differ from actor_owner_id.")

    # Bank-loan-linked events need a loan_id.
    needs_loan = {EventType.EMI_PAYMENT, EventType.BULK_PREPAYMENT}
    if et in needs_loan and event.loan_id is None:
        errors.append(f"{et.value}: loan_id is required.")

    # Rate change must carry the new rate in metadata.
    if et is EventType.INTERPERSONAL_RATE_CHANGE:
        if "new_rate_pct" not in event.metadata:
            errors.append("INTERPERSONAL_RATE_CHANGE: metadata.new_rate_pct is required.")

    # Compensating entry must reference the original event.
    if et is EventType.COMPENSATING_ENTRY and event.reverses_event_id is None:
        errors.append("COMPENSATING_ENTRY: reverses_event_id is required.")

    # FX_SNAPSHOT is non-financial and carries its data in metadata.
    if et is EventType.FX_SNAPSHOT:
        if "currency_pair" not in event.metadata:
            errors.append("FX_SNAPSHOT: metadata.currency_pair is required.")
        if "reference_rate" not in event.metadata:
            errors.append("FX_SNAPSHOT: metadata.reference_rate is required.")

    # EQUITY_ADJUSTMENT must record the new equity_pct.
    if et is EventType.EQUITY_ADJUSTMENT:
        if "new_equity_pct" not in event.metadata:
            errors.append("EQUITY_ADJUSTMENT: metadata.new_equity_pct is required.")

    # EXIT must record the buyout terms.
    if et is EventType.EXIT:
        if "buyout_formula" not in event.metadata:
            errors.append("EXIT: metadata.buyout_formula is required.")
        if "buyout_amount" not in event.metadata:
            errors.append("EXIT: metadata.buyout_amount is required.")

    return errors


# -----------------------------------------------------------------------------
# Financial effect routing — "what balances does this event move?"
# -----------------------------------------------------------------------------
# This function centralizes the routing logic so the balance projection engine
# (Session 3) does not need to re-derive it. The output is a structured dict
# that names exactly which balance(s) move and by how much.
#
# A return value with no keys (or all-zero deltas) means the event has no
# balance impact (e.g., FX_SNAPSHOT, INTERPERSONAL_RATE_CHANGE).
def get_financial_effect(event: LedgerEvent) -> dict[str, Any]:
    """
    Describe which balances this event moves. The balance projection engine
    consumes this dict to apply the right deltas without re-implementing
    per-event-type routing logic.

    Output schema (keys are present only when relevant):
        {
            "interpersonal": {
                "lender":   UUID,
                "borrower": UUID,
                "delta":    Decimal,   # positive = borrower owes lender more
            },
            "bank_loan": {
                "loan_id": UUID,
                "delta":   Decimal,    # negative = principal reduced
            },
            "owner_capex": {
                "owner_id": UUID,
                "delta":    Decimal,   # positive = capex contribution increase
            },
            "owner_opex": {
                "owner_id": UUID,
                "delta":    Decimal,   # positive = opex contribution increase
            },
        }

    The dict is the contract; balance.py and tests both depend on its shape.

    Compensating entries: dispatched through the parent event's routing path
    using `metadata.original_event_type`. The compensating entry's amount and
    inr_landed are already negated by `build_compensating_entry`, so running
    them through the parent's logic produces correctly negated deltas in the
    parent's framing — preserving lender/borrower order, the bank_loan sign,
    and the inr_landed-based credit for cross-currency contributions. If
    `original_event_type` is missing (e.g., a hand-built compensating entry
    that did not go through the helper), the function returns an empty dict
    and the caller is expected to flag the row.
    """
    # No-op event types — explicit so callers don't have to special-case.
    no_op_types = {
        EventType.FX_SNAPSHOT,
        EventType.INTERPERSONAL_RATE_CHANGE,
        EventType.EQUITY_ADJUSTMENT,
        EventType.EXIT,
        EventType.OPEX_EXPENSE,  # the gross expense; splits drive the actual deltas
    }
    if event.event_type in no_op_types:
        return {}

    if event.event_type is EventType.COMPENSATING_ENTRY:
        parent_type_str = event.metadata.get("original_event_type")
        if not parent_type_str:
            return {}
        try:
            parent_type = EventType(parent_type_str)
        except ValueError:
            return {}
        # Refuse to recurse if the metadata claims another COMPENSATING_ENTRY
        # as the parent — that's nonsensical and would loop.
        if parent_type is EventType.COMPENSATING_ENTRY or parent_type in no_op_types:
            return {}
        return _route_by_type(parent_type, event)

    return _route_by_type(event.event_type, event)


def _route_by_type(logical_type: EventType, event: LedgerEvent) -> dict[str, Any]:
    """
    Apply the routing for a given logical event type using the field values on
    `event`. Used directly for normal events, and used with `logical_type =
    parent_event_type` for COMPENSATING_ENTRY rows so the negated amounts on
    the compensating entry flow through the parent's framing.
    """
    effect: dict[str, Any] = {}
    amount = event.amount_property_currency

    if logical_type is EventType.CONTRIBUTION:
        # Credit the actor's CapEx with inr_landed (the dual-rate rule),
        # falling back to amount_property_currency when inr_landed is not
        # set (e.g., same-currency contributions). This same logic applies
        # when routing a compensating entry, where inr_landed is already
        # negated — yielding a clean cancellation of the original credit.
        credit = event.inr_landed if event.inr_landed is not None else amount
        if credit is not None:
            effect["owner_capex"] = {"owner_id": event.actor_owner_id, "delta": credit}
        return effect

    if logical_type is EventType.EMI_PAYMENT:
        # The payer's CapEx grows by the principal portion paid (passed via
        # metadata) — but routing the principal/interest split is the
        # caller's job. Here we credit the gross amount to capex; balance.py
        # consults metadata for the principal-only portion.
        if amount is not None:
            effect["owner_capex"] = {"owner_id": event.actor_owner_id, "delta": amount}
        if event.loan_id is not None and amount is not None:
            effect["bank_loan"] = {"loan_id": event.loan_id, "delta": -amount}
        return effect

    if logical_type is EventType.BULK_PREPAYMENT:
        if amount is not None:
            effect["owner_capex"] = {"owner_id": event.actor_owner_id, "delta": amount}
        if event.loan_id is not None and amount is not None:
            effect["bank_loan"] = {"loan_id": event.loan_id, "delta": -amount}
        return effect

    if logical_type is EventType.INTERPERSONAL_LOAN_DISBURSEMENT:
        if event.target_owner_id is not None and amount is not None:
            effect["interpersonal"] = {
                "lender": event.actor_owner_id,
                "borrower": event.target_owner_id,
                "delta": amount,
            }
        return effect

    if logical_type is EventType.INTERPERSONAL_LOAN_REPAYMENT:
        # actor=borrower repaying target=lender. Normalized to lender→borrower
        # framing with a negative delta.
        if event.target_owner_id is not None and amount is not None:
            effect["interpersonal"] = {
                "lender": event.target_owner_id,
                "borrower": event.actor_owner_id,
                "delta": -amount,
            }
        return effect

    if logical_type is EventType.SETTLEMENT:
        # Conventional direction: actor=payer, target=recipient. The payer
        # reduces what they owed the recipient (or, equivalently, the
        # recipient now owes the payer if there was no debt).
        if event.target_owner_id is not None and amount is not None:
            effect["interpersonal"] = {
                "lender": event.target_owner_id,
                "borrower": event.actor_owner_id,
                "delta": -amount,
            }
        return effect

    if logical_type is EventType.OPEX_SPLIT:
        # actor=the owner whose share this is, target=the owner who paid.
        # The split's actor owes the target their share — except when the
        # actor IS the target (the payer's own share, which is a no-op for
        # inter-personal balance but still records owner_opex).
        if (
            event.target_owner_id is not None
            and event.target_owner_id != event.actor_owner_id
            and amount is not None
        ):
            effect["interpersonal"] = {
                "lender": event.target_owner_id,
                "borrower": event.actor_owner_id,
                "delta": amount,
            }
        if amount is not None:
            effect["owner_opex"] = {"owner_id": event.actor_owner_id, "delta": amount}
        return effect

    raise ValueError(f"Unhandled event type: {logical_type}")
