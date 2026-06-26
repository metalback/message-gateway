"""WhatsApp template CRUD endpoints (scaffold placeholder)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def list_templates() -> dict[str, str]:
    """List WhatsApp templates (placeholder)."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="list_templates is not implemented in the scaffold",
    )
