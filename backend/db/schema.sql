-- =============================================================================
-- Multi-Party Loan & Settlement Ledger — Schema
-- =============================================================================
--
-- Vanilla PostgreSQL 13+ only. No extensions, no vendor-specific features.
--   - gen_random_uuid() is built into core since pg13 (no pgcrypto extension needed).
--   - Status enums are TEXT + CHECK constraint (not Postgres ENUM types) for
--     migration ergonomics.
--
-- ARCHITECTURAL RULES (enforced by convention; AI agents must not violate):
--   1. The `events` table is APPEND-ONLY. Never UPDATE or DELETE rows.
--   2. No stored balance columns anywhere. Balances are projections derived
--      from event replay (see view stubs at the end of this file).
--   3. Every event row carries an HMAC-SHA256 signature in `hmac_signature`.
--      Canonical field order (frozen, do not change):
--        {id}|{event_type}|{actor_owner_id}|{amount_property_currency}|{effective_date}|{recorded_at}
--   4. Errors are corrected via a COMPENSATING_ENTRY event with
--      `reverses_event_id` pointing at the original. Originals are never edited.
--   5. N-owner generalization: owner count, equity splits, and currencies are
--      runtime config. Nothing in this schema assumes a fixed owner count.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Table: properties
-- -----------------------------------------------------------------------------
-- The asset being co-owned. One row per property. Multiple owners and bank
-- loans can attach to a single property.
-- -----------------------------------------------------------------------------
CREATE TABLE properties (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT        NOT NULL,                                 -- human-friendly name (e.g. "Banjara Hills Flat")
    address             TEXT        NOT NULL,                                 -- street address line(s)
    city                TEXT        NOT NULL,                                 -- city
    state               TEXT,                                                 -- state / province (nullable for jurisdictions w/o)
    country             TEXT        NOT NULL,                                 -- ISO country name or code
    property_currency   TEXT        NOT NULL,                                 -- ISO 4217 currency code, e.g. 'INR'
    purchase_price      NUMERIC(15,2) NOT NULL,                               -- in property_currency
    purchase_date       DATE        NOT NULL,                                 -- date of registration / sale deed
    status              TEXT        NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'sold', 'disputed')), -- lifecycle state
    notes               TEXT,                                                 -- freeform
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()                    -- row insertion timestamp
);

CREATE INDEX idx_properties_status ON properties(status);
CREATE INDEX idx_properties_country ON properties(country);


-- -----------------------------------------------------------------------------
-- Table: owners
-- -----------------------------------------------------------------------------
-- A co-owner of a specific property. The (property_id, email) pair is unique:
-- the same person can co-own multiple properties with separate owner rows.
--
-- NOTE: equity_pct values for a given property should sum to 100. This is
-- enforced at the application layer for now. A future migration may add a
-- DEFERRABLE constraint trigger.
-- -----------------------------------------------------------------------------
CREATE TABLE owners (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id         UUID        NOT NULL REFERENCES properties(id) ON DELETE RESTRICT, -- which property
    display_name        TEXT        NOT NULL,                                 -- human-friendly name
    email               TEXT        NOT NULL,                                 -- magic-link auth identity
    equity_pct          NUMERIC(7,4) NOT NULL,                                -- percentage (e.g. 33.3333). Sum per property should equal 100.
    floor_label         TEXT,                                                 -- usage allocation (e.g. "Floor 2") — separate from equity
    base_currency       TEXT        NOT NULL,                                 -- the currency this owner transacts in (e.g. 'USD')
    joined_at           DATE        NOT NULL,                                 -- date this owner joined the property
    exited_at           DATE,                                                 -- date this owner exited (nullable for active owners)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (property_id, email)
);

CREATE INDEX idx_owners_property ON owners(property_id);
CREATE INDEX idx_owners_email ON owners(email);
CREATE INDEX idx_owners_active ON owners(property_id) WHERE exited_at IS NULL;


