"""FastAPI application entry point.

This file is intentionally thin: it wires the FastAPI app, registers
middleware/CORS, and mounts routers. Domain logic lives in submodules
(added in later tasks). The health endpoints are defined here because
they belong to the platform itself, not a business domain.
"""

from __future__ import annotations

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware

from app.api import register_v1
from app.config import Settings, get_settings
from app.health import overall_ok, run_readiness_checks
from app.observability import configure_logging

APP_NAME = "Message Gateway API"
APP_VERSION = "0.1.0"


def _cors_origins(raw: str) -> list[str]:
    """Parse a comma-separated CORS allow-list.

    Empty / wildcard values fall back to a permissive list to make
    local development friction-free. Production deployments must set
    a concrete list in `CORS_ALLOW_ORIGINS`.
    """
    items = [o.strip() for o in raw.split(",") if o.strip()]
    return items or ["*"]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory.

    Accepting an optional `Settings` instance makes it trivial for
    tests to construct the app with overridden configuration without
    touching the cached singleton.
    """
    settings = settings or get_settings()

    # Install the project-wide log handler before any route is
    # registered so the very first log line (uvicorn's startup
    # banner, our own ``configure_logging`` call, etc.) shares
    # the same formatter.
    configure_logging(settings)

    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(settings.cors_allow_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """Liveness probe.

        Returns the app name and version without touching external
        services. The endpoint is safe to hit on every interval:
        if it answers, the process is up and the event loop is
        healthy. Deeper checks live on ``/health/ready``.
        """
        return {"status": "ok", "app": APP_NAME, "version": APP_VERSION}

    @app.get(
        "/health/ready",
        tags=["meta"],
        responses={
            200: {"description": "All dependencies reachable."},
            503: {"description": "At least one dependency is unreachable."},
        },
    )
    async def readiness(response: Response) -> dict[str, object]:
        """Readiness probe.

        Runs the dependency probes (database, Redis) registered in
        :mod:`app.health`. Returns 200 when every check succeeds and
        503 when any check fails so a load-balancer can take the
        pod out of rotation until the dependencies recover.
        """
        checks = await run_readiness_checks(settings)
        ok = overall_ok(checks)
        if not ok:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if ok else "degraded",
            "app": APP_NAME,
            "version": APP_VERSION,
            "checks": [check.to_dict() for check in checks],
        }

    @app.get("/", tags=["meta"], include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"message": "Message Gateway API", "docs": "/docs"}

    # Versioned API surface. New versions (`/v2`, …) are mounted
    # by the same pattern: build a router, include it once.
    app.include_router(register_v1())

    return app


# Module-level instance for `uvicorn app.main:app`.
app = create_app()
