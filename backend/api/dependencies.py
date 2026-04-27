"""
dependencies.py — FastAPI dependency providers.

Stubs for DB session and auth. Real implementations land in Session 4.
"""

from collections.abc import AsyncIterator


async def get_db() -> AsyncIterator[object]:
    """
    Yield a database session for the duration of a request.

    TODO Session 4: wire up an asyncpg / SQLAlchemy async session pool.
    """
    raise NotImplementedError("TODO Session 4: implement DB session dependency")
    yield  # pragma: no cover  (keeps the generator signature valid for typing)


async def get_current_owner() -> dict:
    """
    Resolve the authenticated owner from a magic-link session token.

    Returns a dict with at least: {"id": UUID, "email": str, "property_id": UUID}.

    TODO Session 4: implement magic-link token verification.
    """
    raise NotImplementedError("TODO Session 4: implement magic-link auth dependency")
