"""
Tests for fx fetch functions in backend/core/fx.py.

Two functions are tested independently because they serve different roles
(see docs/business-logic/fx-and-wire-transfers.md):

  - fetch_reference_rate(date, pair, store)
        Read-time lookup. Reads from the `fx_rates` store ONLY. Never
        calls the live API. This is what makes historical balance replays
        deterministic.

  - fetch_reference_rate_from_api(date, pair, http_client)
        Populator-side helper. Calls exchangerate.host. Returns None on
        any failure (the cron decides what to do). Never written to by
        any read-time code path.

The HTTP layer is mocked with `pytest-httpx`. Real network calls in the
test suite would be slow, flaky, and dependent on a third-party service.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import httpx
import pytest

from core.fx import (
    EXCHANGERATE_HOST_BASE,
    FXRateNotFoundError,
    fetch_reference_rate,
    fetch_reference_rate_from_api,
)


class _StubFXRateStore:
    """A minimal in-memory `fx_rates` store for tests."""

    def __init__(self, rows: list[tuple[date, str, Decimal]] | None = None) -> None:
        self.rows = rows or []

    async def get_latest_reference_rate_on_or_before(
        self, on_or_before: date, currency_pair: str
    ) -> tuple[date, Decimal] | None:
        candidates = [
            (d, rate)
            for (d, pair, rate) in self.rows
            if pair == currency_pair and d <= on_or_before
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0]


# ---------------------------------------------------------------------------
# fetch_reference_rate — read-time lookup, store-only
# ---------------------------------------------------------------------------
class TestFetchReferenceRateReadPath:
    @pytest.mark.asyncio
    async def test_returns_exact_match_without_warning(self, caplog):
        """
        When the store has a row for exactly the requested date, that rate
        is returned with no warning logged. This is the happy path —
        every well-populated read should hit it.
        """
        rate_date = date(2026, 5, 1)
        store = _StubFXRateStore(rows=[(rate_date, "USD_INR", Decimal("83.4521"))])
        with caplog.at_level(logging.WARNING):
            rate = await fetch_reference_rate(rate_date, pair="USD_INR", store=store)
        assert rate == Decimal("83.4521")
        assert isinstance(rate, Decimal)
        # No fallback warning should fire on an exact match.
        assert not any(r.message == "fx_rate_fallback" for r in caplog.records)

    @pytest.mark.asyncio
    async def test_falls_back_to_most_recent_with_structured_warning(self, caplog):
        """
        When the exact date is missing, the function returns the most
        recent stored rate with `rate_date <= requested_date` and emits a
        structured WARNING so the audit trail records that a fallback was
        used. The fallback is acceptable because the reference rate moves
        slowly and drives FX gain/loss reporting only — not balance math.
        """
        rate_date = date(2026, 5, 1)
        fallback_date = date(2026, 4, 30)
        store = _StubFXRateStore(rows=[(fallback_date, "USD_INR", Decimal("83.10"))])
        with caplog.at_level(logging.WARNING):
            rate = await fetch_reference_rate(rate_date, pair="USD_INR", store=store)
        assert rate == Decimal("83.10")
        matches = [r for r in caplog.records if r.message == "fx_rate_fallback"]
        assert matches, "Expected a structured 'fx_rate_fallback' warning."
        record = matches[0]
        assert record.levelno == logging.WARNING
        assert getattr(record, "pair", None) == "USD_INR"
        assert getattr(record, "requested_date", None) == rate_date.isoformat()
        assert getattr(record, "fallback_date", None) == fallback_date.isoformat()

    @pytest.mark.asyncio
    async def test_raises_when_store_has_no_rate(self):
        """
        When the store has no row at or before the requested date, the
        function must raise FXRateNotFoundError with a clear message.
        Silent failure here would let an event be written without a
        reference rate — invisible until tax season.
        """
        rate_date = date(2026, 5, 1)
        store = _StubFXRateStore(rows=[])
        with pytest.raises(FXRateNotFoundError):
            await fetch_reference_rate(rate_date, pair="USD_INR", store=store)

    @pytest.mark.asyncio
    async def test_raises_when_no_store_provided(self):
        """
        Calling without a store is a programming error — there is no
        source of truth to consult. The function must raise
        FXRateNotFoundError rather than silently returning a default.
        """
        with pytest.raises(FXRateNotFoundError):
            await fetch_reference_rate(date(2026, 5, 1), pair="USD_INR", store=None)

    @pytest.mark.asyncio
    async def test_does_not_call_live_api(self, httpx_mock):
        """
        Read-time lookups must NEVER hit the live API — that breaks
        reproducibility. If the function is correctly store-only, no HTTP
        request is dispatched and pytest-httpx asserts no mocks were
        consumed. We verify this by registering NO mocks: any HTTP call
        would fail loudly on pytest-httpx's "no matching response" error.
        """
        rate_date = date(2026, 5, 1)
        store = _StubFXRateStore(rows=[(rate_date, "USD_INR", Decimal("83.4521"))])
        rate = await fetch_reference_rate(rate_date, pair="USD_INR", store=store)
        assert rate == Decimal("83.4521")

    @pytest.mark.asyncio
    async def test_is_deterministic_across_repeat_calls(self):
        """
        Same (date, pair) query, called multiple times, must return the
        same value. This is the reproducibility guarantee that motivates
        the store-first design — the same balance replay produces the
        same answer every time.
        """
        rate_date = date(2026, 5, 1)
        store = _StubFXRateStore(rows=[(rate_date, "USD_INR", Decimal("83.4521"))])
        a = await fetch_reference_rate(rate_date, pair="USD_INR", store=store)
        b = await fetch_reference_rate(rate_date, pair="USD_INR", store=store)
        c = await fetch_reference_rate(rate_date, pair="USD_INR", store=store)
        assert a == b == c == Decimal("83.4521")


# ---------------------------------------------------------------------------
# fetch_reference_rate_from_api — populator-side, calls live API
# ---------------------------------------------------------------------------
class TestFetchReferenceRateFromApi:
    @pytest.mark.asyncio
    async def test_parses_api_response_to_decimal(self, httpx_mock):
        """
        On a successful 200 response with a well-formed body, the function
        must return the rate parsed as a Decimal. The str() round-trip
        avoids float drift if the API returned a JSON number.
        """
        rate_date = date(2026, 5, 1)
        httpx_mock.add_response(
            url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
            json={"base": "USD", "date": rate_date.isoformat(), "rates": {"INR": 83.4521}},
        )
        rate = await fetch_reference_rate_from_api(rate_date, pair="USD_INR")
        assert isinstance(rate, Decimal)
        assert rate == Decimal("83.4521")

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200(self, httpx_mock):
        """
        A 5xx (or any non-200) must return None. The cron decides whether
        to retry, alert, or skip writing for that day.
        """
        rate_date = date(2026, 5, 1)
        httpx_mock.add_response(
            url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
            status_code=500,
        )
        result = await fetch_reference_rate_from_api(rate_date, pair="USD_INR")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_network_error(self, httpx_mock):
        """
        A connection error must return None rather than propagating —
        the cron is the only caller and treats None uniformly.
        """
        httpx_mock.add_exception(httpx.ConnectError("network down"))
        result = await fetch_reference_rate_from_api(date(2026, 5, 1), pair="USD_INR")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_malformed_json(self, httpx_mock):
        """
        A 200 with an HTML body (which sometimes happens during outages)
        must return None — parsing errors do not propagate.
        """
        rate_date = date(2026, 5, 1)
        httpx_mock.add_response(
            url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
            content=b"<html>not json</html>",
            headers={"content-type": "text/html"},
        )
        result = await fetch_reference_rate_from_api(rate_date, pair="USD_INR")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_rates_field_missing(self, httpx_mock):
        """
        A 200 with a JSON body but no `rates` key (API shape change) must
        return None. Defensive parsing keeps the populator resilient to
        upstream changes.
        """
        rate_date = date(2026, 5, 1)
        httpx_mock.add_response(
            url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
            json={"base": "USD", "date": rate_date.isoformat()},
        )
        result = await fetch_reference_rate_from_api(rate_date, pair="USD_INR")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_quote_missing_from_rates(self, httpx_mock):
        """
        A `rates` dict that doesn't contain the requested quote currency
        means the upstream provider doesn't track that pair on that date.
        Return None.
        """
        rate_date = date(2026, 5, 1)
        httpx_mock.add_response(
            url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
            json={"base": "USD", "rates": {"EUR": 0.92}},
        )
        result = await fetch_reference_rate_from_api(rate_date, pair="USD_INR")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_unparseable_rate_value(self, httpx_mock):
        """
        A rate value that is not Decimal-coercible (e.g., a string token
        like 'unavailable') must return None.
        """
        rate_date = date(2026, 5, 1)
        httpx_mock.add_response(
            url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
            json={"base": "USD", "rates": {"INR": "unavailable"}},
        )
        result = await fetch_reference_rate_from_api(rate_date, pair="USD_INR")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_null_rate_value(self, httpx_mock):
        """
        `null` rate (the API has no data for that pair on that date —
        e.g., weekends or holidays for some pairs) must return None.
        """
        rate_date = date(2026, 5, 1)
        httpx_mock.add_response(
            url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
            json={"base": "USD", "rates": {"INR": None}},
        )
        result = await fetch_reference_rate_from_api(rate_date, pair="USD_INR")
        assert result is None

    @pytest.mark.asyncio
    async def test_raises_on_invalid_pair_token(self):
        """
        A malformed pair token is a programming error and must raise
        immediately. The pair format is the canonical 'BASE_QUOTE'.
        """
        with pytest.raises(ValueError):
            await fetch_reference_rate_from_api(date(2026, 5, 1), pair="USDINR")
