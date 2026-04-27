# AGENTS.md — AI Coding Session Memory

> This file is the persistent context for every AI coding session on this repo
> (Claude Code, Copilot, Cursor, or any future agent). A cold-start AI reading
> only this file should be able to continue development without asking
> clarifying questions.
>
> **Update when:** architecture changes, a session completes, or the build phase advances.

---

## Project Identity

- **Name:** Multi-Party Loan & Settlement Ledger
- **One-liner:** Event-sourced, append-only financial ledger for groups of N people co-purchasing and co-managing property across borders.
- **Open-source intent:** Will be published for the Indian diaspora co-buying community and beyond. Code must be clean, documented, and N-generalized — never hardcoded to a specific household.
- **Problem it solves:** Spreadsheets cannot model uneven contributions, multiple bank loans, inter-personal loans, off-ledger settlements, dual-currency wires, shared maintenance, and exit/buyout math over a 20+ year horizon. This app does.
- **Real-world origin:** Three cousins co-buying a flat in India is the use case that shaped the design. That story lives in `docs/HOUSE_CONTEXT.md` as the canonical business logic reference. The **app itself is generic.**

## Scalability Principle

The app is for **any group of N co-owners.** The cousins V, P, and S referenced in `docs/HOUSE_CONTEXT.md` are example seed data only — they must never appear as hardcoded entities in schema, logic, or config.

- Number of owners is runtime config.
- Equity splits are runtime config (per-owner `equity_pct`).
- Base currency per owner is runtime config (USD, INR, GBP, etc.).
- Property currency is runtime config.
- Bank loans, FX pairs, and tax jurisdictions are all data, not code.

V, P, and S only appear in `backend/db/seed.sql` and in example documentation.

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI (Python) | Strong Python background; async-native; clean OpenAPI output |
| Database | PostgreSQL (vanilla only) | Portable `pg_dump`, concurrent-write safe, runs anywhere |
| Agent layer | FastMCP | Exposes ledger as MCP tools for conversational logging via Claude/ChatGPT |
| Frontend | Next.js + React | Separate deployment lifecycle from backend |
| Styling | Tailwind + shadcn/ui | Component-rich, readable, consistent |
| Charts | Recharts or Visx | Balance timelines, contribution breakdowns |
| Tables | TanStack Table | Sortable/filterable ledger views |
| Auth | Email magic-link | No passwords to rotate over 20 years |
| Docs storage | S3-compatible | Deeds, loan docs, receipts, tax forms |
| Deploy | Docker (Fly.io / Railway / VPS) | Simple, portable, not Kubernetes |

## What is explicitly NOT used (and why)

- **No Supabase RLS** — vendor lock-in.
- **No Neon branching features** — vendor lock-in.
- **No Postgres extensions** — schema must apply on any vanilla pg16+.
- **No SQLite** — no concurrent-write safety for MCP agents.
- **No Kubernetes** — overkill for this scale.
- **No stored balance columns** — all balances are projections over the event log.
- **No hardcoded owner count** — everything is N-configurable.
- **No floats in financial math** — `Decimal` everywhere.

## Non-Negotiable Architectural Rules

These are hard constraints. Violations require an explicit migration discussion with the human reviewer.

1. **Append-only event log.** Every mutation is an immutable event row. No `UPDATE` or `DELETE` on ledger data — ever.
2. **Balances are projections.** Never store a computed balance as a column. Always derive from event replay.
3. **HMAC signatures on every event.** Each event row is signed. Canonical field order: `{id}|{event_type}|{actor_owner_id}|{amount_inr}|{effective_date}|{recorded_at}`.
4. **Compensating transactions for corrections.** Errors are fixed by writing a reversal event linked to the original via `reverses_event_id`. Never edit or delete the original.
5. **Vanilla PostgreSQL only.** No Supabase RLS, no Neon branching, no extensions. Schema must run identically on any Postgres host.
6. **N-owner generalization.** Never hardcode the number of owners. Owner count, equity splits, base currency, and property currency are all runtime config.
7. **Schema migrations are human-reviewed.** Propose changes — do not auto-apply. The migration policy is documented in `backend/db/migrations/README.md`.
8. **Dual-rate FX stamping.** Every USD↔INR event records both the actual wire rate (used for balance math) and the reference mid-market rate (used for FX gain/loss reporting).
9. **Wire fees are the sender's cost.** Credit to the sender's balance = INR landed (not USD sent × rate). The fee delta is never socialized.
10. **Nightly export must always work.** CSV + JSON dump to a Git repo is the 20-year survival strategy. Keep the export path functional at all times.

