# HOUSE_CONTEXT.md

**Project:** Tri-Party Loan & Settlement Ledger
**Status:** Approved for Build — Starting with Data Model & Feature Spec
**Last Updated:** 2026-04-27 

## What This Is
A financial ledger web app to track the co-purchase, multiple loan repayments, and ongoing maintenance of a 4-story house in India, shared by three cousins (V, P, S). The app handles uneven contributions, inter-personal reimbursements, FX-stamped USD→INR payments, off-ledger settlements, and exit/buyout scenarios. Intended to be the single source of truth for 20+ years — no migrations, no spreadsheet decay, no vendor lock-in. Will be open-sourced for the Indian diaspora co-buying community.

## The People & The Asset
* **V** — max earner. Often fronts payments (downpayment, bulk prepayments when business does well). Acts as informal banker within the group.
* **P** — contributor (the user authoring this project).
* **S** — contributor.
* **Property** — 4-story house in India. 1 floor per cousin + 1 shared floor. Floor usage is a separate concept from equity ownership.

## Finalized Business Logic

### Equity
* Fixed at exactly 1/3 each for V, P, S. Contributions do not shift equity.
* Optional one-time adjustment at t=0 only: if cousins bid on preferred floors (e.g., everyone wants the top floor), the winner pays a premium that offsets equity slightly (e.g., 35/32.5/32.5). Frozen after that. Not required for v1 but schema should accommodate it.

### When V Pays More Than His Share
* V is making a loan to P and/or S, not buying their equity.
* Creates an inter-personal loan entry: V → P and/or V → S.
* P and S reimburse V over time via Zelle, wire, or off-ledger settlements.

### Inter-Personal Loans (V↔P, V↔S)
* Interest-bearing with a configurable rate. Default 0%, but can be changed at any time. Rate changes apply forward only.
* Modeled as an event series per pair: disbursement → optional rate-change(s) → repayments.
* App generates per-financial-year interest statements per pair (needed for US tax reporting; Indian tax treatment is favorable since cousins are "relatives" under the IT Act, but statements are still useful).

### Payments, Multiple Loans & Settlements
* **Multiple Bank Loans:** The property can be financed by one or multiple external bank loans. Each loan has its own EMI schedule, interest rate, and prepayment rules.
* EMIs can be paid by V, P, or S directly, or P/S pays V who then pays the bank(s).
* V can make bulk prepayments to any specific bank loan; P/S can offload bonuses or savings to the bank or to V.
* **OpEx & Shared Maintenance:** Ongoing carrying costs (property tax, HOA/society fees, shared floor utilities and furnishing) are tracked as non-equity-building expenses and socialized 1/3 each.
* Off-ledger settlements are first-class citizens. Any value transfer counts: Zelle, V buying P's flight, S covering V's dinner in Mumbai. These reduce inter-personal balances exactly like cash transfers. Think Splitwise layered on top of the loan ledger.

### FX & Wire Transfers
* Every USD↔INR movement is timestamped with two rates:
    * Actual wire rate — the rate the bank actually applied. Used for balance-owed math.
    * Reference rate — RBI / exchangerate.host mid-market rate on that date. Used for FX gain/loss reporting.
* Daily automated FX snapshots from a reference source; manual override required for actual wire-day rate.
* Wire/transfer fees are borne by the sender. App records both USD sent and INR landed in the receiver's account. Credit to the sender's balance is calculated from INR landed, not USD sent. The delta is the sender's cost — not socialized.

### Exit / Drop-Off Scenarios
* Any party can exit at any time. App computes three buyout numbers on demand, side by side:
    1.  Net-contribution buyout (what they put in minus what they owe), adjusted for historical inflation.
    2.  1/3 of current market value (requires manual market value input).
    3.  Weighted blend of the two.
* The app surfaces numbers; humans and lawyers make the final call.
* Shared floor options on exit: exiter retains 1/3 of shared unit, or dilutes their share to the remaining two.

## Decided Tech Stack

| Layer | Choice | Notes |
| :--- | :--- | :--- |
| **Frontend** | Next.js / React  | Deployed on Vercel or Cloudflare Pages  |
| **Styling** | Tailwind + shadcn/ui  | Clean, readable, component-rich  |
| **Charts** | Recharts or Visx  | Balance over time, contribution breakdowns  |
| **Tables** | TanStack Table  | Ledger views, sortable/filterable  |
| **Backend** | FastAPI (Python)  | User is strong in Python  |
| **Database** | PostgreSQL  | Vanilla features only — no vendor-specific extensions  |
| **Agent layer** | FastMCP  | Exposes MCP tools for Claude/ChatGPT agent access  |
| **Auth** | Email magic-link  | No passwords to rotate over 20 years  |
| **Document storage** | S3-compatible  | Deed, loan docs, EMI receipts, Form 15CA/CB, TDS proofs  |
| **Deployment** | Docker container  | Fly.io / Railway / VPS. Not Kubernetes — overkill  |

