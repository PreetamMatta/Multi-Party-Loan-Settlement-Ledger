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

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


class FXRateNotFoundError(Exception):
    """Raised when no reference rate is available for a (date, pair)."""


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


async def fetch_reference_rate(
    rate_date: date,
    pair: str = "USD_INR",
    db: object | None = None,
) -> Decimal:
    """
    Look up the reference rate for a given date and currency pair.

    Returns the `reference_rate` from the `fx_rates` table.

    Raises:
        FXRateNotFoundError: if no row exists for (rate_date, pair).

    TODO Session 3: implement against asyncpg / SQLAlchemy session.
        - Query: SELECT reference_rate FROM fx_rates
                 WHERE rate_date = $1 AND currency_pair = $2 LIMIT 1;
        - On miss: log a warning and raise FXRateNotFoundError.
        - Wire up the daily exchangerate.host snapshot job (separate cron).
    """
    raise NotImplementedError(
        "TODO Session 3: implement reference rate lookup against fx_rates table"
    )
