"""
Test doubles for the asyncpg.Connection interface used by core/balance.py and
core/interest.py. The projection functions only call `db.fetch`, `db.fetchval`,
and `db.fetchrow`; this module supplies in-memory equivalents that match queries
by substring keyword.

These helpers are deliberately small — they replicate just enough of asyncpg's
shape to drive unit tests. Functional tests (backend/tests/functional/) use a
real Postgres connection.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.events import LedgerEvent

_FetchResponse = list[dict[str, Any]] | Callable[[str, tuple], list[dict[str, Any]]]
_ValResponse = Any | Callable[[str, tuple], Any]


class FakeConnection:
    """
    A minimal asyncpg-shaped mock. Register handlers for queries by substring;
    when a method is called, the first handler whose substring is in the query
    wins. Unmatched queries return empty result / None.
    """

    def __init__(self) -> None:
        self._fetch: list[tuple[str, _FetchResponse]] = []
        self._fetchval: list[tuple[str, _ValResponse]] = []

    def on_fetch(self, substring: str, response: _FetchResponse) -> None:
        self._fetch.append((substring, response))

    def on_fetchval(self, substring: str, response: _ValResponse) -> None:
        self._fetchval.append((substring, response))

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        for substr, resp in self._fetch:
            if substr in query:
                return resp(query, args) if callable(resp) else list(resp)
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        for substr, resp in self._fetchval:
            if substr in query:
                return resp(query, args) if callable(resp) else resp
        return None

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        rows = await self.fetch(query, *args)
        return rows[0] if rows else None


def event_to_row(event: LedgerEvent) -> dict[str, Any]:
    """Convert a LedgerEvent into an asyncpg-Record-shaped dict."""
    return {
        "id": event.id,
        "property_id": event.property_id,
        "event_type": event.event_type.value,
        "actor_owner_id": event.actor_owner_id,
        "target_owner_id": event.target_owner_id,
        "loan_id": event.loan_id,
        "amount_source_currency": event.amount_source_currency,
        "source_currency": event.source_currency,
        "amount_property_currency": event.amount_property_currency,
        "property_currency": event.property_currency,
        "fx_rate_actual": event.fx_rate_actual,
        "fx_rate_reference": event.fx_rate_reference,
        "fee_source_currency": event.fee_source_currency,
        "inr_landed": event.inr_landed,
        "description": event.description,
        "metadata": event.metadata,
        "reverses_event_id": event.reverses_event_id,
        "hmac_signature": event.hmac_signature or "test-sig",
        "recorded_by": event.recorded_by,
        "recorded_at": event.recorded_at,
        "effective_date": event.effective_date,
    }