**Why Postgres over SQLite:** Portable `pg_dump` archive format, concurrent-write safety for when MCP agents and humans write simultaneously, runs everywhere with no lock-in.
**Why FastMCP from day one:** Exposes endpoints like `get_balance(person, as_of_date)`, `record_payment(...)`, `simulate_exit(person)` as MCP tools. Enables conversational logging ("log that P sent V $500 via Zelle yesterday") which is more practical than tapping through forms on a phone.

## Non-Negotiable Architectural Principles
* **No vendor lock-in.** Only vanilla Postgres features. No Supabase RLS DSL, no Firebase, no Neon-specific branching. The schema must run identically on any Postgres host.
* **Append-only audit log.** Every mutation is an immutable signed event row (HMAC). Fields: who, what action, when, previous value, new value. No update-in-place. Balances are projections over the event log, not stored state.
* **Compensating Transactions:** Because the ledger is append-only, human data-entry errors are fixed via a "reversal" or "compensating" entry linked to the original error, preserving cryptographic certainty.
* **Nightly export escape hatch.** Full DB dumps to CSV + JSON, committed to a private Git repo that all three cousins clone locally. If the app dies tomorrow, the data survives in a human-readable, app-independent format.
* **Open-source generalization.** Build for N co-owners (not hardcoded 3), configurable equity splits, configurable base currency and property currency, pluggable FX provider.

## Core Questions the App Must Answer
* What is the current outstanding loan balance across all active bank loans? 
* What does P owe V today? What did P owe V on date X? 
* What has each person contributed in total (INR equivalent), split by CapEx (Equity) and OpEx (Maintenance)? 
* What are the inter-personal loan interest accruals per pair, per financial year? 
* If P exits today, what are the three buyout numbers? 
* What is the next EMI due for each loan, and who is paying it? 
* Show me the full event log for any given transaction.

## v1 Feature Scope
* **Contribution ledger** — every USD→INR payment, dual-rate FX, linked to specific bank loans, inter-personal loans, or OpEx.
* **Multi-Loan amortization tracker** — bank EMI schedules with prepayment attribution per person, per loan.
* **Inter-personal loan ledger** — per pair (V↔P, V↔S), event-sourced, configurable interest rate.
* **Settlement & OpEx entity** — generic off-ledger value transfers and shared property maintenance costs.
* **Document vault** — S3-linked to events (deed, sanction letters, receipts, tax docs).
* **Dashboard** — one screen: total loan balances, equity, inter-personal balances, next EMIs, recent events.
* **Exit-scenario calculator** — 3 buyout numbers on demand, requires manual market value input.
* **Per-FY interest statement generator** — per lender-borrower pair, exportable for tax filing.
* **MCP tool surface** — `get_balance`, `record_payment`, `simulate_exit`, `log_settlement`, `get_fx_rate`.
* **Nightly Git export** — full CSV + JSON dump, auto-committed to shared repo.

## Build Tooling Plan
* **Claude Code** — initial scaffold and hard pieces: schema design, FX math, audit log, MCP layer.
* **GitHub Copilot** — ongoing feature work, year-2+ maintenance.
* **Schema migrations written and reviewed by hand** — LLMs not trusted here; too easy to silently break data integrity[cite: 119, 120].
* Frontend and backend deployed separately.

## Pending External Input
* CA consultation in India: NRI property purchase compliance, FEMA remittance rules, TDS on property >₹50L. Note: Schema will include placeholders for Form 15CA/CB reference numbers and TDS deduction receipts.

## Immediate Next Steps (Do These In Order)
1.  **Core entities & relationships** ← START HERE. Multiple Loans, contributions, settlements, OpEx, inter-personal loans, FX events, documents — full ERD with field-level detail.
2.  **Event/audit model:** Mutation schema, HMAC signing approach, compensating transactions, export format.
3.  **Key computed views:** Balance queries, equity, per-FY interest statements, exit scenario math.
4.  **Screens & user flows:** Dashboard, ledger views, exit calculator, document vault.
5.  **MCP tool surface:** Tool definitions, input/output schemas.
6.  **Deployment & nightly-export story:** Docker setup, export cron job, Git sync.