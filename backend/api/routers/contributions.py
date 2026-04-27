"""
contributions.py — CapEx contribution endpoints.

TODO Session 4: implement.
Planned endpoints:
  POST /contributions          — record a new CapEx contribution event
  GET  /contributions/{owner}  — list contributions by owner
"""

from fastapi import APIRouter

router = APIRouter()
