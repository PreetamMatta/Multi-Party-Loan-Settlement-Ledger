# Business Logic Documentation

This directory is the authoritative reference for **what the ledger means** —
the financial, legal, and accounting reasoning behind every decision the code
makes. The schema in `backend/db/schema.sql` and the modules in `backend/core/`
are *implementations* of the rules described here.

> If the code and these documents disagree, **the documents are right and the
> code needs fixing.** This rule is non-negotiable. Ledger code that drifts
> from documented intent is a liability — silently wrong financial code is the
> worst kind of bug.

## Why this directory exists

This application is intended to run for **20+ years**. Over that horizon:

- The original authors will not always be available.
- Multiple AI coding sessions, contributors, and forks will touch the code.
- Tax authorities, lawyers, and co-owners will need to inspect the math.
- Edge cases (rate changes, FX shocks, an exit, an audit, a death) will arrive
  on a timeline that nobody can predict.

If the rationale for *why* a CONTRIBUTION event credits `inr_landed` instead
of `amount_source × rate` lives only in someone's head, it will be lost. If it
lives only in a Python comment, it will be mis-translated by the next session.
These documents are the durable record.

## How to read these documents

Read in roughly this order — each builds on the previous:

| # | File | What it covers |
|---|------|----------------|
| 1 | [event-log.md](event-log.md) | The append-only event log: every event type, what it means, HMAC signing, compensating entries, `effective_date` vs `recorded_at`. |
| 2 | [fx-and-wire-transfers.md](fx-and-wire-transfers.md) | The dual-rate FX system: actual rate vs reference rate, wire fee rules, FX gain/loss math, reference rate fetch process. |
| 3 | [interpersonal-loans.md](interpersonal-loans.md) | Inter-personal lending: forward-only rate changes, accrual math, per-FY statements, balance computation. |
| 4 | [balances-and-equity.md](balances-and-equity.md) | The fundamental equity-vs-balance distinction, the projection model, the one-time equity adjustment. |
| 5 | [exit-scenarios.md](exit-scenarios.md) | The three buyout numbers, the shared-floor election, what the calculator does *not* do. |
| 6 | [computed-views.md](computed-views.md) | The read interface for the ledger: four SQL views and one Python function that consumers go through to read balances. |

## The relationship between these documents and the code

```
docs/business-logic/*.md   ←—  the WHY and the CONTRACT
       │
       ▼
backend/core/*.py          ←—  the implementation of the contract
       │
       ▼
backend/db/schema.sql      ←—  the storage shape
       │
       ▼
backend/tests/*.py         ←—  the verification that code matches contract
```

When you change behavior — pick a different interest accrual convention, add a
new event type, change how FX is stamped — the order of operations is:

1. Update the relevant document in this directory **first**.
2. Get a human review of the documentation change before touching code.
3. Update the code to match.
4. Update the tests to verify the new contract.
5. Commit all three together so the docs, code, and tests stay in lockstep.

Drifts between documents and code are bugs. If you find one while reading,
**stop and fix it** — do not assume "the code is probably right."

## What does NOT belong in this directory

- **Implementation details** (file names, function signatures, SQL syntax).
  Those live in code or in the code's docstrings.
- **Build / deploy / infra** instructions. Those live in `README.md` and
  `backend/README.md`.
- **Architectural decisions** about the technology stack. Those are ADRs in
  `docs/decisions/`.
- **The story of the original cousins (V, P, S).** That lives in
  `docs/HOUSE_CONTEXT.md` as the original motivating use case. The documents
  in *this* directory describe the **generalized N-owner system**.

## A note on tone

These documents are written for two audiences simultaneously:

- A future human developer or co-owner reading the code for the first time.
- A future AI coding session reading these as cold-start context.

That means: explicit, plain English, concrete worked examples, no implicit
context. If a paragraph requires the reader to already understand the system,
rewrite it.
