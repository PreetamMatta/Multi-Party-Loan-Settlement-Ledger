"""
settlements.py — Off-ledger settlement endpoints.

TODO Session 4: implement.
Planned endpoints:
  POST /settlements           — record a Zelle / cash / in-kind settlement event
  POST /settlements/opex      — record a shared-OpEx expense and its split
"""

from fastapi import APIRouter

router = APIRouter()
