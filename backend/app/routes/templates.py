"""WhatsApp template CRUD endpoints (issue #8).

Implements the public surface documented in the PRD:

- ``POST   /v1/templates``       – create a new template.
- ``GET    /v1/templates``       – list the customer's templates.
- ``GET    /v1/templates/{id}``  – fetch a single template.
- ``PUT    /v1/templates/{id}``  – update a template's mutable
                                    fields.
- ``DELETE /v1/templates/{id}``  – remove a template.

All endpoints require a valid ``X-API-Key`` header (the
``require_api_key`` dependency in :mod:`app.routes.auth` is
the single source of truth for API-key authentication). The
domain logic lives in :mod:`app.services.templates`; this
module only translates the HTTP request into a service call
and renders the response.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.client import Client
from app.models.whatsapp_template import (
    WhatsAppTemplate,
    WhatsAppTemplateCategory,
    WhatsAppTemplateStatus,
)
from app.routes.auth import require_api_key
from app.services.templates import (
    DuplicateTemplateError,
    InvalidTemplateError,
    TemplateError,
    TemplateImmutableError,
    TemplateNotFoundError,
    TemplateResult,
    create_template,
    delete_template,
    get_template,
    list_templates,
    template_to_dict,
    update_template,
)

router = APIRouter(prefix="/templates", tags=["templates"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ComponentSpec(BaseModel):
    """Single Meta-side component in a template's ``components`` array.

    The MVP does not validate the deep shape of each component
    – Meta's WABA endpoint is the source of truth for that –
    but the API requires every component to declare a
    ``type`` (``"HEADER"`` / ``"BODY"`` / ``"FOOTER"`` /
    ``"BUTTONS"``). The route layer forwards the value as-is
    to the service layer, which serialises it to JSON.
    """

    type: str = Field(..., min_length=1, max_length=32)
    # The Meta spec lets every component carry arbitrary
    # additional fields (``text``, ``format``, ``buttons``,
    # …). The platform stores them as a free-form ``dict``
    # so the schema can evolve without breaking older
    # clients.
    model_config = {"extra": "allow"}


class CreateTemplateRequest(BaseModel):
    """Body of ``POST /v1/templates``."""

    name: str = Field(..., min_length=1, max_length=512)
    language: str = Field(..., min_length=1, max_length=16)
    # ``category`` is optional: when omitted, the service
    # falls back to ``utility`` (Meta's default for the
    # common transactional templates).
    category: WhatsAppTemplateCategory | None = None
    # ``components`` is optional at the API edge: the
    # customer can create a "shell" template and fill the
    # components in later. The service layer still validates
    # the blob and stores it as ``[]`` when missing.
    components: list[ComponentSpec] | None = None
    description: str | None = Field(default=None, max_length=500)


class UpdateTemplateRequest(BaseModel):
    """Body of ``PUT /v1/templates/{id}``.

    Every field is optional – a customer can patch the
    ``description`` without re-sending the rest of the
    template. The service layer is responsible for the
    "only mutable while in DRAFT" rule.
    """

    name: str | None = Field(default=None, min_length=1, max_length=512)
    language: str | None = Field(default=None, min_length=1, max_length=16)
    category: WhatsAppTemplateCategory | None = None
    components: list[ComponentSpec] | None = None
    description: str | None = Field(default=None, max_length=500)


class TemplateResponse(BaseModel):
    """Projection of a :class:`WhatsAppTemplate` row for the public API.

    Mirrors the dict returned by
    :func:`app.services.templates.template_to_dict` so the
    response is rendered through Pydantic and the OpenAPI
    schema documents the shape.
    """

    id: str
    client_id: str
    name: str
    language: str
    category: str
    status: str
    meta_template_id: str | None
    rejection_reason: str | None
    description: str | None
    components: list[dict[str, Any]]
    created_at: str | None
    updated_at: str | None
    submitted_at: str | None


class ListTemplatesResponse(BaseModel):
    """Response of a successful ``GET /v1/templates``."""

    templates: list[TemplateResponse]
    count: int


class CreateTemplateResponse(BaseModel):
    """Response of a successful ``POST /v1/templates``."""

    template: TemplateResponse


class UpdateTemplateResponse(BaseModel):
    """Response of a successful ``PUT /v1/templates/{id}``."""

    template: TemplateResponse


class DeleteTemplateResponse(BaseModel):
    """Response of a successful ``DELETE /v1/templates/{id}``."""

    id: str
    deleted: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_response(template: WhatsAppTemplate) -> TemplateResponse:
    """Project a :class:`WhatsAppTemplate` row to a :class:`TemplateResponse`."""
    return TemplateResponse(**template_to_dict(template))


def _components_to_dicts(
    components: list[ComponentSpec] | None,
) -> list[dict[str, Any]] | None:
    """Convert the Pydantic-validated components into plain dicts.

    The service layer accepts either a list of dicts or a
    pre-serialised JSON string; passing the parsed dicts
    keeps the error path (malformed JSON, missing ``type``)
    inside the validator.
    """
    if components is None:
        return None
    return [component.model_dump(exclude_none=True) for component in components]


def _raise_template_error(exc: TemplateError) -> None:
    """Convert a :class:`TemplateError` into the matching HTTPException.

    Centralised so the handlers do not have to know which
    HTTP status each domain error maps to. Adding a new
    :class:`TemplateError` subclass is a one-line change
    (override ``http_status`` / ``code``) and no edits
    here.
    """
    raise HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CreateTemplateResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Template created in DRAFT status."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        409: {"description": "A template with the same name/language already exists."},
        422: {"description": "The request body failed validation."},
    },
)
async def create(
    payload: CreateTemplateRequest,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> CreateTemplateResponse:
    """Create a new WhatsApp template for the current client.

    The new row starts in :attr:`WhatsAppTemplateStatus.DRAFT`.
    A future iteration will add a ``POST /v1/templates/{id}/submit``
    endpoint that forwards the template to Meta's WABA
    endpoint; for the MVP, the customer (or a follow-up
    background job) drives the transition to ``pending``
    by re-saving the row.
    """
    try:
        result: TemplateResult = await create_template(
            session,
            client=current_client,
            name=payload.name,
            language=payload.language,
            category=payload.category or WhatsAppTemplateCategory.UTILITY,
            components=_components_to_dicts(payload.components) or [],
            description=payload.description,
        )
    except (InvalidTemplateError, DuplicateTemplateError) as exc:
        _raise_template_error(exc)
    return CreateTemplateResponse(template=_to_response(result.template))


@router.get(
    "",
    response_model=ListTemplatesResponse,
    responses={
        200: {"description": "The customer's templates, newest first."},
        401: {"description": "The X-API-Key header is missing or invalid."},
    },
)
async def list_(
    status_filter: WhatsAppTemplateStatus | None = Query(
        default=None,
        alias="status",
        description="Filter templates by lifecycle state.",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of templates to return.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of templates to skip (pagination).",
    ),
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> ListTemplatesResponse:
    """List the customer's WhatsApp templates, newest first.

    The endpoint supports a ``status`` filter and basic
    ``limit`` / ``offset`` pagination so the dashboard can
    render the "Pending review" tab without re-fetching the
    full list.
    """
    templates = await list_templates(
        session,
        client=current_client,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return ListTemplatesResponse(
        templates=[_to_response(template) for template in templates],
        count=len(templates),
    )


@router.get(
    "/{template_id}",
    response_model=TemplateResponse,
    responses={
        200: {"description": "The current state of the template."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        404: {"description": "The template does not exist (or belongs to another client)."},
    },
)
async def get_one(
    template_id: str,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> TemplateResponse:
    """Read the current state of a single template.

    A template that belongs to a different client is
    reported as ``not_found`` (not ``forbidden``) so the
    existence of another tenant's resource is not leaked.
    """
    try:
        template = await get_template(
            session,
            client=current_client,
            template_id=template_id,
        )
    except TemplateNotFoundError as exc:
        _raise_template_error(exc)
    return _to_response(template)


@router.put(
    "/{template_id}",
    response_model=UpdateTemplateResponse,
    responses={
        200: {"description": "Template updated."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        404: {"description": "The template does not exist (or belongs to another client)."},
        409: {
            "description": (
                "The template is no longer editable (already submitted) or "
                "the new name/language collides with another template."
            )
        },
        422: {"description": "The request body failed validation."},
    },
)
async def update(
    template_id: str,
    payload: UpdateTemplateRequest,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> UpdateTemplateResponse:
    """Update a template's mutable fields.

    Only templates in :attr:`WhatsAppTemplateStatus.DRAFT`
    can be updated – once a template has been submitted to
    Meta, the upstream owns the canonical shape. A future
    iteration can let Meta push the new version back into
    the row, but the MVP keeps the customer edit window
    narrow.
    """
    try:
        result: TemplateResult = await update_template(
            session,
            client=current_client,
            template_id=template_id,
            name=payload.name,
            language=payload.language,
            category=payload.category,
            components=_components_to_dicts(payload.components),
            description=payload.description,
        )
    except TemplateNotFoundError as exc:
        _raise_template_error(exc)
    except (InvalidTemplateError, DuplicateTemplateError, TemplateImmutableError) as exc:
        _raise_template_error(exc)
    return UpdateTemplateResponse(template=_to_response(result.template))


@router.delete(
    "/{template_id}",
    response_model=DeleteTemplateResponse,
    responses={
        200: {"description": "Template deleted."},
        401: {"description": "The X-API-Key header is missing or invalid."},
        404: {"description": "The template does not exist (or belongs to another client)."},
    },
)
async def delete(
    template_id: str,
    current_client: Client = Depends(require_api_key),
    session: AsyncSession = Depends(get_db),
) -> DeleteTemplateResponse:
    """Delete a template owned by the current client.

    The endpoint is idempotent at the service layer – a
    second delete call on the same id returns 404, mirroring
    the read-path contract.
    """
    try:
        await delete_template(
            session,
            client=current_client,
            template_id=template_id,
        )
    except TemplateNotFoundError as exc:
        _raise_template_error(exc)
    return DeleteTemplateResponse(id=template_id, deleted=True)
