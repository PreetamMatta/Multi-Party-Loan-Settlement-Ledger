# Functional Tests

These tests use a **real test database** with `backend/db/schema.sql`
applied. They verify the full pipeline from event insert through
projection query — what the unit tests in `backend/tests/test_balance.py`
and `backend/tests/test_interest.py` mock with `FakeConnection`.

## Running

```bash
# Boot a Postgres test DB. The simplest local option:
docker run --rm -d --name ledger-test-db \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=ledger_test \
  -p 5433:5432 postgres:16

# Run the functional suite:
TEST_DATABASE_URL=postgres://postgres:test@localhost:5433/ledger_test \
  pytest backend/tests/functional/

# Stop the test DB when done:
docker stop ledger-test-db
```

## Skipping behavior

If `TEST_DATABASE_URL` is not set, every test in this directory is
**skipped at module load time** — no spurious failures, no half-run
suites. CI sets the env var; local development can opt in by exporting
it. The unit tests in `backend/tests/` exercise the same code paths
without a database.

## What the scenarios cover

- `test_scenario_v_fronts_emi_for_p` — front-paying an EMI via a
  CONTRIBUTION + INTERPERSONAL_LOAN_DISBURSEMENT pair.
- `test_scenario_partial_repayment_and_settlement` — a wire-back plus
  an in-kind settlement together reduce the running balance.
- `test_scenario_compensating_entry_corrects_error` — both the
  original error and the compensating entry persist in the events
  table; time-travel queries before/after see the right balances.
- `test_scenario_as_of_date_time_travel` — point-in-time queries
  return the principal as it stood on each date.
- `test_scenario_interest_accrual_end_to_end` — rate-change boundary
  correctly switches from 0% to 6% accrual.
