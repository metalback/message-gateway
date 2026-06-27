"""Aggregator module that wires the versioned ``/v1`` API surface.

:func:`register_v1` mounts every router from :mod:`app.routes`
under a single ``APIRouter`` so the application factory only
needs to know about this one entry point. New versions (``v2``,
…) get their own sibling module.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.routes import admin, auth, billing, messages, templates, webhooks

# Routers are registered eagerly so the public API surface is
# discoverable from a single import. Each module exposes a
# module-level ``router`` of type ``APIRouter``.
_ROUTERS = (
    messages.router,
    templates.router,
    webhooks.router,
    auth.router,
    billing.router,
    admin.router,
)


class V1Discovery(BaseModel):
    """Response shape for the lightweight ``GET /v1`` endpoint."""

    api_version: str
    routes: list[str]


def _child_prefixes(router: APIRouter) -> list[str]:
    """Return the absolute prefixes of every child router mounted on
    the v1 surface. Pydantic-friendly list of strings for the
    discovery endpoint.

    Each child router's ``prefix`` attribute is its own segment
    (``/messages``); we re-apply the v1 prefix so the result is
    the path a client would actually hit (``/v1/messages``).
    """
    parent = router.prefix or ""
    prefixes: set[str] = set()
    for route in router.routes:
        original = getattr(route, "original_router", None)
        if original is None:
            continue
        child = getattr(original, "prefix", "") or ""
        combined = (parent + child) or "/"
        prefixes.add(combined)
    return sorted(prefixes)


def register_v1() -> APIRouter:
    """Build the ``/v1`` ``APIRouter`` aggregating every feature.

    Returns a fresh router (instead of caching one) so the function
    is side-effect free and tests can mount the same surface onto a
    custom ``FastAPI`` instance without sharing state.
    """
    v1 = APIRouter(prefix="/v1")

    @v1.get("", tags=["meta"], include_in_schema=False, response_model=V1Discovery)
    async def v1_root() -> V1Discovery:
        """Lightweight discovery endpoint.

        Lists the top-level prefixes registered on the v1 router so
        a client (or an operator running ``curl``) can confirm which
        feature surfaces are live without having to open ``/docs``.
        """
        return V1Discovery(api_version="v1", routes=_child_prefixes(v1))

    for router in _ROUTERS:
        v1.include_router(router)
    return v1
