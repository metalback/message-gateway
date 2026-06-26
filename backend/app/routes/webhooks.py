"""Webhook subscription endpoints (scaffold placeholder)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def list_webhooks() -> dict[str, str]:
    """List webhook subscriptions (placeholder)."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="list_webhooks is not implemented in the scaffold",
    )