## Business Logic Reference

(Distilled from `docs/HOUSE_CONTEXT.md`. Read that file for the full narrative.)

- **Equity model:** Fixed equal shares per owner by default. Optional one-time floor-premium offset at t=0 (e.g., 35 / 32.5 / 32.5 if one owner takes a preferred floor), frozen after that. Overpayment by one owner is an inter-personal loan, not an equity shift.
- **Inter-personal loans:** Tracked per lender↔borrower pair. Configurable interest rate (default 0%). Rate changes apply forward only — never retroactively. Generates per-financial-year statements for tax filing.
- **EMI payment flows:** Any owner can pay the bank directly, or pay another owner who then pays the bank. All hops are tracked as events. The property may have multiple concurrent bank loans, each with its own amortization.
- **Off-ledger settlements:** First-class events. Zelle, paying someone's flight, covering a dinner — all reduce inter-personal balances exactly like cash.
- **OpEx:** Property tax, HOA, shared utilities — socialized equally among owners. Tracked separately from CapEx (contribution toward principal / equity).
- **FX:** Actual wire rate → balance math. Reference (mid-market) rate → FX gain/loss reporting. Wire fees borne by the sender.
- **Exit:** Three buyout numbers on demand: (1) net-contribution adjusted for inflation, (2) equal share of current market value, (3) weighted blend. App surfaces numbers — humans decide.

## Core Questions the App Must Answer

- What is the outstanding balance across all active bank loans?
- What does Owner A owe Owner B today? What did they owe on date X?
- What has each owner contributed (in property currency equivalent), split by CapEx vs OpEx?
- What are the inter-personal loan interest accruals per pair, per financial year?
- If Owner X exits today, what are the three buyout numbers?
- What is the next EMI due per loan, and who is paying it?
- Show me the full event log for a given transaction.

## Current Build Phase

```
Session 1 — Scaffold, docs, schema, backend foundation       ← COMPLETE
Session 2 — Event log full implementation + HMAC tests       ← NEXT
Session 3 — Balance projection engine, FX module, computed views
Session 4 — FastAPI endpoints (contribution, payment, settlement, FX)
Session 5 — FastMCP tool surface implementation
Session 6 — Exit scenario calculator, per-FY interest statements
Session 7 — Frontend (Next.js, TanStack Table, Recharts dashboard)
Session 8 — Docker prod config, nightly export, Git sync cron
```

## File Ownership & Update Rules

```
.agents/AGENTS.md            — Update when: architecture changes, session completes, build phase advances
README.md                    — Update when: directory structure changes, quickstart steps change, new env vars added
docs/HOUSE_CONTEXT.md        — Update when: business logic is finalized or revised, new decisions made
backend/db/schema.sql        — Only changed via human-reviewed migration scripts. AI proposes, human applies.
backend/db/migrations/*.sql  — Append-only. Once a migration is applied, its file is never edited.
docs/decisions/*.md          — One ADR per architectural decision. Append-only.
```

## What AI Sessions Must NOT Do

- **Do not auto-apply schema migrations.** Propose SQL — wait for a human to apply it.
- **Do not store computed balances as columns.** Always replay the event log.
- **Do not use Postgres-vendor-specific features** (RLS, Neon branching, custom extensions beyond core).
- **Do not hardcode owner count or owner names.** No `if len(owners) == 3` style logic anywhere.
- **Do not skip HMAC signing when adding new event types.** Every event row must be signed.
- **Do not use floats in financial math.** Always `Decimal`.
- **Do not write frontend code during a backend session** (and vice versa). Stay in scope.
- **Do not overwrite `docs/HOUSE_CONTEXT.md` without being explicitly asked.** It is the canonical reference.
- **Do not edit or delete event log rows.** Corrections are compensating entries.
- **Do not introduce floats, ENUM types, or vendor-specific features** silently.
