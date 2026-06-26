"""Authentication endpoints (scaffold placeholder)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def register() -> dict[str, str]:
    """Register a new client and mint an API key (placeholder)."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="register is not implemented in the scaffold",
    )