-- -----------------------------------------------------------------------------
-- Table: bank_loans
-- -----------------------------------------------------------------------------
-- An external bank loan against a property. A single property can carry
-- multiple concurrent loans (top-up, second mortgage, etc.).
-- -----------------------------------------------------------------------------
CREATE TABLE bank_loans (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id          UUID         NOT NULL REFERENCES properties(id) ON DELETE RESTRICT, -- which property
    lender_name          TEXT         NOT NULL,                               -- e.g. "HDFC Bank"
    loan_account_number  TEXT,                                                -- bank-issued account ref (nullable; some loans omitted from records)
    principal_inr        NUMERIC(15,2) NOT NULL,                              -- original disbursed principal in property currency
    interest_rate_pct    NUMERIC(6,4) NOT NULL,                               -- annualized nominal rate, e.g. 8.5000
    tenure_months        INT          NOT NULL,                               -- total term length in months
    emi_amount           NUMERIC(12,2) NOT NULL,                              -- per-period EMI amount in property currency
    disbursement_date    DATE         NOT NULL,                               -- when the loan was funded
    status               TEXT         NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'closed', 'prepaid')),
    notes                TEXT,                                                -- freeform
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_bank_loans_property ON bank_loans(property_id);
CREATE INDEX idx_bank_loans_status ON bank_loans(status);


-- -----------------------------------------------------------------------------
-- Table: emi_schedule
-- -----------------------------------------------------------------------------
-- Generated amortization rows for a bank loan. One row per scheduled EMI.
-- Status moves forward only; pre/early payments are recorded as additional
-- events in the events table — schedule rows are updated to reflect their
-- final status, but the event log remains the source of truth.
-- -----------------------------------------------------------------------------
CREATE TABLE emi_schedule (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    loan_id              UUID         NOT NULL REFERENCES bank_loans(id) ON DELETE RESTRICT,
    due_date             DATE         NOT NULL,                               -- scheduled due date
    principal_component  NUMERIC(12,2) NOT NULL,                              -- principal portion of this EMI
    interest_component   NUMERIC(12,2) NOT NULL,                              -- interest portion of this EMI
    total_emi            NUMERIC(12,2) NOT NULL,                              -- principal + interest
    status               TEXT         NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'paid', 'prepaid', 'missed')),
    paid_at              TIMESTAMPTZ,                                         -- when this EMI was settled (nullable until paid)
    paid_by_owner_id     UUID         REFERENCES owners(id) ON DELETE RESTRICT, -- which owner ultimately paid (nullable until paid)
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_emi_schedule_loan ON emi_schedule(loan_id);
CREATE INDEX idx_emi_schedule_due_date ON emi_schedule(due_date);
CREATE INDEX idx_emi_schedule_status ON emi_schedule(status);
CREATE INDEX idx_emi_schedule_pending ON emi_schedule(loan_id, due_date) WHERE status = 'pending';


-- -----------------------------------------------------------------------------
-- Table: fx_rates
-- -----------------------------------------------------------------------------
-- Daily FX snapshots from a reference source (RBI, exchangerate.host, etc.).
-- Used as the *reference* rate in dual-rate FX stamping. The *actual* wire
-- rate lives on the event row itself.
-- -----------------------------------------------------------------------------
CREATE TABLE fx_rates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rate_date       DATE         NOT NULL,                                    -- the calendar date this rate is for
    currency_pair   TEXT         NOT NULL,                                    -- e.g. 'USD_INR'
    reference_rate  NUMERIC(12,6) NOT NULL,                                   -- mid-market rate
    source          TEXT         NOT NULL,                                    -- e.g. 'exchangerate.host', 'RBI'
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    UNIQUE (rate_date, currency_pair)
);

CREATE INDEX idx_fx_rates_pair_date ON fx_rates(currency_pair, rate_date DESC);


-- -----------------------------------------------------------------------------
-- Table: interpersonal_loans
-- -----------------------------------------------------------------------------
-- Tracks the relationship between a lender and borrower owner pair on a
-- given property. Stores only the *current* configurable rate; rate history
-- and balance state are derived from the events table.
-- -----------------------------------------------------------------------------
CREATE TABLE interpersonal_loans (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id         UUID         NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
    lender_owner_id     UUID         NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
    borrower_owner_id   UUID         NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
    current_rate_pct    NUMERIC(6,4) NOT NULL DEFAULT 0,                       -- annualized rate; default 0% as per HOUSE_CONTEXT
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),

    UNIQUE (property_id, lender_owner_id, borrower_owner_id),
    CHECK (lender_owner_id <> borrower_owner_id)
);

