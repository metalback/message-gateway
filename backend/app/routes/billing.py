"""Billing / balance endpoints (scaffold placeholder)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/balance", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def get_balance() -> dict[str, str]:
    """Return the current month's balance (placeholder)."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="get_balance is not implemented in the scaffold",
    )
