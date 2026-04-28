"""
fx.py — FX utilities for dual-rate stamping and wire fee handling.

Every USD↔INR (or other cross-currency) movement in the ledger must be stamped
with two rates:

  - fx_rate_actual:    The rate the bank actually applied. Used for balance math.
  - fx_rate_reference: The mid-market reference rate on that date (RBI /
                       exchangerate.host). Used for FX gain/loss reporting only.

Wire fees are the sender's cost and are never socialized.
The credit to the sender's balance = inr_landed (not amount_source * rate).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

EXCHANGERATE_HOST_BASE = "https://api.exchangerate.host"


class FXRateNotFoundError(Exception):
    """Raised when no reference rate is available for a (date, pair)."""


class FXRateFallbackStore(Protocol):
    """
    Minimal interface the fallback database lookup must satisfy. Lets us
    inject a fake in tests without dragging asyncpg or SQLAlchemy in. The
    real implementation against the database is wired in Session 3 / 4.
    """

    async def get_latest_reference_rate_on_or_before(
        self, on_or_before: date, currency_pair: str
    ) -> tuple[date, Decimal] | None:
        ...


@dataclass(frozen=True)
class FXStamp:
    """
    The dual-rate snapshot attached to a cross-currency event.

    Attributes:
        amount_source_currency:    Amount sent in the source currency (e.g. USD).
        source_currency:           ISO code of the source currency (e.g. 'USD').
        fee_source_currency:       Wire / transfer fee in source currency.
                                   Borne by the sender; never socialized.
        fx_rate_actual:            The rate the bank actually applied.
                                   Drives balance math.
        fx_rate_reference:         Mid-market reference rate on that date.
                                   Drives FX gain/loss reporting only.
        inr_landed:                What actually arrived in property currency
                                   after fees and the actual rate. This is the
                                   value credited to the sender's balance.
        inr_reference_equivalent:  What the gross amount would have been worth
                                   in property currency at the reference rate.
        fx_gain_loss_inr:          inr_landed - inr_reference_equivalent.
                                   Positive means the sender did better than
                                   the reference rate (after fees); negative
                                   means worse (typical due to spread + fees).
    """

    amount_source_currency: Decimal
    source_currency: str
    fee_source_currency: Decimal
    fx_rate_actual: Decimal
    fx_rate_reference: Decimal
    inr_landed: Decimal
    inr_reference_equivalent: Decimal
    fx_gain_loss_inr: Decimal


def stamp_fx_event(
    amount_source: Decimal,
    fee_source: Decimal,
    rate_actual: Decimal,
    rate_reference: Decimal,
    source_currency: str = "USD",
) -> FXStamp:
    """
    Compute the dual-rate FX stamp for a cross-currency transfer.

    Math (all `Decimal` — never `float`):
        inr_landed              = (amount_source - fee_source) * rate_actual
        inr_reference_equivalent = amount_source * rate_reference
        fx_gain_loss_inr        = inr_landed - inr_reference_equivalent

    `amount_source` is the gross amount sent (before fee). `fee_source` is the
    sender's wire/transfer fee in the source currency. The fee is intentionally
    NOT applied to `inr_reference_equivalent` because that figure represents
    what the gross amount *would* have been worth at the mid-market rate — the
    delta between the two captures both the bank's spread and the fee in one
    number, which is what the FX gain/loss report wants.
    """
    inr_landed = (amount_source - fee_source) * rate_actual
    inr_reference_equivalent = amount_source * rate_reference
    fx_gain_loss_inr = inr_landed - inr_reference_equivalent

    return FXStamp(
        amount_source_currency=amount_source,
        source_currency=source_currency,
        fee_source_currency=fee_source,
        fx_rate_actual=rate_actual,
        fx_rate_reference=rate_reference,
        inr_landed=inr_landed,
        inr_reference_equivalent=inr_reference_equivalent,
        fx_gain_loss_inr=fx_gain_loss_inr,
    )


def _parse_pair(pair: str) -> tuple[str, str]:
    """
    Split an internal pair token like 'USD_INR' into (base, quote).

    The internal canonical format is 'BASE_QUOTE' (e.g., 'USD_INR'); the
    external API expects them as separate `base=`/`symbols=` query params.
    """
    if "_" not in pair:
        raise ValueError(f"Invalid currency pair token: {pair!r} (expected 'BASE_QUOTE').")
    base, quote = pair.split("_", 1)
    return base, quote


async def fetch_reference_rate(
    rate_date: date,
    pair: str = "USD_INR",
    fallback_store: FXRateFallbackStore | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Decimal:
    """
    Look up the mid-market reference rate for a (date, pair).

    Resolution order:
      1. Hit exchangerate.host for the requested date.
      2. On any failure (network error, non-200, missing field) fall back to
         the most recent reference rate stored for this pair on or before
         `rate_date` via `fallback_store`.
      3. If no fallback exists either, raise `FXRateNotFoundError`.

    A `WARNING` is emitted with structured fields whenever the fallback path
    is used, so the audit trail records exactly which date's rate was
    substituted. See docs/business-logic/fx-and-wire-transfers.md for the
    full rationale.

    Note: The Session 8 daily-snapshot cron job will call this same function
    to populate the `fx_rates` table; that job is the producer, this function
    serves both the producer and read-time callers.

    TODO Session 8: wire up the daily cron that pre-populates fx_rates so the
        fallback path is rarely hit during normal operation.
    """
    base, quote = _parse_pair(pair)

    rate = await _fetch_from_exchangerate_host(rate_date, base, quote, http_client)
    if rate is not None:
        return rate

    # API failed — try the fallback store.
    if fallback_store is not None:
        fallback = await fallback_store.get_latest_reference_rate_on_or_before(
            on_or_before=rate_date, currency_pair=pair
        )
        if fallback is not None:
            fallback_date, fallback_rate = fallback
            logger.warning(
                "fx_rate_fallback",
                extra={
                    "event": "fx_rate_fallback",
                    "requested_date": rate_date.isoformat(),
                    "fallback_date": fallback_date.isoformat(),
                    "pair": pair,
                },
            )
            return fallback_rate

    raise FXRateNotFoundError(
        f"No reference rate available for {pair} on or before {rate_date.isoformat()}; "
        "API call failed and no fallback rate exists in the store."
    )


async def _fetch_from_exchangerate_host(
    rate_date: date,
    base: str,
    quote: str,
    http_client: httpx.AsyncClient | None,
) -> Decimal | None:
    """
    Single attempt at the live API. Returns None on any failure (network,
    non-200, missing field, parse error). Never raises — the caller decides
    whether to fall back or surface the error.
    """
    url = f"{EXCHANGERATE_HOST_BASE}/{rate_date.isoformat()}"
    params = {"base": base, "symbols": quote}
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=10.0)
    try:
        try:
            response = await client.get(url, params=params)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            return None
        rates = payload.get("rates")
        if not isinstance(rates, dict):
            return None
        raw = rates.get(quote)
        if raw is None:
            return None
        # str() round-trip avoids float→Decimal precision drift if the API
        # returned a JSON number that parsed as a Python float.
        try:
            return Decimal(str(raw))
        except (ArithmeticError, ValueError):
            return None
    finally:
        if owns_client:
            await client.aclose()