CREATE INDEX idx_interpersonal_loans_lender ON interpersonal_loans(lender_owner_id);
CREATE INDEX idx_interpersonal_loans_borrower ON interpersonal_loans(borrower_owner_id);


-- =============================================================================
-- Table: events  —  THE CORE APPEND-ONLY LEDGER TABLE
-- =============================================================================
-- Every ledger mutation is recorded as exactly one row here. NEVER updated.
-- NEVER deleted. Errors are corrected by writing a COMPENSATING_ENTRY event
-- linked via `reverses_event_id` to the row being negated.
--
-- Valid `event_type` values (stored as TEXT for portability):
--   CONTRIBUTION                       — equity-building contribution toward principal/down-payment
--   EMI_PAYMENT                        — payment of a scheduled bank EMI
--   BULK_PREPAYMENT                    — lump-sum prepayment against a bank loan
--   INTERPERSONAL_LOAN_DISBURSEMENT    — owner A lends owner B
--   INTERPERSONAL_LOAN_REPAYMENT       — owner B repays owner A
--   INTERPERSONAL_RATE_CHANGE          — change to current_rate_pct on an interpersonal loan (forward-only)
--   SETTLEMENT                         — off-ledger value transfer (Zelle, in-kind, dinner-paid-for)
--   OPEX_EXPENSE                       — shared maintenance expense (property tax, HOA, utilities)
--   OPEX_SPLIT                         — recording of how an OpEx expense splits among owners (see opex_splits)
--   FX_SNAPSHOT                        — manual capture of an FX rate at a point in time
--   EQUITY_ADJUSTMENT                  — one-time t=0 floor-premium adjustment (frozen after)
--   EXIT                               — owner exit / buyout finalization
--   COMPENSATING_ENTRY                 — reversal of a prior event due to data-entry error
-- =============================================================================
CREATE TABLE events (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id                 UUID         NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
    event_type                  TEXT         NOT NULL
                                CHECK (event_type IN (
                                    'CONTRIBUTION', 'EMI_PAYMENT', 'BULK_PREPAYMENT',
                                    'INTERPERSONAL_LOAN_DISBURSEMENT', 'INTERPERSONAL_LOAN_REPAYMENT',
                                    'INTERPERSONAL_RATE_CHANGE', 'SETTLEMENT', 'OPEX_EXPENSE',
                                    'OPEX_SPLIT', 'FX_SNAPSHOT', 'EQUITY_ADJUSTMENT',
                                    'EXIT', 'COMPENSATING_ENTRY'
                                )),                                            -- see enum-as-text comment above
    actor_owner_id              UUID         NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,  -- who initiated this event
    target_owner_id             UUID         REFERENCES owners(id) ON DELETE RESTRICT,           -- counterparty for inter-personal events (nullable)
    loan_id                     UUID         REFERENCES bank_loans(id) ON DELETE RESTRICT,       -- linked bank loan if applicable (nullable)

    -- amounts and FX (all nullable: not every event has a financial value)
    amount_source_currency      NUMERIC(15,2),                                  -- amount in actor's base currency (e.g. USD)
    source_currency             TEXT,                                           -- e.g. 'USD'
    amount_property_currency    NUMERIC(15,2),                                  -- amount in property currency (e.g. INR) — used for balance math
    property_currency           TEXT,                                           -- e.g. 'INR'
    fx_rate_actual              NUMERIC(12,6),                                  -- rate the bank actually applied (drives balance math)
    fx_rate_reference           NUMERIC(12,6),                                  -- mid-market rate on effective_date (for FX gain/loss reporting)
    fee_source_currency         NUMERIC(12,2),                                  -- wire/transfer fee in source currency (sender's cost)
    inr_landed                  NUMERIC(15,2),                                  -- what actually arrived after fees (in property_currency); credits sender's balance

    description                 TEXT         NOT NULL,                          -- human-readable narrative for this event
    metadata                    JSONB        NOT NULL DEFAULT '{}',             -- flexible: EMI period, Form 15CA ref, settlement method, TDS details, etc.

    reverses_event_id           UUID         REFERENCES events(id) ON DELETE RESTRICT, -- set ONLY on COMPENSATING_ENTRY rows

    -- tamper-evidence and provenance
    hmac_signature              TEXT         NOT NULL,                          -- HMAC-SHA256 over canonical fields (see core/events.py)
    recorded_by                 TEXT         NOT NULL,                          -- email or agent id of who wrote this row
    recorded_at                 TIMESTAMPTZ  NOT NULL DEFAULT now(),            -- when the row was inserted (server clock)
    effective_date              DATE         NOT NULL                           -- business date (may differ from recorded_at)
);

-- Indexes for the most common query patterns:
--   - "show all events for property X in date range"
--   - "show all events between owner A and owner B"
--   - "show all events for loan L"
--   - "find all compensating entries for original event E"
CREATE INDEX idx_events_property_effective ON events(property_id, effective_date DESC);
CREATE INDEX idx_events_property_recorded ON events(property_id, recorded_at DESC);
CREATE INDEX idx_events_actor ON events(actor_owner_id);
CREATE INDEX idx_events_target ON events(target_owner_id);
CREATE INDEX idx_events_loan ON events(loan_id);
CREATE INDEX idx_events_type ON events(event_type);
CREATE INDEX idx_events_reverses ON events(reverses_event_id) WHERE reverses_event_id IS NOT NULL;
CREATE INDEX idx_events_pair ON events(actor_owner_id, target_owner_id, effective_date DESC)
    WHERE target_owner_id IS NOT NULL;


-- -----------------------------------------------------------------------------
-- Table: opex_splits
-- -----------------------------------------------------------------------------
-- How an OpEx expense event is divided among owners. One row per (event,
-- owner) pair. The sum of share_pct across rows for a single event should
-- equal 100; enforced at the application layer.
-- -----------------------------------------------------------------------------
CREATE TABLE opex_splits (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id                        UUID         NOT NULL REFERENCES events(id) ON DELETE RESTRICT, -- the OPEX_EXPENSE event being split
    owner_id                        UUID         NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
    share_pct                       NUMERIC(7,4) NOT NULL,                                          -- this owner's share (e.g. 33.3333)
    amount_owed_property_currency   NUMERIC(12,2) NOT NULL,                                         -- this owner's share in property currency
    created_at                      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    UNIQUE (event_id, owner_id)
);

CREATE INDEX idx_opex_splits_event ON opex_splits(event_id);
CREATE INDEX idx_opex_splits_owner ON opex_splits(owner_id);


-- -----------------------------------------------------------------------------
-- Table: market_value_snapshots
-- -----------------------------------------------------------------------------
-- Manual property valuation entries used by the exit scenario calculator.
-- These are not events (no balance impact); they are auxiliary inputs.
-- -----------------------------------------------------------------------------
CREATE TABLE market_value_snapshots (
    id                                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id                         UUID         NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
    estimated_value_property_currency   NUMERIC(15,2) NOT NULL,                  -- the entered valuation, in property_currency
    entered_by_owner_id                 UUID         NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
    valuation_date                      DATE         NOT NULL,                   -- the date the valuation reflects
    notes                               TEXT,                                    -- source of the estimate (broker quote, recent comparable, etc.)
    created_at                          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_market_value_snapshots_property ON market_value_snapshots(property_id, valuation_date DESC);


-- -----------------------------------------------------------------------------
-- Table: documents
-- -----------------------------------------------------------------------------
-- S3-linked document vault. Stores only metadata; the actual file lives in
-- an S3-compatible bucket keyed by `s3_key`.
-- -----------------------------------------------------------------------------
CREATE TABLE documents (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id             UUID         NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
    related_event_id        UUID         REFERENCES events(id) ON DELETE RESTRICT, -- linked event (e.g. the EMI_PAYMENT this receipt belongs to)
    doc_type                TEXT         NOT NULL
                                CHECK (doc_type IN (
                                    'DEED', 'LOAN_SANCTION', 'EMI_RECEIPT',
                                    'FORM_15CA', 'FORM_15CB', 'TDS_CERTIFICATE',
                                    'WIRE_RECEIPT', 'OTHER'
                                )),
    s3_key                  TEXT         NOT NULL,                              -- bucket key
    filename                TEXT         NOT NULL,                              -- original filename (for download)
    uploaded_by_owner_id    UUID         NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
    uploaded_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),                -- when the file was uploaded
    notes                   TEXT,                                               -- freeform
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_documents_property ON documents(property_id);
CREATE INDEX idx_documents_related_event ON documents(related_event_id);
CREATE INDEX idx_documents_doc_type ON documents(doc_type);


-- =============================================================================
-- COMPUTED VIEWS
-- Session 3 — see docs/business-logic/computed-views.md for the contracts.
--
-- These views are the read interface for the ledger. Consumers go through
-- them rather than crafting ad-hoc queries against `events`. The interest-
-- aware balance is intentionally NOT a SQL view — see the Python
-- `get_interpersonal_balance_with_interest` function in core/balance.py
-- (rate-change accrual requires procedural logic).
-- =============================================================================


-- v_interpersonal_balances --------------------------------------------------
-- Net principal balance per (property, lender, borrower) pair, derived from
-- event replay. PRINCIPAL ONLY — accrued interest is layered in by the Python
-- engine. Time-travel queries belong to the Python projection function;
-- this view is the "now" snapshot.
--
-- Sign convention: principal_balance_inr is what borrower_owner_id owes
-- lender_owner_id. Positive = debt; zero = settled; negative = the named
-- lender is actually the debtor.
CREATE OR REPLACE VIEW v_interpersonal_balances AS
WITH pair_events AS (
    -- Outbound: actor=lender lending to target=borrower (DISBURSEMENT, FX context).
    SELECT
        e.property_id,
        e.actor_owner_id   AS lender_owner_id,
        e.target_owner_id  AS borrower_owner_id,
        e.event_type,
        COALESCE(e.amount_property_currency, 0) AS signed_amount,
        e.effective_date
      FROM events e
     WHERE e.target_owner_id IS NOT NULL
       AND e.event_type = 'INTERPERSONAL_LOAN_DISBURSEMENT'

    UNION ALL

    -- Inbound: actor=borrower repaying target=lender. Normalize to lender→borrower
    -- framing with a negative sign.
    --
    -- KNOWN LIMITATION (SETTLEMENT only): this branch assumes actor=borrower and
    -- target=lender. INTERPERSONAL_LOAN_REPAYMENT always satisfies this by
    -- definition. SETTLEMENT can also go the reverse direction (actor=lender
    -- giving value to target=borrower, which INCREASES the borrower's debt).
    -- The SQL view cannot distinguish the two directions without joining
    -- interpersonal_loans to determine the canonical pair — it therefore handles
    -- reverse-direction SETTLEMENT incorrectly (assigns the signed_amount to
    -- the wrong pair bucket). Callers that may have reverse-direction SETTLEMENTs
    -- must use the Python function get_interpersonal_balance() instead of querying
    -- this view directly. See docs/business-logic/computed-views.md.
    SELECT
        e.property_id,
        e.target_owner_id  AS lender_owner_id,
        e.actor_owner_id   AS borrower_owner_id,
        e.event_type,
        -COALESCE(e.amount_property_currency, 0) AS signed_amount,
        e.effective_date
      FROM events e
     WHERE e.target_owner_id IS NOT NULL
       AND e.event_type IN ('INTERPERSONAL_LOAN_REPAYMENT', 'SETTLEMENT')

    UNION ALL

    -- OPEX_SPLIT: actor=share-owner, target=payer; split owes payer.
    -- Skip self-shares (actor=target) since they net to zero.
    SELECT
        e.property_id,
        e.target_owner_id  AS lender_owner_id,
        e.actor_owner_id   AS borrower_owner_id,
        e.event_type,
        COALESCE(e.amount_property_currency, 0) AS signed_amount,
        e.effective_date
      FROM events e
     WHERE e.event_type = 'OPEX_SPLIT'
       AND e.target_owner_id IS NOT NULL
       AND e.actor_owner_id <> e.target_owner_id

    UNION ALL

    -- COMPENSATING_ENTRY rows. metadata.original_event_type drives the
    -- direction; amount_property_currency is already negated by the helper
    -- that created the row.
    SELECT
        e.property_id,
        CASE
            WHEN e.metadata->>'original_event_type' = 'INTERPERSONAL_LOAN_DISBURSEMENT'
                THEN e.actor_owner_id
            ELSE e.target_owner_id
        END AS lender_owner_id,
        CASE
            WHEN e.metadata->>'original_event_type' = 'INTERPERSONAL_LOAN_DISBURSEMENT'
                THEN e.target_owner_id
            ELSE e.actor_owner_id
        END AS borrower_owner_id,
        e.event_type,
        CASE
            WHEN e.metadata->>'original_event_type' = 'INTERPERSONAL_LOAN_DISBURSEMENT'
                -- amount_property_currency is already negated → direct use cancels
                -- the original positive disbursement credit.
                THEN COALESCE(e.amount_property_currency, 0)
            WHEN e.metadata->>'original_event_type' = 'OPEX_SPLIT'
                -- OPEX_SPLIT original was positive (actor owes target); compensating
                -- entry amount is already negated → direct use cancels the original
                -- positive debt. Double-negating would double the debt instead.
                THEN COALESCE(e.amount_property_currency, 0)
            ELSE
                -- INTERPERSONAL_LOAN_REPAYMENT and SETTLEMENT: original was stored
                -- as negative in the view (amount negated). The compensating entry
                -- amount is also negated, so flip once to produce a positive value
                -- that cancels the negative original.
                -COALESCE(e.amount_property_currency, 0)
        END AS signed_amount,
        e.effective_date
      FROM events e
     WHERE e.event_type = 'COMPENSATING_ENTRY'
       AND e.target_owner_id IS NOT NULL
       AND e.metadata->>'original_event_type' IN (
           'INTERPERSONAL_LOAN_DISBURSEMENT',
           'INTERPERSONAL_LOAN_REPAYMENT',
           'SETTLEMENT',
           'OPEX_SPLIT'
       )
)
SELECT
    property_id,
    lender_owner_id,
    borrower_owner_id,
    SUM(signed_amount)::NUMERIC(15,2) AS principal_balance_inr,
    MAX(effective_date)               AS last_event_date
  FROM pair_events
 GROUP BY property_id, lender_owner_id, borrower_owner_id;


-- v_bank_loan_balances ------------------------------------------------------
-- Outstanding principal per bank loan derived entirely from the events table.
-- Includes COMPENSATING_ENTRY rows that reverse EMI_PAYMENT or BULK_PREPAYMENT
-- so that append-only corrections immediately update the outstanding balance.
--
-- principal_delta semantics: positive = reduction in outstanding (payment or
-- prepayment); negative = restoration (compensating entry reversal). The
-- view computes outstanding = original_principal - SUM(deltas).
CREATE OR REPLACE VIEW v_bank_loan_balances AS
WITH loan_principal_events AS (
    -- EMI_PAYMENT: principal_component is stored in metadata.
    SELECT
        e.loan_id,
        COALESCE((e.metadata->>'principal_component')::NUMERIC, 0) AS principal_delta,
        e.effective_date
      FROM events e
     WHERE e.event_type = 'EMI_PAYMENT'
       AND e.loan_id IS NOT NULL

    UNION ALL

    -- BULK_PREPAYMENT: full amount reduces principal.
    SELECT
        e.loan_id,
        COALESCE(e.amount_property_currency, 0) AS principal_delta,
        e.effective_date
      FROM events e
     WHERE e.event_type = 'BULK_PREPAYMENT'
       AND e.loan_id IS NOT NULL

    UNION ALL

    -- COMPENSATING_ENTRY reversing EMI_PAYMENT: metadata.principal_component
    -- is already negated by build_compensating_entry → negative delta restores
    -- the principal that the original EMI_PAYMENT had reduced.
    SELECT
        e.loan_id,
        COALESCE((e.metadata->>'principal_component')::NUMERIC, 0) AS principal_delta,
        e.effective_date
      FROM events e
     WHERE e.event_type = 'COMPENSATING_ENTRY'
       AND e.loan_id IS NOT NULL
       AND e.metadata->>'original_event_type' = 'EMI_PAYMENT'

    UNION ALL

    -- COMPENSATING_ENTRY reversing BULK_PREPAYMENT: amount_property_currency
    -- is already negated → negative delta restores the prepaid principal.
    SELECT
        e.loan_id,
        COALESCE(e.amount_property_currency, 0) AS principal_delta,
        e.effective_date
      FROM events e
     WHERE e.event_type = 'COMPENSATING_ENTRY'
       AND e.loan_id IS NOT NULL
       AND e.metadata->>'original_event_type' = 'BULK_PREPAYMENT'
)
SELECT
    bl.id                                                       AS loan_id,
    bl.lender_name,
    bl.principal_inr                                            AS original_principal_inr,
    COALESCE(SUM(lpe.principal_delta), 0)::NUMERIC(15,2)        AS total_paid_principal_inr,
    GREATEST(
        bl.principal_inr - COALESCE(SUM(lpe.principal_delta), 0),
        0
    )::NUMERIC(15,2)                                            AS outstanding_principal_inr,
    MAX(lpe.effective_date)                                     AS last_payment_date
  FROM bank_loans bl
  LEFT JOIN loan_principal_events lpe ON lpe.loan_id = bl.id
 GROUP BY bl.id, bl.lender_name, bl.principal_inr;


-- v_owner_contributions -----------------------------------------------------
-- Per-owner CapEx + OpEx contribution totals in property currency.
-- CapEx aggregates CONTRIBUTION (full amount), EMI_PAYMENT (principal only),
-- BULK_PREPAYMENT (full amount), and COMPENSATING_ENTRY rows that reverse
-- any of those (which carry negated amounts and thus reduce the totals).
-- OpEx aggregates the owner's share of OpEx expenses via opex_splits.
--
-- Cross-currency events are credited at inr_landed; same-currency at
-- amount_property_currency. COALESCE applies the dual-rate rule.
-- For EMI_PAYMENT (and its compensating entries), only the principal component
-- counts — the interest component is the bank's revenue, not a contribution.
CREATE OR REPLACE VIEW v_owner_contributions AS
WITH capex AS (
    SELECT
        e.actor_owner_id     AS owner_id,
        e.property_id,
        SUM(
            CASE
                WHEN e.event_type = 'EMI_PAYMENT'
                  OR (e.event_type = 'COMPENSATING_ENTRY'
                      AND e.metadata->>'original_event_type' = 'EMI_PAYMENT')
                    -- principal_component is negated on compensating entries
                    -- → automatically reduces the sum.
                    THEN COALESCE((e.metadata->>'principal_component')::NUMERIC, 0)
                ELSE
                    -- CONTRIBUTION, BULK_PREPAYMENT, and their compensating
                    -- entries. inr_landed and amount_property_currency are
                    -- negated on compensating entries → reduces the sum.
                    COALESCE(e.inr_landed, e.amount_property_currency, 0)
            END
        ) AS capex_inr
      FROM events e
     WHERE e.event_type IN ('CONTRIBUTION', 'EMI_PAYMENT', 'BULK_PREPAYMENT')
        OR (e.event_type = 'COMPENSATING_ENTRY'
            AND e.metadata->>'original_event_type' IN (
                'CONTRIBUTION', 'EMI_PAYMENT', 'BULK_PREPAYMENT'
            ))
     GROUP BY e.actor_owner_id, e.property_id
),
opex AS (
    SELECT
        os.owner_id,
        e.property_id,
        SUM(os.amount_owed_property_currency) AS opex_inr
      FROM opex_splits os
      JOIN events e ON e.id = os.event_id
     GROUP BY os.owner_id, e.property_id
)
SELECT
    COALESCE(c.owner_id, o.owner_id)                          AS owner_id,
    COALESCE(c.property_id, o.property_id)                    AS property_id,
    COALESCE(c.capex_inr, 0)::NUMERIC(15,2)                   AS capex_inr,
    COALESCE(o.opex_inr, 0)::NUMERIC(15,2)                    AS opex_inr,
    (COALESCE(c.capex_inr, 0) + COALESCE(o.opex_inr, 0))::NUMERIC(15,2) AS total_inr
  FROM capex c
  FULL OUTER JOIN opex o
    ON o.owner_id = c.owner_id AND o.property_id = c.property_id;


-- v_emi_upcoming ------------------------------------------------------------
-- Next pending EMIs across active loans, with the assigned payer if any.
-- Callers typically `ORDER BY due_date ASC LIMIT N`. The
-- idx_emi_schedule_pending partial index covers the filter.
CREATE OR REPLACE VIEW v_emi_upcoming AS
SELECT
    es.loan_id,
    bl.lender_name,
    es.due_date,
    es.principal_component,
    es.interest_component,
    es.total_emi          AS total_emi_inr,
    es.paid_by_owner_id
  FROM emi_schedule es
  JOIN bank_loans   bl ON bl.id = es.loan_id
 WHERE es.status = 'pending';
