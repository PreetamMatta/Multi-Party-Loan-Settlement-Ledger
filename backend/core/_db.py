"""
_db.py — Shared asyncpg-row helpers for balance.py and interest.py.

Extracted so any schema change to the events table touches exactly one place.
Both projection modules import from here rather than maintaining independent
copies.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from core.events import EventType, LedgerEvent, get_financial_effect

_EVENT_COLUMNS = (
    "id, property_id, event_type, actor_owner_id, target_owner_id, loan_id, "
    "amount_source_currency, source_currency, amount_property_currency, "
    "property_currency, fx_rate_actual, fx_rate_reference, fee_source_currency, "
    "inr_landed, description, metadata, reverses_event_id, hmac_signature, "
    "recorded_by, recorded_at, effective_date"
)


def _row_to_event(row: Any) -> LedgerEvent:
    """
    Reconstruct a LedgerEvent from an asyncpg.Record (or any mapping with the
    same column names).

    The HMAC signature is intentionally NOT re-verified here — verification is
    a separate audit concern; balance math trusts the log.
    """
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


def _events_to_pair_balance(
    events: Iterable[LedgerEvent],
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
) -> Decimal:
    """
    Fold the event stream into a net (borrower owes lender) principal balance.

    The router returns deltas in normalized lender→borrower framing. If a
    routed effect's lender/borrower matches the requested pair direction we
    add; if it matches the reverse direction we subtract; otherwise we ignore.
    """
    balance = Decimal("0")
    for event in events:
        effect = get_financial_effect(event)
        ip = effect.get("interpersonal")
        if ip is None:
            continue
        if ip["lender"] == lender_id and ip["borrower"] == borrower_id:
            balance += ip["delta"]
        elif ip["lender"] == borrower_id and ip["borrower"] == lender_id:
            balance -= ip["delta"]
    return balance
