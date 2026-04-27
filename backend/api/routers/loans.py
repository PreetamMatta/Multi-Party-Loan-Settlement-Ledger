"""
loans.py — Bank loan and EMI endpoints.

TODO Session 4: implement.
Planned endpoints:
  POST /loans                       — register a new bank loan + generate EMI schedule
  GET  /loans                       — list active loans for a property
  POST /loans/{loan_id}/emi-payment — record an EMI payment event
  POST /loans/{loan_id}/prepayment  — record a bulk prepayment event
"""

from fastapi import APIRouter

router = APIRouter()
