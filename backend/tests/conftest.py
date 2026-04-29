"""
Shared fixtures for the backend test suite.

Two helpers are exposed:
  - `secret_key` — a deterministic HMAC key for signing test events.
  - `make_event` — a factory that builds a populated `LedgerEvent` with
                   sensible defaults, overridable by keyword.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# Make the backend package importable when pytest is run from the repo root or
# the backend directory. The package is laid out as flat modules at backend/
# (api/, core/, etc.), not nested in a `backend/` package, so we need to add
# the backend directory to sys.path.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core.events import EventType, LedgerEvent  # noqa: E402


@pytest.fixture()
def secret_key() -> str:
    """A deterministic HMAC key. Tests should never use the prod key."""
    return "test-secret-key-do-not-use-in-prod"


@pytest.fixture()
def alt_secret_key() -> str:
    """A second key, for tests that compare two-key behavior."""
    return "different-test-secret-key"


@pytest.fixture()
def make_event():
    """
    Factory that returns a `LedgerEvent` populated with valid defaults for a
    CONTRIBUTION-style event. Override any field by keyword:

        evt = make_event(event_type=EventType.SETTLEMENT, target_owner_id=uuid4())
    """

    def _factory(**overrides: Any) -> LedgerEvent:
        event_type = overrides.get("event_type", EventType.CONTRIBUTION)
        # EMI_PAYMENT carries an amortization split in metadata. The factory
        # fills in a sensible default so callers don't have to supply it for
        # every EMI test; explicit `metadata=` overrides win.
        default_metadata: dict[str, Any] = {}
        if event_type is EventType.EMI_PAYMENT:
            default_metadata = {
                "principal_component": Decimal("35000.00"),
                "interest_component": Decimal("15000.00"),
                "emi_schedule_id": str(uuid.uuid4()),
            }

        defaults: dict[str, Any] = {
            "property_id": uuid.uuid4(),
            "event_type": EventType.CONTRIBUTION,
            "actor_owner_id": uuid.uuid4(),
            "target_owner_id": None,
            "loan_id": None,
            "amount_source_currency": Decimal("5000.00"),
            "source_currency": "USD",
            "amount_property_currency": Decimal("415000.00"),
            "property_currency": "INR",
            "fx_rate_actual": Decimal("83.000000"),
            "fx_rate_reference": Decimal("83.250000"),
            "fee_source_currency": Decimal("25.00"),
            "inr_landed": Decimal("413925.00"),
            "description": "Test event",
            "metadata": default_metadata,
            "reverses_event_id": None,
            "hmac_signature": None,
            "recorded_by": "tester@example.com",
            "recorded_at": datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
            "effective_date": date(2026, 5, 1),
        }
        defaults.update(overrides)
        return LedgerEvent(**defaults)

    return _factory
