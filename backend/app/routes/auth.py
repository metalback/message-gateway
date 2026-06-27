"""Authentication endpoints (register, login, API key auth).

Replaces the ``501 Not Implemented`` scaffold with the actual
``POST /v1/auth/register`` and ``POST /v1/auth/login`` handlers
documented in the PRD. The same module also exposes:

- ``GET  /v1/auth/me``        – resolve the API key in the
  ``X-API-Key`` header to the current client. Useful for the
  dashboard to confirm "yes, this key still works" without
  exposing a separate introspection endpoint.
- ``POST /v1/auth/api-keys/rotate`` – mint a new API key for
  the current client. The previous key is invalidated
  immediately; the new plain key is returned exactly once.

The handlers stay thin: every business decision lives in
:mod:`app.services.auth`, the request / response models in
this module, and the FastAPI dependency (``require_api_key``)
is the only place that converts a header into a
:class:`~app.models.client.Client` row.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client, ClientPlan
from app.services.auth import (
    AuthError,
    DuplicateIdentityError,
    InactiveClientError,
    InvalidApiKeyError,
    InvalidCredentialsError,
    InvalidInputError,
    authenticate_api_key,
    authenticate_login,
    decode_session_token,
    register_client,
    rotate_api_key,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Body of ``POST /v1/auth/register``.

    Pydantic validates ``email`` via the optional ``email-validator``
    dependency (already pinned in ``requirements.txt``); an
    obviously invalid value is rejected with a 422 before the
    handler runs. ``name`` / ``rut`` / ``password`` are validated
    by the service layer because the rules (RUT check digit,
    password length) are domain-specific.
    """

    name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr
    rut: str = Field(..., min_length=1, max_length=20)
    password: str = Field(..., min_length=1, max_length=255)
    # Optional plan pick; defaults to ``starter`` on the service
    # side. We accept the value as a string here so the OpenAPI
    # schema documents the legal values without us hard-coding
    # the enum in the request type.
    plan: ClientPlan | None = None


class RegisterResponse(BaseModel):
    """Response shape for a successful registration.

    The plain ``api_key`` is included **once**: the platform
    does not store it and cannot recover it later. The caller
    (typically the dashboard onboarding wizard) is expected to
    surface the value to the user and discard it from memory
    as soon as the user has confirmed they have stored it.
    """

    id: str
    name: str
    email: EmailStr
    rut: str
    plan: ClientPlan
    status: str
    api_key: str
    api_key_last4: str
    created_at: datetime


class LoginRequest(BaseModel):
    """Body of ``POST /v1/auth/login``."""

    email: EmailStr
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    """Response shape for a successful login.

    The ``token`` is a short-lived JWT the dashboard can attach
    to subsequent requests as a ``Bearer`` credential. The
    ``expires_at`` field lets the UI show a countdown without
    having to decode the JWT locally.
    """

    token: str
    token_type: str = "bearer"
    expires_at: datetime
    client: ClientSummary


class ClientSummary(BaseModel):
    """Lightweight projection of a :class:`Client` row.

    The dashboard needs the customer's name, plan and current
    status to render the top bar. The full row (including
    bcrypt digests) is never sent over the wire.
    """

    id: str
    name: str
    email: EmailStr
    rut: str
    plan: ClientPlan
    status: str


class RotateKeyResponse(BaseModel):
    """Response shape for a successful API key rotation."""

    api_key: str
    api_key_last4: str
    rotated_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_summary(client: Client) -> ClientSummary:
    """Build a :class:`ClientSummary` from a :class:`Client` row.

    Defined as a module-private helper so the two endpoints
    that need it (``/login`` and ``/me``) share the same
    projection. Keeping the projection out of the model file
    itself means the service layer does not have to import
    FastAPI / Pydantic just to build a response.
    """
    return ClientSummary(
        id=client.id,
        name=client.name,
        email=client.email,
        rut=client.rut,
        plan=client.plan,
        status=client.status.value,
    )


def _register_response(client: Client, api_key: str) -> RegisterResponse:
    """Build a :class:`RegisterResponse` from a registration result.

    Same pattern as :func:`_client_summary`: factor the
    projection out of the handlers so the response shape is
    defined in one place.
    """
    return RegisterResponse(
        id=client.id,
        name=client.name,
        email=client.email,
        rut=client.rut,
        plan=client.plan,
        status=client.status.value,
        api_key=api_key,
        api_key_last4=client.api_key_last4,
        created_at=client.created_at,
    )


