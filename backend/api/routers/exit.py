"""
exit.py — Exit / buyout scenario endpoints.

TODO Session 6: implement.
Planned endpoints:
  POST /exit/simulate    — compute three buyout numbers given a market value
  POST /exit/finalize    — record an EXIT event (irreversible, append-only)
"""

from fastapi import APIRouter

router = APIRouter()
