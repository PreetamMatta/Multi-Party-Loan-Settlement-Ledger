"""
Tests for fetch_reference_rate in backend/core/fx.py.

The HTTP layer is mocked with `pytest-httpx`. Real network calls in the test
suite would be slow, flaky, and dependent on a third-party service — none
of which is acceptable.

These tests pin three behaviors documented in
docs/business-logic/fx-and-wire-transfers.md:
  1. Successful API responses are parsed correctly to Decimal.
  2. API failures fall back to the most recent stored rate.
  3. Missing API + missing fallback raises FXRateNotFoundError.
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
)


class _StubFallbackStore:
    """A minimal in-memory fallback store for tests."""

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


@pytest.mark.asyncio
async def test_fetch_reference_rate_parses_api_response(httpx_mock):
    """
    On a successful 200 response with a well-formed body, the function
    must return the rate parsed as a Decimal. Going through str() ensures
    we don't inherit float drift from JSON parsing.
    """
    rate_date = date(2026, 5, 1)
    httpx_mock.add_response(
        url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
        json={"base": "USD", "date": rate_date.isoformat(), "rates": {"INR": 83.4521}},
    )
    rate = await fetch_reference_rate(rate_date, pair="USD_INR")
    assert isinstance(rate, Decimal)
    assert rate == Decimal("83.4521")


@pytest.mark.asyncio
async def test_fetch_reference_rate_falls_back_on_api_failure(httpx_mock, caplog):
    """
    When the API returns a 5xx, the function must fall back to the most
    recent rate stored in the fallback store. A WARNING with structured
    fields must be logged so the audit trail records that a fallback was used.
    """
    rate_date = date(2026, 5, 1)
    fallback_date = date(2026, 4, 30)
    httpx_mock.add_response(
        url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
        status_code=500,
    )
    store = _StubFallbackStore(rows=[(fallback_date, "USD_INR", Decimal("83.1000"))])
    with caplog.at_level(logging.WARNING):
        rate = await fetch_reference_rate(rate_date, pair="USD_INR", fallback_store=store)
    assert rate == Decimal("83.1000")
    # Find the structured warning record.
    matches = [r for r in caplog.records if r.message == "fx_rate_fallback"]
    assert matches, "Expected a structured 'fx_rate_fallback' warning."
    record = matches[0]
    assert record.levelno == logging.WARNING
    assert getattr(record, "pair", None) == "USD_INR"
    assert getattr(record, "requested_date", None) == rate_date.isoformat()
    assert getattr(record, "fallback_date", None) == fallback_date.isoformat()


@pytest.mark.asyncio
async def test_fetch_reference_rate_raises_when_no_fallback(httpx_mock):
    """
    When the API fails AND the fallback store has no candidate, the
    function must raise FXRateNotFoundError with a clear message. Silent
    failure here would let an event be written without a reference rate
    — a correctness bug invisible until tax season.
    """
    rate_date = date(2026, 5, 1)
    httpx_mock.add_response(
        url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
        status_code=500,
    )
    empty_store = _StubFallbackStore(rows=[])
    with pytest.raises(FXRateNotFoundError):
        await fetch_reference_rate(rate_date, pair="USD_INR", fallback_store=empty_store)


@pytest.mark.asyncio
async def test_fetch_reference_rate_raises_on_network_error_with_no_fallback(httpx_mock):
    """
    A network-level failure (ConnectError) on the API call must be
    indistinguishable from a non-200 from the caller's perspective: it
    falls back, and if no fallback exists, raises FXRateNotFoundError.
    """
    rate_date = date(2026, 5, 1)
    httpx_mock.add_exception(httpx.ConnectError("network down"))
    with pytest.raises(FXRateNotFoundError):
        await fetch_reference_rate(rate_date, pair="USD_INR", fallback_store=None)


@pytest.mark.asyncio
async def test_fetch_reference_rate_raises_on_invalid_pair():
    """
    A malformed pair token must raise immediately. The pair format is the
    canonical 'BASE_QUOTE'; anything else is a programming error and
    should not be silently coerced.
    """
    with pytest.raises(ValueError):
        await fetch_reference_rate(date(2026, 5, 1), pair="USDINR")


@pytest.mark.asyncio
async def test_fetch_reference_rate_falls_back_on_malformed_json(httpx_mock):
    """
    A 200 response whose body is not valid JSON must be treated as a
    failed fetch (not a crash). Real APIs occasionally return HTML error
    pages with a 200 status during outages — we cannot let that propagate
    as a parser exception.
    """
    rate_date = date(2026, 5, 1)
    httpx_mock.add_response(
        url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
        content=b"<html>not json</html>",
        headers={"content-type": "text/html"},
    )
    fallback_date = date(2026, 4, 30)
    store = _StubFallbackStore(rows=[(fallback_date, "USD_INR", Decimal("83.10"))])
    rate = await fetch_reference_rate(rate_date, pair="USD_INR", fallback_store=store)
    assert rate == Decimal("83.10")


@pytest.mark.asyncio
async def test_fetch_reference_rate_falls_back_when_rates_missing(httpx_mock):
    """
    A 200 response whose body is JSON but lacks the expected `rates`
    field is treated as a failed fetch. exchangerate.host has historically
    changed its response shape; defensive parsing keeps the ledger
    resilient.
    """
    rate_date = date(2026, 5, 1)
    httpx_mock.add_response(
        url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
        json={"base": "USD", "date": rate_date.isoformat()},  # no `rates` key
    )
    fallback_date = date(2026, 4, 30)
    store = _StubFallbackStore(rows=[(fallback_date, "USD_INR", Decimal("83.10"))])
    rate = await fetch_reference_rate(rate_date, pair="USD_INR", fallback_store=store)
    assert rate == Decimal("83.10")


@pytest.mark.asyncio
async def test_fetch_reference_rate_falls_back_when_quote_missing(httpx_mock):
    """
    A 200 response with a `rates` dict that doesn't contain the requested
    quote currency is a failed fetch. This is what happens when the
    upstream provider doesn't track that pair on a given date.
    """
    rate_date = date(2026, 5, 1)
    httpx_mock.add_response(
        url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
        json={"base": "USD", "rates": {"EUR": 0.92}},  # no INR
    )
    fallback_date = date(2026, 4, 30)
    store = _StubFallbackStore(rows=[(fallback_date, "USD_INR", Decimal("83.10"))])
    rate = await fetch_reference_rate(rate_date, pair="USD_INR", fallback_store=store)
    assert rate == Decimal("83.10")


@pytest.mark.asyncio
async def test_fetch_reference_rate_falls_back_on_unparseable_rate_value(httpx_mock):
    """
    A 200 response whose rate value is not coercible to Decimal (e.g.,
    `null`, a string like 'unavailable') must be treated as a failed
    fetch rather than crashing.
    """
    rate_date = date(2026, 5, 1)
    httpx_mock.add_response(
        url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
        json={"base": "USD", "rates": {"INR": "unavailable"}},
    )
    fallback_date = date(2026, 4, 30)
    store = _StubFallbackStore(rows=[(fallback_date, "USD_INR", Decimal("83.10"))])
    rate = await fetch_reference_rate(rate_date, pair="USD_INR", fallback_store=store)
    assert rate == Decimal("83.10")


@pytest.mark.asyncio
async def test_fetch_reference_rate_falls_back_on_null_rate_value(httpx_mock):
    """
    A `null` rate value (the API has no data for that pair on that date)
    must be treated as a failed fetch. This is documented behavior for
    exchangerate.host on weekends and holidays for some pairs.
    """
    rate_date = date(2026, 5, 1)
    httpx_mock.add_response(
        url=f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}?base=USD&symbols=INR",
        json={"base": "USD", "rates": {"INR": None}},
    )
    fallback_date = date(2026, 4, 30)
    store = _StubFallbackStore(rows=[(fallback_date, "USD_INR", Decimal("83.10"))])
    rate = await fetch_reference_rate(rate_date, pair="USD_INR", fallback_store=store)
    assert rate == Decimal("83.10")