def _raise_auth_error(exc: AuthError) -> None:
    """Convert an :class:`AuthError` into the matching HTTPException.

    The mapping is centralised so the route handlers do not
    have to know which HTTP status each domain error maps to.
    Adding a new error class is a one-line change in
    :mod:`app.services.auth` (override ``http_status``) plus
    no changes here.
    """
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    session: AsyncSession = Depends(get_db),
) -> Client:
    """FastAPI dependency that resolves ``X-API-Key`` to a :class:`Client`.

    Used by any future endpoint that requires API-key auth (the
    message-send, template and webhook routes wire this in as
    they land). The dependency returns the :class:`Client`
    instance so handlers can call ``current_client.id`` without
    having to re-query the database.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_api_key", "message": "X-API-Key header is required"},
        )
    try:
        return await authenticate_api_key(session, api_key=x_api_key)
    except InvalidApiKeyError as exc:
        _raise_auth_error(exc)
        # Unreachable: ``_raise_auth_error`` always raises.
        # The explicit ``raise`` keeps mypy's flow analysis
        # honest and makes the contract obvious to a future
        # reader who is wondering "what happens if the
        # exception is not raised?".
        raise AssertionError("unreachable") from exc  # pragma: no cover


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Client registered; ``api_key`` is shown only once."},
        409: {"description": "A client with the same email or RUT already exists."},
        422: {"description": "One or more fields failed validation."},
    },
)
async def register(
    payload: RegisterRequest, session: AsyncSession = Depends(get_db)
) -> RegisterResponse:
    """Create a new client and mint a fresh API key.

    The endpoint follows the PRD contract: the response carries
    the **plain** API key exactly once, and the dashboard is
    expected to surface it to the user before they navigate
    away. The platform does not store the plain key and
    therefore cannot re-issue it later – losing the value
    means calling ``POST /v1/auth/api-keys/rotate`` to mint
    a new one.
    """
    try:
        result = await register_client(
            session,
            name=payload.name,
            email=payload.email,
            rut=payload.rut,
            password=payload.password,
            plan=payload.plan or ClientPlan.STARTER,
        )
    except InvalidInputError as exc:
        _raise_auth_error(exc)
    except DuplicateIdentityError as exc:
        _raise_auth_error(exc)
    return _register_response(result.client, result.api_key)


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={
        200: {"description": "Login succeeded; ``token`` is a short-lived JWT."},
        401: {"description": "The email / password pair is invalid."},
        403: {"description": "The matched client is not active."},
    },
)
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_db)) -> LoginResponse:
    """Authenticate the dashboard user and mint a session token.

    The endpoint accepts a JSON body (``email`` + ``password``)
    rather than HTTP Basic Auth because the Angular dashboard
    sends the body as a ``POST`` already; forcing Basic Auth
    would mean the dashboard does a redundant string
    concatenation on the way out.
    """
    try:
        result = await authenticate_login(
            session,
            email=payload.email,
            password=payload.password,
        )
    except InvalidCredentialsError as exc:
        _raise_auth_error(exc)
    except InactiveClientError as exc:
        _raise_auth_error(exc)
    return LoginResponse(
        token=result.token,
        token_type="bearer",
        expires_at=result.expires_at,
        client=_client_summary(result.client),
    )


@router.post(
    "/api-keys/rotate",
    response_model=RotateKeyResponse,
    responses={
        200: {"description": "API key rotated; the new plain key is returned exactly once."},
        401: {"description": "The X-API-Key header is missing or invalid."},
    },
)
async def rotate_key(
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> RotateKeyResponse:
    """Mint a new API key for the current client.

    Requires the existing API key to be valid (the
    ``require_api_key`` dependency short-circuits the
    request otherwise). The previous key is invalidated
    immediately and cannot be recovered.
    """
    new_key = await rotate_api_key(session, client=current_client)
    return RotateKeyResponse(
        api_key=new_key,
        api_key_last4=current_client.api_key_last4,
        rotated_at=datetime.utcnow(),
    )


@router.get(
    "/me",
    response_model=ClientSummary,
    responses={
        200: {"description": "The current client."},
        401: {"description": "The X-API-Key header is missing or invalid."},
    },
)
async def me(current_client: Client = Depends(require_api_key)) -> ClientSummary:
    """Return the client identified by the ``X-API-Key`` header.

    Useful for the dashboard on first paint: a single request
    confirms "the key is valid" *and* returns the metadata
    needed to render the top bar. The endpoint is
    deliberately narrow (it does not list messages or
    templates) so it stays cheap to call on every page
    navigation.
    """
    return _client_summary(current_client)


# ---------------------------------------------------------------------------
# Internal: dashboard session validation
#
# The dashboard sends the JWT minted by ``/login`` as a
# ``Bearer`` token. This helper is exported so future
# dashboard-facing endpoints can validate the session without
# re-implementing the JWT decode logic. It is **not** mounted
# on the router because it is a dependency, not an endpoint.
# ---------------------------------------------------------------------------


async def require_session(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, object]:
    """FastAPI dependency that validates the dashboard session token.

    Returns the decoded JWT payload so handlers can pull the
    ``sub`` (client id) without re-decoding. The same
    exception-to-HTTP mapping is used as for the API-key
    dependency.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_token", "message": "Authorization header is required"},
        )
    token = authorization.split(" ", 1)[1].strip()
    try:
        return decode_session_token(token)
    except InvalidCredentialsError as exc:
        _raise_auth_error(exc)
        # Unreachable – see ``require_api_key`` for the rationale.
        raise AssertionError("unreachable") from exc  # pragma: no cover
