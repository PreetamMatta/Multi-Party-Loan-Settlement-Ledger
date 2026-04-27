"""
events.py — Event log model, HMAC signing, and compensating transaction helpers.

Every write to the ledger must go through this module.

The HMAC signature guarantees tamper-evidence: if any canonical field is altered
after signing, verification will fail. The canonical field order is FROZEN —
future event types must use the same order, or the entire historical event log
will fail re-verification on a key rotation.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# -----------------------------------------------------------------------------
# Event types — must match the documented enum in backend/db/schema.sql
# -----------------------------------------------------------------------------
class EventType(str, Enum):
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
    target_owner_id: Optional[uuid.UUID] = None
    loan_id: Optional[uuid.UUID] = None

    amount_source_currency: Optional[Decimal] = None
    source_currency: Optional[str] = None
    amount_property_currency: Optional[Decimal] = None
    property_currency: Optional[str] = None
    fx_rate_actual: Optional[Decimal] = None
    fx_rate_reference: Optional[Decimal] = None
    fee_source_currency: Optional[Decimal] = None
    inr_landed: Optional[Decimal] = None

    description: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    reverses_event_id: Optional[uuid.UUID] = None

    hmac_signature: Optional[str] = None
    recorded_by: str
    recorded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    effective_date: date


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
        str(event.amount_property_currency)
        if event.amount_property_currency is not None
        else ""
    )
    # Render recorded_at in a stable, timezone-aware ISO format.
    recorded_at = event.recorded_at
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
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
        amount_negated: Optional[Decimal] = None
    else:
        amount_negated = -original.amount_property_currency

    if original.amount_source_currency is None:
        source_amount_negated: Optional[Decimal] = None
    else:
        source_amount_negated = -original.amount_source_currency

    if original.inr_landed is None:
        inr_landed_negated: Optional[Decimal] = None
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
        metadata={"reverses_original_event": str(original.id)},
        reverses_event_id=original.id,
        recorded_by=actor_email,
        effective_date=original.effective_date,
    )

    compensating.hmac_signature = sign_event(compensating, secret_key)
    return compensating
