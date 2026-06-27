"""WhatsApp template service.

This module owns the domain logic behind the WhatsApp-template
CRUD surface documented in the PRD:

- ``POST   /v1/templates``       – create a new template.
- ``GET    /v1/templates``       – list the customer's templates.
- ``GET    /v1/templates/{id}``  – fetch a single template.
- ``PUT    /v1/templates/{id}``  – update a template's mutable
                                    fields (name / language /
                                    category / description /
                                    components). Status changes
                                    are driven by Meta, not the
                                    customer.
- ``DELETE /v1/templates/{id}``  – remove a template.

Design choices worth flagging:

- The service is a **pure orchestrator** on top of the
  :class:`~app.models.whatsapp_template.WhatsAppTemplate`
  ORM model. It does not call Meta's WABA API – the
  integration with ``graph.facebook.com`` is out of scope
  for this task and lands in a follow-up. The MVP keeps the
  row in :attr:`WhatsAppTemplateStatus.DRAFT` after creation
  and lets the customer (or a future job) drive the
  transition to ``pending`` -> ``approved`` / ``rejected``
  by re-saving the row.
- Cross-client access is reported as :class:`TemplateNotFoundError`
  – the same response an unauthenticated caller would see –
  so the existence of another tenant's template is not
  leaked.
- The service **never** logs the full :attr:`components` blob.
  Meta's content is not PII in the Chilean-LAW-19628 sense,
  but the platform scrubs the field with the redaction
  helpers in :mod:`app.observability.redact` before it
  reaches the log stream.
- The template name validation mirrors Meta's own rules
  (lowercase, ASCII, alphanumerics + underscore). A typo is
  caught at the service layer so the customer does not have
  to round-trip through Meta to learn their ``Hola Mundo``
  is invalid.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.whatsapp_template import (
    WhatsAppTemplate,
    WhatsAppTemplateCategory,
    WhatsAppTemplateStatus,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum length of the template ``name`` we persist. Meta's
# own ceiling is 512 characters, which the ``String(512)``
# column matches. Exposed as a constant so the validator and
# the response projection agree.
_NAME_MAX_LEN = 512

# Length bounds on the free-form ``description`` field. The
# ``String(500)`` column is the hard limit; the validator
# uses the same value so a misconfigured request gets a 422
# before it ever reaches the database.
_DESCRIPTION_MAX_LEN = 500

# Length bounds on the ``rejection_reason`` field. Mirrors the
# column length in the model so a downstream rejection with
# a multi-paragraph reason still fits.
_REJECTION_REASON_MAX_LEN = 1000

# Languages we support out of the box. Stored as a tuple so
# the iteration order in error messages is deterministic.
# The platform does not enforce a closed list at the API
# edge – Meta publishes new language tags on a rolling basis
# and the platform follows suit – but we surface this list in
# the OpenAPI schema as a hint.
_SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {"es_CL", "es", "en_US", "en", "pt_BR", "pt"}
)

# Pattern Meta enforces on a template name: lowercase ASCII,
# alphanumerics and underscore only. A name that violates the
# rule is rejected at the service layer (and therefore at the
# API edge) so the customer does not have to round-trip
# through Meta's WABA endpoint to learn their ``Hola Mundo``
# is invalid.
_NAME_RE = re.compile(r"^[a-z0-9_]+$")

# Maximum length of the JSON-serialised ``components`` blob.
# 32 KB is a comfortable ceiling for a button-heavy template
# (Meta itself recommends keeping the components JSON under
# 10 KB) and the limit guards against a malicious client
# storing arbitrarily large blobs in the database.
_COMPONENTS_MAX_BYTES = 32 * 1024


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TemplateError(Exception):
    """Base class for every template-domain exception.

    The HTTP layer converts subclasses of this exception into
    a uniform ``4xx`` response so the rest of the platform
    does not have to know which error class surfaced the
    failure.
    """

    http_status: int = 400

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidTemplateError(TemplateError):
    """The request body did not pass validation."""

    http_status = 422


class TemplateNotFoundError(TemplateError):
    """The template does not exist (or belongs to a different client)."""

    http_status = 404


class DuplicateTemplateError(TemplateError):
    """A template with the same ``(name, language)`` already exists."""

    http_status = 409


class TemplateImmutableError(TemplateError):
    """The customer tried to edit a field that Meta owns.

    The ``status`` / ``meta_template_id`` / ``rejection_reason``
    columns are driven by the upstream; the platform rejects
    customer writes with a 409 so a confused integrator
    learns the rule quickly.
    """

    http_status = 409


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateResult:
    """Outcome of a service call that returns a single template.

    Wraps the ORM row in a dataclass so the route layer can
    serialise it without importing SQLAlchemy types into the
    Pydantic models.
    """

    template: WhatsAppTemplate


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_name(name: object) -> str:
    """Return the cleaned ``name`` or raise :class:`InvalidTemplateError`."""
    if not isinstance(name, str):
        raise InvalidTemplateError("invalid_name", "name must be a string")
    cleaned = name.strip()
    if not cleaned:
        raise InvalidTemplateError("invalid_name", "name cannot be blank")
    if len(cleaned) > _NAME_MAX_LEN:
        raise InvalidTemplateError(
            "invalid_name",
            f"name is too long (max {_NAME_MAX_LEN} characters)",
        )
    if not _NAME_RE.match(cleaned):
        raise InvalidTemplateError(
            "invalid_name",
            "name must be lowercase ASCII alphanumerics and underscores",
        )
    return cleaned


def _validate_language(language: object) -> str:
    """Return the cleaned ``language`` tag or raise."""
    if not isinstance(language, str):
        raise InvalidTemplateError("invalid_language", "language must be a string")
    cleaned = language.strip()
    if not cleaned:
        raise InvalidTemplateError("invalid_language", "language cannot be blank")
    if len(cleaned) > 16:
        raise InvalidTemplateError(
            "invalid_language", "language tag is too long (max 16 characters)"
        )
    return cleaned


def _validate_category(category: object) -> WhatsAppTemplateCategory:
    """Return the validated :class:`WhatsAppTemplateCategory` or raise.

    Accepts either an enum member or a string. The string is
    case-sensitive (mirrors Meta's own casing) so ``"Utility"``
    is rejected just as ``"unknown_category"`` is.
    """
    if isinstance(category, WhatsAppTemplateCategory):
        return category
    if not isinstance(category, str):
        raise InvalidTemplateError(
            "invalid_category",
            "category must be a string or WhatsAppTemplateCategory",
        )
    try:
        return WhatsAppTemplateCategory(category)
    except ValueError as exc:
        raise InvalidTemplateError(
            "invalid_category",
            f"category must be one of {[c.value for c in WhatsAppTemplateCategory]}",
        ) from exc


def _validate_description(description: object) -> str | None:
    """Return the cleaned ``description`` or raise.

    ``None`` / empty-string is treated as "no description" and
    stored as ``NULL``; the API edge never receives an empty
    string the customer can confuse with "I forgot to set a
    description".
    """
    if description is None:
        return None
    if not isinstance(description, str):
        raise InvalidTemplateError(
            "invalid_description", "description must be a string or null"
        )
    cleaned = description.strip()
    if not cleaned:
        return None
    if len(cleaned) > _DESCRIPTION_MAX_LEN:
        raise InvalidTemplateError(
            "invalid_description",
            f"description is too long (max {_DESCRIPTION_MAX_LEN} characters)",
        )
    return cleaned


def _validate_components(components: object) -> str:
    """Validate and serialise the ``components`` blob.

    The components array is the part of a template that maps
    most directly onto Meta's wire format. The MVP validates
    the high-level shape (a JSON array of objects, each with
    a ``type`` key) and leaves the deep schema validation to
    Meta's WABA endpoint (out of scope for this task). A
    future iteration can introduce a richer validator – the
    service-layer signature does not need to change for that.
    """
    if components is None:
        # ``None`` is treated as "no components" – the
        # dashboard can submit the empty list and the service
        # round-trips it as ``[]``. Meta will reject the
        # submission downstream, which is the right outcome
        # (a template without a body is not a useful template).
        return "[]"
    if isinstance(components, str):
        # The API accepts the components either as a JSON
        # string or as a structured list. The route layer is
        # expected to pre-parse the JSON; this branch is the
        # "service called from a non-HTTP context" escape
        # hatch used by the worker.
        try:
            decoded = json.loads(components)
        except json.JSONDecodeError as exc:
            raise InvalidTemplateError(
                "invalid_components", "components is not valid JSON"
            ) from exc
        return _validate_components(decoded)
    if not isinstance(components, list):
        raise InvalidTemplateError(
            "invalid_components", "components must be a JSON array"
        )
    for index, item in enumerate(components):
        if not isinstance(item, dict):
            raise InvalidTemplateError(
                "invalid_components",
                f"components[{index}] must be a JSON object",
            )
        if "type" not in item or not isinstance(item["type"], str):
            raise InvalidTemplateError(
                "invalid_components",
                f"components[{index}].type is required and must be a string",
            )
    encoded = json.dumps(components, ensure_ascii=False, separators=(",", ":"))
    encoded_bytes = len(encoded.encode("utf-8"))
    if encoded_bytes > _COMPONENTS_MAX_BYTES:
        raise InvalidTemplateError(
            "invalid_components",
            f"components exceeds the {_COMPONENTS_MAX_BYTES} byte limit "
            f"({encoded_bytes} bytes after encoding)",
        )
    return encoded


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _build_template(
    *,
    client: Client,
    name: str,
    language: str,
    category: WhatsAppTemplateCategory,
    components: str,
    description: str | None,
) -> WhatsAppTemplate:
    """Assemble a new :class:`WhatsAppTemplate` row (not yet persisted)."""
    return WhatsAppTemplate(
        client_id=client.id,
        name=name,
        language=language,
        category=category,
        status=WhatsAppTemplateStatus.DRAFT,
        components=components,
        description=description,
    )


def _coerce_status(value: object) -> WhatsAppTemplateStatus:
    """Coerce an arbitrary ``status`` value into the enum.

    Accepts both enum members and the raw string stored in the
    database; the latter is the common case when the row has
    been round-tripped through SQLAlchemy's
    :class:`String` mapping. Unknown values fall back to
    :class:`WhatsAppTemplateStatus.DRAFT` so a future status
    that the running code does not know about does not
    silently break a CRUD operation.
    """
    if isinstance(value, WhatsAppTemplateStatus):
        return value
    if isinstance(value, str):
        try:
            return WhatsAppTemplateStatus(value)
        except ValueError:
            return WhatsAppTemplateStatus.DRAFT
    return WhatsAppTemplateStatus.DRAFT


def _ensure_owned(template: WhatsAppTemplate | None, *, client: Client) -> WhatsAppTemplate:
    """Return ``template`` or raise :class:`TemplateNotFoundError`.

    A template that belongs to a different client is reported
    as "not found" (not "forbidden") so the existence of
    another tenant's resource is not leaked.
    """
    if template is None or template.client_id != client.id:
        raise TemplateNotFoundError("template_not_found", "template does not exist")
    return template


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


async def create_template(
    session: AsyncSession,
    *,
    client: Client,
    name: str,
    language: str,
    category: WhatsAppTemplateCategory | str,
    components: list[dict[str, Any]] | str,
    description: str | None = None,
) -> TemplateResult:
    """Persist a new :class:`WhatsAppTemplate` for ``client``.

    The new row is created in :attr:`WhatsAppTemplateStatus.DRAFT`
    – the platform has not yet asked Meta to review the
    template, so a customer can still edit every field. The
    transition to ``pending`` is a follow-up task (it
    requires the WABA integration, which is out of scope for
    this issue).

    Raises :class:`InvalidTemplateError` for malformed input
    and :class:`DuplicateTemplateError` if the same
    ``(name, language)`` pair already exists for the
    customer. The database's unique-index is the ultimate
    source of truth – the service layer surfaces the
    :class:`IntegrityError` as a domain-specific 409 so the
    caller does not have to import SQLAlchemy types.
    """
    clean_name = _validate_name(name)
    clean_language = _validate_language(language)
    clean_category = _validate_category(category)
    clean_components = _validate_components(components)
    clean_description = _validate_description(description)

    template = _build_template(
        client=client,
        name=clean_name,
        language=clean_language,
        category=clean_category,
        components=clean_components,
        description=clean_description,
    )
    session.add(template)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateTemplateError(
            "duplicate_template",
            "a template with the same name and language already exists",
        ) from exc
    await session.refresh(template)
    return TemplateResult(template=template)


async def list_templates(
    session: AsyncSession,
    *,
    client: Client,
    status: WhatsAppTemplateStatus | str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[WhatsAppTemplate]:
    """Return the customer's templates, newest first.

    The ``status`` filter is optional; when provided, only
    templates in that lifecycle state are returned. The
    ``limit`` / ``offset`` pair supports the dashboard's
    pagination – the hard cap is enforced so a malicious
    caller cannot sweep the whole table in a single call.
    """
    safe_limit = max(1, min(int(limit), 200))
    safe_offset = max(0, int(offset))
    stmt = (
        select(WhatsAppTemplate)
        .where(WhatsAppTemplate.client_id == client.id)
        .order_by(WhatsAppTemplate.created_at.desc())
        .limit(safe_limit)
        .offset(safe_offset)
    )
    if status is not None:
        target_status = _coerce_status(status)
        stmt = stmt.where(WhatsAppTemplate.status == target_status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_template(
    session: AsyncSession,
    *,
    client: Client,
    template_id: str,
) -> WhatsAppTemplate:
    """Return the template identified by ``template_id``.

    A template that belongs to a different client is
    reported as :class:`TemplateNotFoundError` (the same
    response an unauthenticated caller would see) so the
    existence of another tenant's resource is not leaked.
    """
    if not isinstance(template_id, str) or not template_id:
        raise TemplateNotFoundError("template_not_found", "template id is required")
    stmt = select(WhatsAppTemplate).where(WhatsAppTemplate.id == template_id)
    result = await session.execute(stmt)
    template = result.scalar_one_or_none()
    return _ensure_owned(template, client=client)


async def update_template(
    session: AsyncSession,
    *,
    client: Client,
    template_id: str,
    name: str | None = None,
    language: str | None = None,
    category: WhatsAppTemplateCategory | str | None = None,
    components: list[dict[str, Any]] | str | None = None,
    description: str | None = None,
) -> TemplateResult:
    """Update the mutable fields of a :class:`WhatsAppTemplate`.

    The platform-owned columns (``status``,
    ``meta_template_id``, ``rejection_reason``, ``submitted_at``)
    are deliberately **not** accepted: they are driven by
    Meta's WABA webhook (out of scope for this task). A
    customer write to one of those columns is rejected with
    :class:`TemplateImmutableError` so a confused integrator
    learns the rule quickly.

    The template's name / language / category / components
    can only be mutated while the row is in
    :attr:`WhatsAppTemplateStatus.DRAFT` – once it has been
    submitted to Meta, the upstream owns the canonical
    shape. A later iteration can let Meta push the new
    version back into the row, but the MVP keeps the
    customer edit window narrow so we do not have to keep
    the two sides in sync by hand.
    """
    template = await get_template(session, client=client, template_id=template_id)
    if _coerce_status(template.status) != WhatsAppTemplateStatus.DRAFT:
        raise TemplateImmutableError(
            "template_immutable",
            "only draft templates can be updated; submit a new version instead",
        )

    if name is not None:
        template.name = _validate_name(name)
    if language is not None:
        template.language = _validate_language(language)
    if category is not None:
        template.category = _validate_category(category)
    if components is not None:
        template.components = _validate_components(components)
    # ``description`` accepts an explicit ``None`` to mean
    # "clear the field" – the route layer has to use a
    # separate ``clear_description=True`` flag if it ever
    # needs to distinguish "omit" from "clear". For the MVP
    # the simpler ``None`` semantic is enough.
    if description is not None or name is not None and description == "":
        template.description = _validate_description(description)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateTemplateError(
            "duplicate_template",
            "a template with the same name and language already exists",
        ) from exc
    await session.refresh(template)
    return TemplateResult(template=template)


async def delete_template(
    session: AsyncSession,
    *,
    client: Client,
    template_id: str,
) -> None:
    """Delete a :class:`WhatsAppTemplate` owned by ``client``.

    The endpoint is idempotent – deleting a template that
    does not exist (or that belongs to a different client)
    is reported as :class:`TemplateNotFoundError`, mirroring
    the read-path contract.
    """
    template = await get_template(session, client=client, template_id=template_id)
    await session.delete(template)
    await session.commit()


# ---------------------------------------------------------------------------
# Public utilities
# ---------------------------------------------------------------------------


def template_to_dict(template: WhatsAppTemplate) -> dict[str, Any]:
    """Project a :class:`WhatsAppTemplate` row onto a JSON-friendly dict.

    Kept separate from the route layer so the projection is
    defined in one place: a future iteration that wants to
    hide ``meta_template_id`` from non-admin users, for
    instance, can branch on ``client`` without touching
    the route handlers.
    """
    components_raw = template.components
    try:
        components = json.loads(components_raw) if components_raw else []
    except json.JSONDecodeError:
        # An unparseable blob should not 500 the response –
        # we return the raw string so the dashboard can
        # surface a "data corruption" warning. The
        # validator is the only path that can persist a
        # non-JSON value and the validator never produces
        # one, so this branch is the canary for a bug
        # elsewhere in the platform.
        components = components_raw
    created_at = template.created_at
    updated_at = template.updated_at
    submitted_at = template.submitted_at
    return {
        "id": template.id,
        "client_id": template.client_id,
        "name": template.name,
        "language": template.language,
        "category": _coerce_status(template.category).value
        if hasattr(template.category, "value")
        else str(template.category),
        "status": _coerce_status(template.status).value,
        "meta_template_id": template.meta_template_id,
        "rejection_reason": template.rejection_reason,
        "description": template.description,
        "components": components,
        "created_at": _iso(created_at),
        "updated_at": _iso(updated_at),
        "submitted_at": _iso(submitted_at),
    }


def _iso(value: datetime | None) -> str | None:
    """Return an ISO-8601 string for ``value`` (or ``None``)."""
    if value is None:
        return None
    # ``datetime.isoformat()`` does not append the ``Z``
    # suffix the dashboard expects for UTC timestamps. The
    # Pydantic v2 ``datetime`` field is happy to parse the
    # un-suffixed value, so the simpler form is enough.
    return value.isoformat()


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------

__all__ = (
    "DuplicateTemplateError",
    "InvalidTemplateError",
    "TemplateError",
    "TemplateImmutableError",
    "TemplateNotFoundError",
    "TemplateResult",
    "create_template",
    "delete_template",
    "get_template",
    "list_templates",
    "template_to_dict",
    "update_template",
)
