# Architecture Decision Records (ADRs)

Architecture Decision Records go here.

## Convention

- One markdown file per decision: `NNNN-short-title.md` (e.g. `0001-append-only-event-log.md`)
- Sequentially numbered, never reused
- Append-only: once an ADR is written, supersede it with a new ADR rather than editing
- Each ADR should cover: **Context**, **Decision**, **Consequences**, **Alternatives Considered**

## Status field values

- `Proposed` — under discussion
- `Accepted` — in force
- `Superseded by ADR-NNNN` — replaced by a later decision
- `Deprecated` — no longer relevant but kept for history

## Initial ADRs to backfill (Session 2+)

- 0001 — Append-only event log
- 0002 — HMAC signing of every event row
- 0003 — Balances as projections, never stored
- 0004 — Vanilla PostgreSQL only (no extensions, no RLS)
- 0005 — Dual-rate FX stamping
- 0006 — Wire fees borne by sender (not socialized)
- 0007 — Compensating entries for corrections
- 0008 — Nightly Git-committed CSV/JSON export as survival strategy
