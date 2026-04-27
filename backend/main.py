"""
main.py — Uvicorn entrypoint for the FastAPI service.

Run locally:
    uvicorn main:app --reload --port 8000

In docker-compose, this is the target of the `api` service.
"""

from api.app import create_app

app = create_app()
