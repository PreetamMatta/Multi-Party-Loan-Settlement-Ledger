"""
balance.py — Balance projection engine.

ARCHITECTURAL RULE: This module NEVER reads from a stored balance column.
All balances are computed by replaying the append-only event log.

This enables time-travel queries: "What did Owner A owe Owner B on 2031-03-15?"
The cost is a forward replay over the event log per query — acceptable at
this scale, and trivially cacheable per (as_of_date) snapshot if needed.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal


async def get_interpersonal_balance(
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
    as_of_date: date,
    db: object | None = None,
) -> Decimal:
    """
    Net amount owed by `borrower_id` to `lender_id` as of `as_of_date`.

    Positive: borrower owes lender. Zero: settled. Negative: lender owes
    borrower (which means the labels should be reversed in the UI).

    Replays:
      - INTERPERSONAL_LOAN_DISBURSEMENT (lender → borrower)        : +amount
      - INTERPERSONAL_LOAN_REPAYMENT     (borrower → lender)       : -amount
      - SETTLEMENT                       (off-ledger value xfer)   : ±amount
      - Accrued interest at the rate-history-aware schedule        : +amount
      - COMPENSATING_ENTRY rows that target any of the above       : negation

    TODO Session 3: implement event replay + interest accrual interleaving.
        - Pull all events touching this (lender, borrower) pair on or before
          as_of_date, ordered by effective_date then recorded_at.
        - Apply each event's signed amount_property_currency to a running total.
        - Cross-reference interpersonal_loans.current_rate_pct and any
          INTERPERSONAL_RATE_CHANGE events to compute accrued interest
          forward-only between rate change boundaries.
        - Honor reverses_event_id by skipping pairs that net to zero — or
          equivalently, sum them all (the math is identical because compensating
          entries are stored as negations).
    """
    raise NotImplementedError("TODO Session 3: implement interpersonal balance projection")


async def get_loan_balance(
    loan_id: uuid.UUID,
    as_of_date: date,
    db: object | None = None,
) -> Decimal:
    """
    Outstanding principal on a bank loan as of `as_of_date`.

    Replays:
      - The original principal at disbursement
      - EMI_PAYMENT events       : -principal_component (interest portion does
                                   not reduce principal)
      - BULK_PREPAYMENT events   : -amount
      - COMPENSATING_ENTRY rows  : negation as appropriate

    TODO Session 3: implement event replay against bank_loans + events.
    """
    raise NotImplementedError("TODO Session 3: implement bank loan balance projection")


async def get_owner_contributions(
    owner_id: uuid.UUID,
    property_id: uuid.UUID,
    as_of_date: date,
    db: object | None = None,
) -> dict[str, Decimal]:
    """
    Total amount this owner has contributed up to `as_of_date`, in property
    currency equivalent.

    Returns:
        {
            "capex_property_currency": Decimal,  # CONTRIBUTION + EMI principal portions paid
            "opex_property_currency":  Decimal,  # share of OPEX_EXPENSE / OPEX_SPLIT events
            "total_property_currency": Decimal,  # capex + opex
        }

    All amounts are stamped at their event's actual FX rate (inr_landed
    where applicable), per the dual-rate rule.

    TODO Session 3: implement event replay + opex_splits aggregation.
    """
    raise NotImplementedError("TODO Session 3: implement owner contributions projection")


async def project_exit_scenario(
    owner_id: uuid.UUID,
    property_id: uuid.UUID,
    market_value_property_currency: Decimal,
    db: object | None = None,
) -> dict[str, Decimal]:
    """
    Compute the three buyout numbers for this owner per HOUSE_CONTEXT.md.

    Returns:
        {
            "net_contribution_buyout": Decimal,
                # Total this owner has contributed (CapEx + OpEx if applicable),
                # adjusted for historical inflation in the property currency.
                # Reflects "what they put in, in today's purchasing power".

            "market_value_share":      Decimal,
                # This owner's equity_pct of `market_value_property_currency`,
                # less their share of any outstanding bank loan principal.
                # Reflects "what their slice of the asset is worth today".

            "weighted_blend":          Decimal,
                # A configurable weighted blend of the two above. Default
                # weighting (50/50) is a starting point; the UI lets humans
                # adjust the slider before deciding.
        }

    The app surfaces these three numbers side-by-side. It does NOT pick a
    winner — humans decide which formula governs the buyout.

    TODO Session 6: implement; depends on get_owner_contributions(),
        get_loan_balance() (across all property loans), and a CPI-adjusted
        net-contribution calculator (use a configurable inflation index).
    """
    raise NotImplementedError("TODO Session 6: implement exit scenario projection")
