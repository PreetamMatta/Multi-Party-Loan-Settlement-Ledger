"""
tools.py — FastMCP tool surface.

These tools expose the ledger as MCP-compatible endpoints, enabling
conversational logging:

    "Log that P sent V $500 via Zelle yesterday."

Implemented in Session 5. The signatures below are the public contract — any
change to a parameter name or shape after Session 5 lands is a breaking
change for any agent or chat client integrated against the surface.

TODO Session 5: register these as @mcp.tool() decorators on a FastMCP
instance, wire up the underlying core/ + db/ services, and add an MCP
entrypoint script under backend/mcp/server.py.
"""

from __future__ import annotations

from typing import Optional


def get_balance(
    lender: str,
    borrower: str,
    as_of_date: Optional[str] = None,
) -> dict:
    """
    What does `borrower` owe `lender` (interpersonal balance)?

    Args:
        lender:     Email of the lender owner.
        borrower:   Email of the borrower owner.
        as_of_date: ISO date string (YYYY-MM-DD). If None, uses today.

    Returns:
        {
            "lender_email": str,
            "borrower_email": str,
            "as_of_date": str,
            "balance_property_currency": str,   # Decimal as string
            "property_currency": str,
        }
    """
    raise NotImplementedError("TODO Session 5")


def record_payment(
    from_owner: str,
    to_owner_or_loan: str,
    amount_source_currency: float,
    source_currency: str,
    amount_property_currency: float,
    fx_rate_actual: float,
    fx_rate_reference: float,
    fee_source_currency: float,
    effective_date: str,
    description: str,
) -> dict:
    """
    Record a payment event (CONTRIBUTION / EMI_PAYMENT / INTERPERSONAL_LOAN_REPAYMENT).

    The event_type is inferred from `to_owner_or_loan`: if it matches an
    owner email, the event is interpersonal; if it matches a bank loan id,
    the event is an EMI_PAYMENT or BULK_PREPAYMENT (further disambiguated
    by amount).

    Args:
        from_owner:                Email of the paying owner.
        to_owner_or_loan:          Either an owner email or a bank loan UUID.
        amount_source_currency:    Gross sent amount in source currency.
        source_currency:           ISO code, e.g. 'USD'.
        amount_property_currency:  Gross amount in property currency at fx_rate_actual.
        fx_rate_actual:            Bank's applied rate (drives balance math).
        fx_rate_reference:         Mid-market reference rate (for FX gain/loss).
        fee_source_currency:       Wire/transfer fee in source currency.
        effective_date:            ISO date the payment is effective.
        description:               Human-readable narrative.

    Returns:
        {"event_id": str, "hmac_signature": str, "summary": str}

    NOTE: floats appear here only as the MCP wire format. Internally, the
    handler must convert to Decimal IMMEDIATELY before doing any math.
    """
    raise NotImplementedError("TODO Session 5")


def simulate_exit(
    owner_email: str,
    property_id: str,
    market_value_property_currency: float,
) -> dict:
    """
    Compute the three buyout numbers for an owner exit scenario.

    Args:
        owner_email:                       Email of the exiting owner.
        property_id:                       UUID of the property.
        market_value_property_currency:    Manually-supplied current market value.

    Returns:
        {
            "owner_email": str,
            "property_id": str,
            "net_contribution_buyout": str,   # Decimal as string
            "market_value_share":      str,
            "weighted_blend":          str,
            "as_of": str,                     # ISO timestamp
        }
    """
    raise NotImplementedError("TODO Session 5")


def log_settlement(
    from_owner: str,
    to_owner: str,
    value_property_currency: float,
    method: str,
    description: str,
    effective_date: str,
) -> dict:
    """
    Record an off-ledger SETTLEMENT event (Zelle, in-kind, dinner-paid-for).

    Args:
        from_owner:               Email of the owner who paid.
        to_owner:                 Email of the recipient.
        value_property_currency:  Equivalent value in property currency.
        method:                   Free-form: 'zelle', 'cash', 'flight_ticket', ...
        description:              Human-readable narrative.
        effective_date:           ISO date the settlement is effective.

    Returns:
        {"event_id": str, "hmac_signature": str, "summary": str}
    """
    raise NotImplementedError("TODO Session 5")


def get_fx_rate(rate_date: str, pair: str = "USD_INR") -> dict:
    """
    Look up the reference FX rate for a date / currency pair.

    Args:
        rate_date: ISO date string.
        pair:      Currency pair, default 'USD_INR'.

    Returns:
        {
            "rate_date": str,
            "pair": str,
            "reference_rate": str,   # Decimal as string
            "source": str,
        }

    Raises (in implementation):
        FXRateNotFoundError if no snapshot exists for that date.
    """
    raise NotImplementedError("TODO Session 5")
