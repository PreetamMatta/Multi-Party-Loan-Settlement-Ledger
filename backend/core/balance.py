"""
balance.py — Balance projection engine.

ARCHITECTURAL RULE: This module NEVER reads from a stored balance column.
All balances are computed by replaying the append-only event log.

The four pure-projection functions and one composite (interest-aware)
function in this module are the read interface for the ledger. They
consume the routing contract in `core/events.py.get_financial_effect()`
so balance math is centralized in one place — adding a new event type
only requires updating the router, not every projection function.

See docs/business-logic/balances-and-equity.md and
docs/business-logic/computed-views.md for the contracts.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from core.events import EventType, LedgerEvent, get_financial_effect
from core.interest import calculate_accrued_interest


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------
def _row_to_event(row: Any) -> LedgerEvent:
    """
    Reconstruct a LedgerEvent from an asyncpg.Record (or any mapping with the
    same column names). Used by the projection paths to feed `get_financial_effect`.

    The HMAC signature is intentionally NOT re-verified here — verification is
    a separate audit concern; balance math trusts the log.
    """
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata) if metadata else {}
    elif metadata is None:
        metadata = {}

    recorded_at = row["recorded_at"]
    if isinstance(recorded_at, str):
        recorded_at = datetime.fromisoformat(recorded_at)

    return LedgerEvent(
        id=row["id"],
        property_id=row["property_id"],
        event_type=EventType(row["event_type"]),
        actor_owner_id=row["actor_owner_id"],
        target_owner_id=row["target_owner_id"],
        loan_id=row["loan_id"],
        amount_source_currency=row["amount_source_currency"],
        source_currency=row["source_currency"],
        amount_property_currency=row["amount_property_currency"],
        property_currency=row["property_currency"],
        fx_rate_actual=row["fx_rate_actual"],
        fx_rate_reference=row["fx_rate_reference"],
        fee_source_currency=row["fee_source_currency"],
        inr_landed=row["inr_landed"],
        description=row["description"],
        metadata=metadata,
        reverses_event_id=row["reverses_event_id"],
        hmac_signature=row["hmac_signature"],
        recorded_by=row["recorded_by"],
        recorded_at=recorded_at,
        effective_date=row["effective_date"],
    )


_EVENT_COLUMNS = (
    "id, property_id, event_type, actor_owner_id, target_owner_id, loan_id, "
    "amount_source_currency, source_currency, amount_property_currency, "
    "property_currency, fx_rate_actual, fx_rate_reference, fee_source_currency, "
    "inr_landed, description, metadata, reverses_event_id, hmac_signature, "
    "recorded_by, recorded_at, effective_date"
)


def _events_to_pair_balance(
    events: Iterable[LedgerEvent],
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
) -> Decimal:
    """
    Fold the event stream into a net (borrower owes lender) principal balance.

    The router returns deltas in normalized lender→borrower framing. If a
    routed effect's lender/borrower matches the requested pair direction we
    add; if it matches the reverse direction we subtract; otherwise we ignore
    (events touching only one of the pair members but not both).
    """
    balance = Decimal("0")
    for event in events:
        effect = get_financial_effect(event)
        ip = effect.get("interpersonal")
        if ip is None:
            continue
        if ip["lender"] == lender_id and ip["borrower"] == borrower_id:
            balance += ip["delta"]
        elif ip["lender"] == borrower_id and ip["borrower"] == lender_id:
            balance -= ip["delta"]
    return balance


# -----------------------------------------------------------------------------
# Spec: docs/business-logic/balances-and-equity.md#python-api
# -----------------------------------------------------------------------------
async def get_interpersonal_balance(
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
    as_of_date: date,
    db: Any,
) -> Decimal:
    """
    Net principal owed by `borrower_id` to `lender_id` as of `as_of_date`.

    Spec: docs/business-logic/balances-and-equity.md#projection-contract and
          docs/business-logic/interpersonal-loans.md#balance-computation

    Positive: borrower owes lender. Zero: settled. Negative: the labels are
    reversed (the named lender is actually the debtor).

    PRINCIPAL ONLY — does not include accrued interest. For the combined
    figure use `get_interpersonal_balance_with_interest`.

    Implementation: pull every event whose (actor, target) pair touches both
    owners, in either direction, ordered `effective_date ASC, recorded_at ASC`,
    filtered to `effective_date <= as_of_date`. Run each through the
    centralized router and fold the deltas. COMPENSATING_ENTRY rows are
    handled by the router (they dispatch through their parent's framing with
    pre-negated amounts) — no special case here.
    """
    rows = await db.fetch(
        f"""
        SELECT {_EVENT_COLUMNS}
          FROM events
         WHERE effective_date <= $3
           AND target_owner_id IS NOT NULL
           AND (
                (actor_owner_id = $1 AND target_owner_id = $2)
             OR (actor_owner_id = $2 AND target_owner_id = $1)
           )
         ORDER BY effective_date ASC, recorded_at ASC
        """,
        lender_id,
        borrower_id,
        as_of_date,
    )
    events = [_row_to_event(row) for row in rows]
    return _events_to_pair_balance(events, lender_id, borrower_id)


# -----------------------------------------------------------------------------
# Spec: docs/business-logic/balances-and-equity.md#bank-loan-balance
# -----------------------------------------------------------------------------
async def get_loan_balance(
    loan_id: uuid.UUID,
    as_of_date: date,
    db: Any,
) -> Decimal:
    """
    Outstanding principal on a single bank loan as of `as_of_date`.

    Spec: docs/business-logic/balances-and-equity.md#bank-loan-balance

    Computed as: `bank_loans.principal_inr` + sum of all `bank_loan.delta`
    values returned by the router for events touching this loan within the
    date window. Router deltas are negative for principal reductions
    (EMI principal component, BULK_PREPAYMENT) and positive for
    COMPENSATING_ENTRY rows reversing those.

    Floored at `Decimal('0')`: a negative outstanding indicates a data-entry
    error (over-payment recorded), and surfacing it as zero matches what an
    operator can act on without confusing the UI.
    """
    original = await db.fetchval(
        "SELECT principal_inr FROM bank_loans WHERE id = $1",
        loan_id,
    )
    if original is None:
        return Decimal("0")

    rows = await db.fetch(
        f"""
        SELECT {_EVENT_COLUMNS}
          FROM events
         WHERE loan_id = $1
           AND effective_date <= $2
         ORDER BY effective_date ASC, recorded_at ASC
        """,
        loan_id,
        as_of_date,
    )

    delta_total = Decimal("0")
    for row in rows:
        event = _row_to_event(row)
        effect = get_financial_effect(event)
        bank_loan = effect.get("bank_loan")
        if bank_loan is not None and bank_loan["loan_id"] == loan_id:
            delta_total += bank_loan["delta"]

    outstanding = Decimal(original) + delta_total
    return outstanding if outstanding > 0 else Decimal("0")


# -----------------------------------------------------------------------------
# Spec: docs/business-logic/balances-and-equity.md#contribution-total-per-owner
# -----------------------------------------------------------------------------
async def get_owner_contributions(
    owner_id: uuid.UUID,
    property_id: uuid.UUID,
    as_of_date: date,
    db: Any,
) -> dict[str, Any]:
    """
    Total amount this owner has contributed up to `as_of_date`, in property
    currency equivalent, split CapEx vs OpEx.

    Spec: docs/business-logic/balances-and-equity.md#contribution-total-per-owner

    Returns:
        {
            "capex_inr":   Decimal,  # CONTRIBUTION + EMI principal + BULK_PREPAYMENT
            "opex_inr":    Decimal,  # owner's share of OPEX_SPLIT rows
            "total_inr":   Decimal,  # capex + opex
            "event_count": int,      # rows that contributed (audit/UI hint)
        }

    Currency rule: cross-currency events are credited at `inr_landed`
    (already enforced inside the router), same-currency events at
    `amount_property_currency`. The router applies the rule uniformly so
    callers do not branch here.
    """
    rows = await db.fetch(
        f"""
        SELECT {_EVENT_COLUMNS}
          FROM events
         WHERE property_id = $1
           AND actor_owner_id = $2
           AND effective_date <= $3
         ORDER BY effective_date ASC, recorded_at ASC
        """,
        property_id,
        owner_id,
        as_of_date,
    )

    capex = Decimal("0")
    opex = Decimal("0")
    event_count = 0
    for row in rows:
        event = _row_to_event(row)
        effect = get_financial_effect(event)
        capex_eff = effect.get("owner_capex")
        opex_eff = effect.get("owner_opex")
        contributed = False
        if capex_eff is not None and capex_eff["owner_id"] == owner_id:
            capex += capex_eff["delta"]
            contributed = True
        if opex_eff is not None and opex_eff["owner_id"] == owner_id:
            opex += opex_eff["delta"]
            contributed = True
        if contributed:
            event_count += 1

    return {
        "capex_inr": capex,
        "opex_inr": opex,
        "total_inr": capex + opex,
        "event_count": event_count,
    }


# -----------------------------------------------------------------------------
# Spec: docs/business-logic/exit-scenarios.md
# -----------------------------------------------------------------------------
async def project_exit_scenario(
    owner_id: uuid.UUID,
    property_id: uuid.UUID,
    market_value_property_currency: Decimal,
    db: Any,
    blend_weight_contribution: Decimal = Decimal("0.5"),
    blend_weight_market: Decimal = Decimal("0.5"),
) -> dict[str, Any]:
    """
    Compute the three buyout numbers for `owner_id`.

    Spec: docs/business-logic/exit-scenarios.md

    Returns a dict with the three numbers, the inputs used (for auditability),
    and a warning string when relevant inputs are missing.

    Buyout #1 (`buyout_net_contribution`):
        net_capex - debts_owed_to_others + credits_owed_by_others
        # TODO Session 6: layer CPI inflation adjustment on the capex term
        # so contributions made in 2026 are compared to today's purchasing
        # power. v1 returns nominal contribution; that is honest but understates
        # what the contributor "really" gave up.

    Buyout #2 (`buyout_market_value_share`):
        market_value * (equity_pct / 100)

    Buyout #3 (`buyout_weighted_blend`):
        weight_c * Buyout1 + weight_m * Buyout2
        Default weights 50/50; callers can override.
    """
    today = date.today()

    equity_pct_raw = await db.fetchval(
        "SELECT equity_pct FROM owners WHERE id = $1 AND property_id = $2",
        owner_id,
        property_id,
    )
    if equity_pct_raw is None:
        equity_pct = Decimal("0")
        warning: str | None = "Owner not found on property; equity_pct defaulted to 0."
    else:
        equity_pct = Decimal(equity_pct_raw)
        warning = None

    contributions = await get_owner_contributions(owner_id, property_id, today, db)
    capex = contributions["capex_inr"]

    # Find every counterparty this owner has had any inter-personal interaction
    # with, in either direction. We intentionally do not pre-filter to non-zero
    # balances — a legitimate zero is still informative for the audit dict.
    counterparty_rows = await db.fetch(
        """
        SELECT DISTINCT counterparty
          FROM (
                SELECT target_owner_id AS counterparty
                  FROM events
                 WHERE property_id = $2
                   AND actor_owner_id = $1
                   AND target_owner_id IS NOT NULL
                UNION
                SELECT actor_owner_id AS counterparty
                  FROM events
                 WHERE property_id = $2
                   AND target_owner_id = $1
                   AND actor_owner_id IS NOT NULL
          ) AS pairs
         WHERE counterparty <> $1
        """,
        owner_id,
        property_id,
    )

    debts_owed = Decimal("0")
    credits_due = Decimal("0")
    for row in counterparty_rows:
        cp = row["counterparty"]
        # owner as borrower → other as lender → "what owner owes other"
        owed_by_owner = await get_interpersonal_balance(cp, owner_id, today, db)
        # owner as lender → other as borrower → "what other owes owner"
        owed_to_owner = await get_interpersonal_balance(owner_id, cp, today, db)
        if owed_by_owner > 0:
            debts_owed += owed_by_owner
        if owed_to_owner > 0:
            credits_due += owed_to_owner

    buyout_net_contribution = capex - debts_owed + credits_due
    buyout_market_value_share = market_value_property_currency * equity_pct / Decimal("100")
    buyout_weighted_blend = (
        blend_weight_contribution * buyout_net_contribution
        + blend_weight_market * buyout_market_value_share
    )

    return {
        "owner_id": owner_id,
        "as_of_date": today,
        "equity_pct": equity_pct,
        "buyout_net_contribution": buyout_net_contribution,
        "buyout_market_value_share": buyout_market_value_share,
        "buyout_weighted_blend": buyout_weighted_blend,
        "blend_weights": {
            "contribution": blend_weight_contribution,
            "market": blend_weight_market,
        },
        "inputs": {
            "capex_inr": capex,
            "debts_owed_inr": debts_owed,
            "credits_due_inr": credits_due,
            "market_value_property_currency": market_value_property_currency,
        },
        "warning": warning,
    }


# -----------------------------------------------------------------------------
# Spec: docs/business-logic/computed-views.md#get_interpersonal_balance_with_interest-python
# -----------------------------------------------------------------------------
async def get_interpersonal_balance_with_interest(
    lender_id: uuid.UUID,
    borrower_id: uuid.UUID,
    as_of_date: date,
    db: Any,
) -> dict[str, Any]:
    """
    Combined principal + accrued-interest view of an inter-personal balance.

    Lives in Python rather than SQL because rate-change accrual requires
    procedural logic. See docs/business-logic/computed-views.md.
    """
    principal = await get_interpersonal_balance(lender_id, borrower_id, as_of_date, db)
    # Inception is just the earliest possible date — the engine clips to the
    # event history regardless. We pass `as_of_date` as both ends of the
    # window when we want everything-to-date, but `calculate_accrued_interest`
    # is parameterized as `[period_start, period_end]` so we use a far-past
    # sentinel and let the engine itself find the first event.
    accrued = await calculate_accrued_interest(lender_id, borrower_id, date.min, as_of_date, db)
    return {
        "lender_id": lender_id,
        "borrower_id": borrower_id,
        "as_of_date": as_of_date,
        "principal_inr": principal,
        "accrued_interest_inr": accrued,
        "total_owed_inr": principal + accrued,
    }
