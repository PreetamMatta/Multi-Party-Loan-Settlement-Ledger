# Database Migration Policy

This directory holds schema migrations. **Read this entire policy before opening a PR that touches the schema.**

---

## Naming convention

```
YYYYMMDD_NNN_short_description.sql
```

Examples:
- `20260501_001_add_tds_fields_to_events.sql`
- `20260612_002_create_v_owner_contributions_view.sql`

The `NNN` counter resets per day. Two migrations on the same date increment from `001`.

## AI agents must NOT auto-apply migrations

Claude Code, Copilot, Cursor, and any other AI agent may **propose** migration SQL. A human co-owner must:

1. Read the SQL.
2. Sanity-check it against `backend/db/schema.sql` and `docs/HOUSE_CONTEXT.md`.
3. Apply it on a local restore of the latest production dump.
4. Verify: schema applies cleanly, no data loss, no orphaned FKs, app boots, smoke tests pass.
5. Apply to production during a maintenance window.

Until step 5, the migration file may be edited. After production application, the file is **frozen** (see below).

## Append-only after application

Once a migration has been applied to production, **its file is never edited.** If the migration was wrong, write a *new* migration that fixes the prior one. Do not retroactively rewrite history — it breaks reproducibility for anyone restoring from a dump.

## Test against a prod restore

Every non-trivial migration must be tested by:

```bash
# 1. Restore the latest prod dump locally.
pg_restore -d ledger_test latest-prod.dump

# 2. Apply the migration.
psql -d ledger_test -f backend/db/migrations/YYYYMMDD_NNN_*.sql

# 3. Run the schema sanity check (Session 3+ test suite).
pytest backend/tests/db/

# 4. Boot the API against ledger_test and exercise the affected endpoints.
```

## Post-migration responsibilities

- **Update `backend/db/schema.sql`** to match the new state. Schema.sql is always the truth-as-of-now.
- **Update `docs/ERD.md`** if the change is structural (new table, FK, or removed column).
- **Add an ADR in `docs/decisions/`** if the migration represents a non-obvious architectural choice.

## Review requirement

At least one co-owner (other than the author) must review the migration SQL **and** the local-restore test output before it is applied to production. Sign off via PR approval.

## Things migrations are NOT for

- **Bulk data fixes.** Use a one-off Python script committed under `backend/scripts/`, run by hand, and link the script in the relevant ADR. Do not wrap data fixes in migration files.
- **Re-signing event rows.** If `HMAC_SECRET_KEY` rotates, re-signing is a separate batched job — not a schema migration.
- **Editing the events table directly.** The events table is append-only by architectural rule. A migration must never `UPDATE` or `DELETE` event rows.
