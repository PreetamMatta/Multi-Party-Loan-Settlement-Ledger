# Multi-Party Loan & Settlement Ledger

![Backend CI](https://github.com/PreetamMatta/Multi-Party-Loan-Settlement-Ledger/actions/workflows/backend-ci.yml/badge.svg)

> ⚠️ **Keep this file current.** Update the directory tree, quickstart steps, and environment variable list
> whenever they change. Outdated READMEs mislead contributors and future AI sessions alike.

An event-sourced, append-only financial ledger for groups of N people co-purchasing
and co-managing property across borders.

---

## What it solves

Co-buying property is a multi-decade financial entanglement. Spreadsheets break down
within a year — they cannot model uneven contributions, multiple bank loans,
inter-personal loans accruing interest, off-ledger settlements (Zelle, "I covered
your flight"), shared maintenance costs, dual-currency wires with their own
gain/loss profile, or fair exit/buyout math.

This project is the open-source answer for the Indian diaspora co-buying
community and any group that needs:

- A tamper-evident shared ledger that survives 20+ years
- Honest dual-currency tracking (actual wire rate vs. mid-market reference)
- A clean record of who-owes-whom, derivable on any historical date
- An MCP agent surface so logging happens by chat, not by data entry
- A nightly Git-committed CSV/JSON export that lives even if the app dies

## Who it is for

Any group of N co-owners — siblings, cousins, friends, partners — who:

- Live in different countries and wire money in different currencies
- Are jointly servicing one or more bank loans
- Lend each other money informally
- Cover incidental expenses for each other ("I'll pay this dinner / flight / utility bill")
- Need a single source of truth that all parties can trust and inspect
- Will eventually face an exit / buyout conversation and want fair math, not arguments

## Architecture overview

- **Append-only event log.** Every mutation — contribution, EMI payment, inter-personal loan, settlement, OpEx, FX snapshot, exit — is one immutable signed row in `events`.
- **Balances are projections.** No stored balance columns anywhere. All balances are computed by replaying the event log, which makes time-travel queries free ("What did A owe B on 2031-03-15?").
- **HMAC-signed events.** Every event row carries an HMAC-SHA256 signature over its canonical fields. Tampering is detectable.
- **Compensating transactions.** Errors are corrected by writing a reversal event linked via `reverses_event_id`. Originals are never edited or deleted.
- **Dual-rate FX.** Every USD↔INR movement records both the actual wire rate (used for balance math) and the reference mid-market rate (used for FX gain/loss reporting). Wire fees are the sender's cost.
- **MCP agent layer.** A FastMCP tool surface lets you log entries conversationally: *"Record that P sent V $500 via Zelle yesterday."*
- **20-year survival strategy.** A nightly job dumps all tables to CSV + JSON and commits them to a private Git repo every co-owner has cloned. The data outlives any single deployment.
- **Vanilla PostgreSQL only.** No vendor extensions, no RLS, no proprietary features. Runs anywhere `pg_dump` runs.

## Directory structure

```
/
├── README.md                          # this file
├── docker-compose.yml                 # local dev: db + api + adminer
├── .env.example                       # pointer to backend/.env.example
├── .gitignore
├── .agents/
│   └── AGENTS.md                      # cold-start memory for AI coding sessions
├── docs/
│   ├── HOUSE_CONTEXT.md               # the original story (three cousins) that motivated the project
│   ├── ERD.md                         # placeholder — added after schema finalized
│   ├── business-logic/                # authoritative business-logic reference (read this!)
│   │   ├── README.md                  # index and contributor guide
│   │   ├── event-log.md               # event taxonomy, HMAC, compensating entries
│   │   ├── fx-and-wire-transfers.md   # dual-rate FX, wire fees, gain/loss
│   │   ├── interpersonal-loans.md     # interest model, rate changes, FY statements
│   │   ├── balances-and-equity.md     # equity vs balance, projection model
│   │   └── exit-scenarios.md          # three buyout numbers, shared-floor election
│   └── decisions/                     # Architecture Decision Records (ADRs)
├── backend/
│   ├── README.md
│   ├── pyproject.toml
│   ├── .env.example
│   ├── main.py                        # FastAPI entrypoint (uvicorn target)
│   ├── db/
│   │   ├── schema.sql                 # full production-intent schema
│   │   ├── seed.sql                   # dev seed (V, P, S as example data)
│   │   └── migrations/
│   │       └── README.md              # human-review migration policy
│   ├── core/
│   │   ├── events.py                  # event model + HMAC + validation + financial-effect routing
│   │   ├── fx.py                      # dual-rate FX stamping + reference-rate fetch with fallback
│   │   ├── balance.py                 # balance projection engine (Session 3)
│   │   └── interest.py                # interpersonal interest accrual (Session 3/6)
│   ├── tests/                         # pytest suite (test_events, test_fx, test_fx_fetcher)
│   ├── api/
│   │   ├── app.py                     # FastAPI app factory
│   │   ├── dependencies.py            # DB + auth dependencies
│   │   └── routers/                   # endpoint routers (Session 4)
│   ├── mcp/
│   │   └── tools.py                   # FastMCP tool surface (Session 5)
│   └── export/
│       └── nightly.py                 # CSV+JSON export to Git (Session 8)
└── frontend/                          # Next.js app (Session 7 — empty for now)
```

## Quickstart

```bash
# 1. Clone
git clone <this-repo> ledger
cd ledger

# 2. Configure environment
cp backend/.env.example backend/.env
# Edit backend/.env — at minimum, set HMAC_SECRET_KEY:
#   python -c "import secrets; print(secrets.token_hex(32))"

# 3. Boot local stack (db + api + adminer)
docker compose up -d

# 4. Verify
#   API:      http://localhost:8000/docs
#   Adminer:  http://localhost:8080  (Server: db, login from backend/.env)

# Schema and seed are auto-applied to a fresh db on first boot.
# To reset the database:
docker compose down -v && docker compose up -d
```

## Business logic

The authoritative reference for *how the ledger thinks* lives under
[`docs/business-logic/`](docs/business-logic/). The code in `backend/core/`
implements these contracts; if the two ever disagree, the docs are right and
the code needs fixing.

| Document | What it covers |
|----------|----------------|
| [event-log.md](docs/business-logic/event-log.md) | All 13 event types with worked examples, HMAC canonical-string contract, compensating-entry mechanics, `effective_date` vs `recorded_at`. |
| [fx-and-wire-transfers.md](docs/business-logic/fx-and-wire-transfers.md) | The dual-rate FX system: actual rate drives balance math, reference rate drives FX gain/loss reporting. Wire fees are the sender's cost. |
| [interpersonal-loans.md](docs/business-logic/interpersonal-loans.md) | Forward-only rate changes, simple-interest actual/365 accrual, per-financial-year statement shape, balance computation algorithm. |
| [balances-and-equity.md](docs/business-logic/balances-and-equity.md) | Equity is fixed at setup; balances are projections. The one-time t=0 floor-premium adjustment is the only equity exception. |
| [exit-scenarios.md](docs/business-logic/exit-scenarios.md) | Three buyout numbers (net contribution, market-value share, weighted blend), shared-floor election, hard limits on what the calculator does. |

## Key design decisions

| Decision | Rationale |
|---|---|
| Append-only event log | Tamper-evidence, time-travel queries, regulatory-grade audit trail |
| Balances as projections (never stored) | Single source of truth; no risk of drift between cached and replayed values |
| Dual-rate FX stamping | Actual rate drives money-owed math; reference rate drives FX gain/loss reporting |
| Compensating entries (no UPDATE/DELETE) | Errors are recorded as new events linked to originals — full history preserved |
| Vanilla PostgreSQL only | `pg_dump` portability; no vendor lock-in over a 20-year horizon |
| N-owner generalization | Owner count, equity splits, currencies are runtime config — not baked into schema |
| HMAC signing on every event | Detect tampering at the row level; survives migrations and exports |
| Nightly Git-committed export | Survival strategy if the app stack dies — data lives in human-readable form |
| FastMCP tool surface | Conversational logging via Claude/ChatGPT; humans don't fill forms, they describe events |

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI (Python) | Async-native; clean OpenAPI; ergonomic for financial code |
| Database | PostgreSQL 16 (vanilla) | Portable, concurrent-write safe, runs on any host |
| Agent layer | FastMCP | Exposes ledger as MCP tools for conversational logging |
| Frontend | Next.js + React | Independent deploy lifecycle from backend |
| Styling | Tailwind + shadcn/ui | Component-rich, consistent, readable |
| Charts | Recharts / Visx | Balance timelines, contribution breakdowns |
| Tables | TanStack Table | Sortable / filterable ledger views |
| Auth | Email magic-link | No passwords to rotate over 20 years |
| Docs storage | S3-compatible (AWS / R2 / MinIO) | Deeds, loan sanctions, EMI receipts, tax forms |
| Deploy | Docker (Fly.io / Railway / VPS) | Portable, simple, not Kubernetes |

## Contributing & open-source intent

This project is built to be published for the Indian diaspora co-buying community
and beyond. The code must stay clean, documented, and N-generalized — no
hardcoded owner counts, names, currencies, or jurisdictions.

The real-world use case that motivated the project (three cousins co-buying a
flat in India) lives in [`docs/HOUSE_CONTEXT.md`](docs/HOUSE_CONTEXT.md) as the
canonical business logic reference. The example owners V, P, and S appear only
in `backend/db/seed.sql` as illustrative data — never in schema, logic, or config.

If you are an AI coding agent (Claude Code, Copilot, etc.), read
[`.agents/AGENTS.md`](.agents/AGENTS.md) before making any changes. It contains
the non-negotiable architectural rules and the current build phase.

## License

TBD — license to be selected before public release. Until then, all rights reserved.
