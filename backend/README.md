# Backend

FastAPI service for the Multi-Party Loan & Settlement Ledger.

## Layout

```
backend/
├── main.py             # uvicorn entrypoint
├── pyproject.toml      # PEP 621 packaging + deps
├── .env.example        # full env spec
├── db/
│   ├── schema.sql      # production-intent schema (vanilla pg16)
│   ├── seed.sql        # dev seed (V/P/S as example only)
│   └── migrations/     # human-reviewed migrations only
├── core/
│   ├── events.py       # event model + HMAC signing (implemented)
│   ├── fx.py           # dual-rate FX utilities (implemented)
│   ├── balance.py      # balance projection engine (Session 3)
│   └── interest.py     # interpersonal interest accrual (Session 3/6)
├── api/
│   ├── app.py          # FastAPI app factory
│   ├── dependencies.py # DB session + auth
│   └── routers/        # endpoint routers (Session 4)
├── mcp/
│   └── tools.py        # FastMCP tool surface (Session 5)
└── export/
    └── nightly.py      # CSV+JSON export to Git (Session 8)
```

## Local development

```bash
# Via docker-compose (recommended)
docker compose up -d

# Or directly (requires local Postgres)
cp .env.example .env
pip install -e ".[dev]"
uvicorn main:app --reload --port 8000
```

## Conventions

- All financial math uses `Decimal` — never `float`.
- Every event row is HMAC-signed; new event types must use the canonical field order documented in `core/events.py`.
- No stored balance columns; all balances are projections.
- No vendor-specific Postgres features; schema must apply on any vanilla pg16+.

See [`../.agents/AGENTS.md`](../.agents/AGENTS.md) for the full architectural rules.
