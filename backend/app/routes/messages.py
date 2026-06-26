"""Message sending / status endpoints (scaffold placeholder).

Concrete handlers land in later tasks. We expose a single
``APIRouter`` here so the aggregator in :mod:`app.api` can
mount it; the real routes will be added incrementally.

The placeholder ``GET /`` returns a ``501 Not Implemented`` so
the route is observable in the OpenAPI schema without promising
behaviour the domain code does not yet provide.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def list_messages() -> dict[str, str]:
    """List messages (placeholder).

    Returns ``501`` until the messages feature lands in a later
    task. The route is registered so the public surface is
    discoverable via ``GET /v1``.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="list_messages is not implemented in the scaffold",
    )
