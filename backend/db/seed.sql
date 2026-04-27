-- =============================================================================
-- SEED DATA: Example data only. V, P, S are illustrative owners, not system
-- entities. The application is generic and supports any group of N co-owners
-- with arbitrary currencies and equity splits.
--
-- This file is for local development only. Never run on production without
-- clearing first.
--
-- To reset:
--     DROP SCHEMA public CASCADE; CREATE SCHEMA public;
-- then re-apply schema.sql followed by this seed.sql.
-- =============================================================================

-- One example property: a flat in Hyderabad. Property currency is INR.
WITH new_property AS (
    INSERT INTO properties (name, address, city, state, country, property_currency, purchase_price, purchase_date, notes)
    VALUES (
        'Banjara Hills Flat',
        '12-3-456, Road No. 7',
        'Hyderabad',
        'Telangana',
        'India',
        'INR',
        25000000.00,                 -- 2.5 crore INR (example)
        '2025-09-15',
        'Three-cousin co-purchase. Floor allocation: V=top, P=middle, S=ground.'
    )
    RETURNING id
),
-- Three example owners. V is Hyderabad-based and transacts in INR; P and S
-- are US-based and transact in USD. Equity is approximately equal — note
-- the slight 33.3334 / 33.3333 / 33.3333 split to total exactly 100.0000.
new_owners AS (
    INSERT INTO owners (property_id, display_name, email, equity_pct, floor_label, base_currency, joined_at)
    SELECT id, display_name, email, equity_pct, floor_label, base_currency, joined_at
    FROM new_property,
    (VALUES
        ('Owner V (example)', 'v@example.com', 33.3334::NUMERIC, 'Top floor',    'INR', DATE '2025-09-15'),
        ('Owner P (example)', 'p@example.com', 33.3333::NUMERIC, 'Middle floor', 'USD', DATE '2025-09-15'),
        ('Owner S (example)', 's@example.com', 33.3333::NUMERIC, 'Ground floor', 'USD', DATE '2025-09-15')
    ) AS o(display_name, email, equity_pct, floor_label, base_currency, joined_at)
    RETURNING id
)
SELECT count(*) AS owners_seeded FROM new_owners;

-- One example bank loan against the property.
INSERT INTO bank_loans (property_id, lender_name, loan_account_number, principal_inr, interest_rate_pct, tenure_months, emi_amount, disbursement_date, notes)
SELECT id, 'HDFC Bank', 'HDFC-EXAMPLE-001', 15000000.00, 8.5000, 240, 130172.00, '2025-09-20', 'Example: 1.5cr loan, 20yr tenure, ~8.5% interest.'
FROM properties
WHERE name = 'Banjara Hills Flat';

-- One example FX snapshot (used by future events).
INSERT INTO fx_rates (rate_date, currency_pair, reference_rate, source)
VALUES ('2025-09-15', 'USD_INR', 83.250000, 'exchangerate.host (example)');

-- Establish interpersonal loan relationships (default 0% rate per HOUSE_CONTEXT).
-- Pairs: V↔P and V↔S in both directions; P↔S in both directions.
INSERT INTO interpersonal_loans (property_id, lender_owner_id, borrower_owner_id, current_rate_pct)
SELECT p.id, lender.id, borrower.id, 0
FROM properties p
JOIN owners lender    ON lender.property_id   = p.id
JOIN owners borrower  ON borrower.property_id = p.id AND borrower.id <> lender.id
WHERE p.name = 'Banjara Hills Flat';
