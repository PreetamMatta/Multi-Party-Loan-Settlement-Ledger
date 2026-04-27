"""
owners.py — Owner and property administration endpoints.

TODO Session 4: implement.
Planned endpoints:
  GET  /owners              — list owners for a property
  POST /owners              — add an owner (must keep equity_pct sum at 100)
  GET  /owners/{id}/balance — current outstanding interpersonal balance for this owner
"""

from fastapi import APIRouter

router = APIRouter()
