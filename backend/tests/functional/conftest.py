"""
Functional-test fixtures: real Postgres connection with `schema.sql` applied.

Tests in this directory are skipped at module-load time if `TEST_DATABASE_URL`
is not set. No half-runs, no manual `pytest.skip` calls in each test.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# `pytest.skip(allow_module_level=True)` from a conftest is treated as an
# error rather than a graceful skip, so we filter at collection time instead.
# Test modules carry a `pytestmark` that skips them when TEST_DATABASE_URL
# is unset; the fixtures below only run in suites where the DB is available.
_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
DATABASE_AVAILABLE = bool(_DATABASE_URL)

if DATABASE_AVAILABLE:
    import asyncpg
else:
    asyncpg = None  # sentinel — the pytestmark skip prevents the fixtures from running

from core.events import EventType, LedgerEvent, sign_event  # noqa: E402

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


# -----------------------------------------------------------------------------
# Per-session: apply schema once. Per-test: truncate so tests are isolated.
# -----------------------------------------------------------------------------
@pytest.fixture(scope="session")
async def _schema_applied() -> None:
    """Drop everything and reapply schema once at session start."""
    conn = await asyncpg.connect(_DATABASE_URL)
    try:
        # Wipe public schema so a stale prior run doesn't poison the suite.
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(_SCHEMA_PATH.read_text())
    finally:
        await conn.close()


@pytest.fixture()
async def db(_schema_applied) -> Any:
    """A per-test connection. Tables are truncated before each test."""
    conn = await asyncpg.connect(_DATABASE_URL)
    # Coerce JSONB ↔ Python dict automatically — by default asyncpg returns
    # JSONB as a string, which the projection code accepts via str-coercion
    # but the inserts below need a json codec.
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    try:
        # Truncate writeable tables before each test. RESTART IDENTITY isn't
        # needed (UUIDs), but CASCADE handles FK cleanup in one statement.
        await conn.execute(
            """
            TRUNCATE TABLE
                opex_splits, documents, events, market_value_snapshots,
                emi_schedule, bank_loans, interpersonal_loans,
                fx_rates, owners, properties
            CASCADE;
            """
        )
        yield conn
    finally:
        await conn.close()


# -----------------------------------------------------------------------------
# Helpers — keep functional tests readable.
# -----------------------------------------------------------------------------
TEST_SECRET = "functional-test-secret-key"


async def make_property(db: Any, currency: str = "INR") -> uuid.UUID:
    """Insert a single property and return its id."""
    row = await db.fetchrow(
        """
        INSERT INTO properties (
            name, address, city, country, property_currency, purchase_price, purchase_date
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        "Test Property",
        "1 Test Lane",
        "Hyderabad",
        "IN",
        currency,
        Decimal("15000000"),
        date(2026, 1, 1),
    )
    return row["id"]


async def make_owner(
    db: Any, property_id: uuid.UUID, *, name: str, email: str, equity_pct: Decimal
) -> uuid.UUID:
    row = await db.fetchrow(
        """
        INSERT INTO owners (
            property_id, display_name, email, equity_pct, base_currency, joined_at
        ) VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        property_id,
        name,
        email,
        equity_pct,
        "INR",
        date(2026, 1, 1),
    )
    return row["id"]


async def make_bank_loan(db: Any, property_id: uuid.UUID, *, principal: Decimal) -> uuid.UUID:
    row = await db.fetchrow(
        """
        INSERT INTO bank_loans (
            property_id, lender_name, principal_inr, interest_rate_pct,
            tenure_months, emi_amount, disbursement_date
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        property_id,
        "Test Bank",
        principal,
        Decimal("8.5"),
        240,
        Decimal("15000"),
        date(2026, 1, 1),
    )
    return row["id"]


async def insert_event(db: Any, event: LedgerEvent) -> uuid.UUID:
    """
    Sign and insert a fully populated LedgerEvent. Returns the inserted id.
    """
    if event.hmac_signature is None:
        event.hmac_signature = sign_event(event, TEST_SECRET)
    await db.execute(
        """
        INSERT INTO events (
            id, property_id, event_type, actor_owner_id, target_owner_id, loan_id,
            amount_source_currency, source_currency, amount_property_currency,
            property_currency, fx_rate_actual, fx_rate_reference, fee_source_currency,
            inr_landed, description, metadata, reverses_event_id,
            hmac_signature, recorded_by, recorded_at, effective_date
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
            $15, $16, $17, $18, $19, $20, $21
        )
        """,
        event.id,
        event.property_id,
        event.event_type.value,
        event.actor_owner_id,
        event.target_owner_id,
        event.loan_id,
        event.amount_source_currency,
        event.source_currency,
        event.amount_property_currency,
        event.property_currency,
        event.fx_rate_actual,
        event.fx_rate_reference,
        event.fee_source_currency,
        event.inr_landed,
        event.description,
        event.metadata,
        event.reverses_event_id,
        event.hmac_signature,
        event.recorded_by,
        event.recorded_at,
        event.effective_date,
    )
    return event.id


def event(
    *,
    property_id: uuid.UUID,
    event_type: EventType,
    actor: uuid.UUID,
    target: uuid.UUID | None = None,
    loan_id: uuid.UUID | None = None,
    amount: Decimal | None = None,
    effective_date: date,
    description: str = "Test event",
    metadata: dict | None = None,
    reverses_event_id: uuid.UUID | None = None,
    recorded_at: datetime | None = None,
) -> LedgerEvent:
    """Convenience builder — fills in the boring fields."""
    return LedgerEvent(
        property_id=property_id,
        event_type=event_type,
        actor_owner_id=actor,
        target_owner_id=target,
        loan_id=loan_id,
        amount_property_currency=amount,
        property_currency="INR",
        description=description,
        metadata=metadata or {},
        reverses_event_id=reverses_event_id,
        recorded_by="functional-test",
        recorded_at=recorded_at
        or datetime(
            effective_date.year, effective_date.month, effective_date.day, 12, 0, tzinfo=UTC
        ),
        effective_date=effective_date,
    )
