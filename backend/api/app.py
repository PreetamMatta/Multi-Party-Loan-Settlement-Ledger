"""
app.py — FastAPI app factory.

Registers all routers from `api.routers`. Routers themselves are stubbed in
this session; their endpoints land in Session 4.
"""

from fastapi import FastAPI

from api.routers import (
    contributions,
    fx,
    loans,
    owners,
    settlements,
)
from api.routers import (
    exit as exit_router,
)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Multi-Party Loan & Settlement Ledger",
        description=(
            "Event-sourced, append-only financial ledger for groups of N "
            "co-owners managing property across borders."
        ),
        version="0.1.0",
    )

    app.include_router(owners.router, prefix="/owners", tags=["owners"])
    app.include_router(contributions.router, prefix="/contributions", tags=["contributions"])
    app.include_router(loans.router, prefix="/loans", tags=["loans"])
    app.include_router(settlements.router, prefix="/settlements", tags=["settlements"])
    app.include_router(fx.router, prefix="/fx", tags=["fx"])
    app.include_router(exit_router.router, prefix="/exit", tags=["exit"])

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
