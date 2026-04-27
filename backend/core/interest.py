"""
interest.py — Inter-personal loan interest accrual.

Interest is calculated per lender↔borrower pair, per financial year. Rate
changes apply forward only — never retroactively. The model:

  - Each (lender, borrower) pair has a `current_rate_pct` on
    `interpersonal_loans`. Default is 0%.
  - Rate changes are recorded as INTERPERSONAL_RATE_CHANGE events. The new
    rate applies from the event's `effective_date` forward.
  - Accrued interest is a derived quantity, not stored. Statements are
    generated on demand for tax filing.

Outputs power:
  - US tax reporting (1099-INT-equivalent disclosure when applicable)
  - Indian Income Tax Act compliance (cousins-as-relatives carry favorable
    treatment in many cases — verify with a CA before relying on outputs).

This module never modifies the events table. It only reads.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal


async def calculate_accrued_interest(
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
    fy_start: date,
    fy_end: date,
    db: object | None = None,
) -> Decimal:
    """
    Total interest accrued on the (lender, borrower) loan within
    [fy_start, fy_end] (both inclusive).

    Algorithm (Session 3/6 implementation):
      1. Pull all events for this pair effective at-or-before fy_end.
      2. Pull all INTERPERSONAL_RATE_CHANGE events to build a rate-history
         timeline.
      3. Walk the timeline day-by-day (or interval-by-interval) within
         [fy_start, fy_end], multiplying outstanding-balance * applicable
         daily rate.
      4. Return the sum.

    Day-count convention: simple interest on actual/365.

    TODO Session 3/6: implement.
    """
    raise NotImplementedError("TODO Session 3/6: implement interest accrual")


async def generate_fy_statement(
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
    financial_year: int,
    db: object | None = None,
) -> dict:
    """
    Build a per-financial-year statement for tax filing.

    `financial_year` is the *starting* year of the FY. Indian FY is Apr 1 to
    Mar 31. US tax year is calendar (Jan 1 to Dec 31). This function defaults
    to Indian FY semantics; a `jurisdiction` parameter will be added in a
    later iteration.

    Returns a structured dict shaped like:
        {
            "financial_year": int,
            "fy_start": date,
            "fy_end":   date,
            "opening_balance":          Decimal,
            "closing_balance":          Decimal,
            "total_disbursements":      Decimal,
            "total_repayments":         Decimal,
            "total_interest_accrued":   Decimal,
            "monthly":                  list[dict],  # one row per month with
                                                     # opening, disbursements,
                                                     # repayments, interest, closing
        }

    TODO Session 3/6: implement against the event log + calculate_accrued_interest.
    """
    raise NotImplementedError("TODO Session 3/6: implement FY statement generation")
