"""
fx.py — FX rate snapshot and lookup endpoints.

TODO Session 4: implement.
Planned endpoints:
  GET  /fx/rates                       — list rate snapshots in a date range
  GET  /fx/rates/{date}?pair=USD_INR   — fetch reference rate for a date
  POST /fx/rates                       — manually insert a rate snapshot
"""

from fastapi import APIRouter

router = APIRouter()
