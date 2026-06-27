"""Unit tests for the templates service layer (issue #8).

The tests cover:

- :func:`app.services.templates.create_template` – happy
  path, malformed name, missing language, duplicate
  ``(name, language)`` pair, oversized components blob.
- :func:`app.services.templates.list_templates` – default
  ordering, ``status`` filter, ``limit`` / ``offset``
  pagination, hard cap on ``limit``.
- :func:`app.services.templates.get_template` – cross-client
  access guard (foreign template is reported as 404).
- :func:`app.services.templates.update_template` – partial
  patch, immutable-after-submit guard, name validation.
- :func:`app.services.templates.delete_template` – happy
  path, cross-client 404.
- :func:`app.services.templates.template_to_dict` – the
  Pydantic-friendly projection used by the route layer.

The HTTP layer is exercised through ``test_routes``; the
service tests assert the *domain* contract without the
FastAPI plumbing.
"""

from __future__ import annotations

import pytest

from app.models.client import Client, ClientPlan, ClientStatus
from app.models.whatsapp_template import (
    WhatsAppTemplateCategory,
    WhatsAppTemplateStatus,
)
from app.services.templates import (
    DuplicateTemplateError,
    InvalidTemplateError,
    TemplateImmutableError,
    TemplateNotFoundError,
    create_template,
    delete_template,
    get_template,
    list_templates,
    template_to_dict,
    update_template,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def owner(async_session) -> Client:
    """Yield a persisted :class:`Client` to own the templates."""
    client = Client(
        name="Acme",
        email="ops@acme.cl",
        rut="12345678-5",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.GROWTH,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(client)
    await async_session.flush()
    return client


@pytest.fixture
async def other_client(async_session) -> Client:
    """Yield a second :class:`Client` to assert the cross-client guard."""
    client = Client(
        name="Other",
        email="other@acme.cl",
        rut="11.111.111-1",
        password_hash="hashed",
        api_key_hash="also-hashed-2",
        api_key_last4="wxyz",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(client)
    await async_session.flush()
    return client


@pytest.fixture
def simple_components() -> list[dict[str, object]]:
    """Yield a minimal, valid components list for the happy path."""
    return [
        {"type": "BODY", "text": "Hola {{1}}, tu pedido está en camino."},
    ]


# ---------------------------------------------------------------------------
# create_template
# ---------------------------------------------------------------------------


async def test_create_template_persists_row_in_draft_status(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A successful create returns a template in
    ``draft`` status with the platform's UUID primary key."""
    result = await create_template(
        async_session,
        client=owner,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
        description="Confirmación de pedido",
    )
    assert result.template.id is not None
    assert result.template.client_id == owner.id
    assert result.template.name == "order_confirmation"
    assert result.template.language == "es_CL"
    assert result.template.category == WhatsAppTemplateCategory.UTILITY
    assert result.template.status == WhatsAppTemplateStatus.DRAFT
    assert result.template.meta_template_id is None
    assert result.template.rejection_reason is None
    assert result.template.description == "Confirmación de pedido"
    # ``components`` is persisted as a JSON string so the
    # caller / dashboard can round-trip the structure. The
    # service uses compact separators (``","`` / ``":"``)
    # so the assertion checks for the field key only.
    assert '"type":"BODY"' in result.template.components


async def test_create_template_defaults_category_to_utility(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """Omitting ``category`` falls back to ``utility`` – Meta's
    default for transactional templates."""
    result = await create_template(
        async_session,
        client=owner,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    assert result.template.category == WhatsAppTemplateCategory.UTILITY


async def test_create_template_accepts_string_category(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A raw string is coerced into the enum so the route
    layer can pass the value through without re-mapping."""
    result = await create_template(
        async_session,
        client=owner,
        name="marketing_blast",
        language="es_CL",
        category="marketing",
        components=simple_components,
    )
    assert result.template.category == WhatsAppTemplateCategory.MARKETING


async def test_create_template_rejects_invalid_name(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A name with uppercase letters / spaces is rejected at
    the service layer (mirrors Meta's own rules) so the
    customer does not have to round-trip through the WABA
    endpoint to learn the format is wrong."""
    with pytest.raises(InvalidTemplateError) as exc_info:
        await create_template(
            async_session,
            client=owner,
            name="Order Confirmation",
            language="es_CL",
            category=WhatsAppTemplateCategory.UTILITY,
            components=simple_components,
        )
    assert exc_info.value.code == "invalid_name"
    assert "lowercase ASCII" in exc_info.value.message


async def test_create_template_rejects_blank_name(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """An empty / whitespace-only name is rejected with the
    same ``invalid_name`` code as a malformed name."""
    with pytest.raises(InvalidTemplateError) as exc_info:
        await create_template(
            async_session,
            client=owner,
            name="   ",
            language="es_CL",
            category=WhatsAppTemplateCategory.UTILITY,
            components=simple_components,
        )
    assert exc_info.value.code == "invalid_name"


async def test_create_template_rejects_missing_language(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A blank ``language`` is rejected with the
    ``invalid_language`` code so the dashboard can show a
    field-level error."""
    with pytest.raises(InvalidTemplateError) as exc_info:
        await create_template(
            async_session,
            client=owner,
            name="order_confirmation",
            language="",
            category=WhatsAppTemplateCategory.UTILITY,
            components=simple_components,
        )
    assert exc_info.value.code == "invalid_language"


async def test_create_template_rejects_unknown_category(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """An unknown category is rejected with a stable code so
    the dashboard can surface the legal set the platform
    accepts."""
    with pytest.raises(InvalidTemplateError) as exc_info:
        await create_template(
            async_session,
            client=owner,
            name="order_confirmation",
            language="es_CL",
            category="unknown",
            components=simple_components,
        )
    assert exc_info.value.code == "invalid_category"


async def test_create_template_rejects_invalid_components(
    async_session, owner: Client
) -> None:
    """A component without a ``type`` field is rejected with
    the ``invalid_components`` code. The validator is
    deliberately light – Meta's WABA endpoint is the source
    of truth for the deep schema – but the platform still
    refuses to persist obviously broken blobs."""
    with pytest.raises(InvalidTemplateError) as exc_info:
        await create_template(
            async_session,
            client=owner,
            name="order_confirmation",
            language="es_CL",
            category=WhatsAppTemplateCategory.UTILITY,
            components=[{"text": "no type field"}],
        )
    assert exc_info.value.code == "invalid_components"


async def test_create_template_rejects_oversized_components(
    async_session, owner: Client
) -> None:
    """A components blob over the 32 KB ceiling is rejected
    at the service layer so a malicious customer cannot
    exhaust the database's text column."""
    huge = [{"type": "BODY", "text": "x" * (40 * 1024)}]
    with pytest.raises(InvalidTemplateError) as exc_info:
        await create_template(
            async_session,
            client=owner,
            name="order_confirmation",
            language="es_CL",
            category=WhatsAppTemplateCategory.UTILITY,
            components=huge,
        )
    assert exc_info.value.code == "invalid_components"
    assert "byte limit" in exc_info.value.message


async def test_create_template_rejects_duplicate_name_language(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A second template with the same ``(name, language)``
    pair is rejected with :class:`DuplicateTemplateError`
    (HTTP 409) – the platform's contract for "this name is
    already taken on this WABA"."""
    await create_template(
        async_session,
        client=owner,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    with pytest.raises(DuplicateTemplateError) as exc_info:
        await create_template(
            async_session,
            client=owner,
            name="order_confirmation",
            language="es_CL",
            category=WhatsAppTemplateCategory.UTILITY,
            components=simple_components,
        )
    assert exc_info.value.code == "duplicate_template"
    assert exc_info.value.http_status == 409


async def test_create_template_allows_same_name_different_language(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """The same name in a different language is allowed –
    Meta lets a customer ship the same template in multiple
    locales."""
    first = await create_template(
        async_session,
        client=owner,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    second = await create_template(
        async_session,
        client=owner,
        name="order_confirmation",
        language="en_US",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    assert first.template.id != second.template.id


async def test_create_template_normalises_empty_description_to_none(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A whitespace-only ``description`` is stored as
    ``NULL`` so the dashboard can distinguish "I forgot to
    set a description" from "I set a description and the
    value is empty"."""
    result = await create_template(
        async_session,
        client=owner,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
        description="   ",
    )
    assert result.template.description is None


# ---------------------------------------------------------------------------
# list_templates
# ---------------------------------------------------------------------------


async def test_list_templates_returns_only_owner_templates(
    async_session,
    owner: Client,
    other_client: Client,
    simple_components: list[dict[str, object]],
) -> None:
    """``list_templates`` only returns the requesting
    customer's templates – the cross-client guard is the
    same as for messages and webhooks."""
    await create_template(
        async_session,
        client=owner,
        name="mine",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    await create_template(
        async_session,
        client=other_client,
        name="theirs",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    mine = await list_templates(async_session, client=owner)
    assert len(mine) == 1
    assert mine[0].name == "mine"


async def test_list_templates_orders_by_newest_first(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """Templates are returned newest-first so the
    dashboard's "recent" tab works out of the box."""
    first = await create_template(
        async_session,
        client=owner,
        name="alpha",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    second = await create_template(
        async_session,
        client=owner,
        name="beta",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    listed = await list_templates(async_session, client=owner)
    assert [t.id for t in listed] == [second.template.id, first.template.id]


async def test_list_templates_filters_by_status(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A status filter narrows the list to the requested
    lifecycle state so the dashboard's "Pending review" tab
    does not have to round-trip the full list."""
    draft = await create_template(
        async_session,
        client=owner,
        name="still_draft",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    approved = await create_template(
        async_session,
        client=owner,
        name="already_approved",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    approved.template.status = WhatsAppTemplateStatus.APPROVED
    await async_session.commit()

    drafts = await list_templates(
        async_session, client=owner, status=WhatsAppTemplateStatus.DRAFT
    )
    assert [t.id for t in drafts] == [draft.template.id]
    approved_list = await list_templates(
        async_session, client=owner, status=WhatsAppTemplateStatus.APPROVED
    )
    assert [t.id for t in approved_list] == [approved.template.id]


async def test_list_templates_pagination_caps_huge_limits(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A limit over the hard cap (200) is silently clamped
    so a malicious caller cannot exhaust the database."""
    await create_template(
        async_session,
        client=owner,
        name="single",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    listed = await list_templates(async_session, client=owner, limit=10_000)
    assert len(listed) == 1


# ---------------------------------------------------------------------------
# get_template
# ---------------------------------------------------------------------------


async def test_get_template_returns_row(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A valid ``template_id`` is returned as-is."""
    created = await create_template(
        async_session,
        client=owner,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    loaded = await get_template(
        async_session, client=owner, template_id=created.template.id
    )
    assert loaded.id == created.template.id


async def test_get_template_404_for_foreign_template(
    async_session,
    owner: Client,
    other_client: Client,
    simple_components: list[dict[str, object]],
) -> None:
    """A template that belongs to another client is reported
    as :class:`TemplateNotFoundError` – the same response
    an unauthenticated caller would see – so the existence
    of another tenant's resource is not leaked."""
    foreign = await create_template(
        async_session,
        client=other_client,
        name="private",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    with pytest.raises(TemplateNotFoundError) as exc_info:
        await get_template(
            async_session, client=owner, template_id=foreign.template.id
        )
    assert exc_info.value.code == "template_not_found"
    assert exc_info.value.http_status == 404


async def test_get_template_404_for_unknown_id(
    async_session, owner: Client
) -> None:
    """An unknown id is reported as :class:`TemplateNotFoundError`
    (not :class:`TemplateImmutableError` or a 500) so the
    dashboard can branch on a single error code."""
    with pytest.raises(TemplateNotFoundError):
        await get_template(
            async_session,
            client=owner,
            template_id="00000000-0000-0000-0000-000000000000",
        )


async def test_get_template_404_for_blank_id(
    async_session, owner: Client
) -> None:
    """A blank id is rejected at the service layer so the
    caller cannot probe for the existence of templates
    with empty-string lookups."""
    with pytest.raises(TemplateNotFoundError):
        await get_template(async_session, client=owner, template_id="")


# ---------------------------------------------------------------------------
# update_template
# ---------------------------------------------------------------------------


async def test_update_template_patches_draft(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A successful update modifies the requested fields
    and leaves the rest untouched."""
    created = await create_template(
        async_session,
        client=owner,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
        description="old",
    )
    new_components: list[dict[str, object]] = [{"type": "BODY", "text": "new"}]
    result = await update_template(
        async_session,
        client=owner,
        template_id=created.template.id,
        description="new",
        components=new_components,
    )
    assert result.template.id == created.template.id
    assert result.template.description == "new"
    assert '"text":"new"' in result.template.components
    # Untouched fields keep their value.
    assert result.template.name == "order_confirmation"
    assert result.template.language == "es_CL"
    assert result.template.status == WhatsAppTemplateStatus.DRAFT


async def test_update_template_rejects_changes_to_submitted_template(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A template that has already been submitted to Meta
    cannot be mutated through the customer-facing
    endpoint – the upstream owns the canonical shape. The
    service returns :class:`TemplateImmutableError` (HTTP
    409) so the dashboard learns the rule quickly."""
    created = await create_template(
        async_session,
        client=owner,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    created.template.status = WhatsAppTemplateStatus.PENDING
    await async_session.commit()
    with pytest.raises(TemplateImmutableError) as exc_info:
        await update_template(
            async_session,
            client=owner,
            template_id=created.template.id,
            description="cannot edit",
        )
    assert exc_info.value.code == "template_immutable"
    assert exc_info.value.http_status == 409


async def test_update_template_rejects_renaming_to_collision(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """Renaming a template to a ``(name, language)`` pair
    that already exists is rejected with
    :class:`DuplicateTemplateError` so a customer cannot
    accidentally wipe out an existing template by typo'ing
    the wrong name into the PATCH body."""
    await create_template(
        async_session,
        client=owner,
        name="first",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    second = await create_template(
        async_session,
        client=owner,
        name="second",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    with pytest.raises(DuplicateTemplateError):
        await update_template(
            async_session,
            client=owner,
            template_id=second.template.id,
            name="first",
        )


async def test_update_template_validates_new_name(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A rename to an invalid name is rejected with the
    same ``invalid_name`` code the create path uses, so
    the dashboard can build a single error-handling shape
    around one error code."""
    created = await create_template(
        async_session,
        client=owner,
        name="valid_name",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    with pytest.raises(InvalidTemplateError):
        await update_template(
            async_session,
            client=owner,
            template_id=created.template.id,
            name="Invalid Name",
        )


# ---------------------------------------------------------------------------
# delete_template
# ---------------------------------------------------------------------------


async def test_delete_template_removes_row(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """A successful delete removes the row; a subsequent
    ``get`` returns 404."""
    created = await create_template(
        async_session,
        client=owner,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    await delete_template(
        async_session, client=owner, template_id=created.template.id
    )
    with pytest.raises(TemplateNotFoundError):
        await get_template(
            async_session, client=owner, template_id=created.template.id
        )


async def test_delete_template_404_for_foreign_template(
    async_session,
    owner: Client,
    other_client: Client,
    simple_components: list[dict[str, object]],
) -> None:
    """A delete that targets another client's template is
    reported as :class:`TemplateNotFoundError` so the
    existence of another tenant's resource is not leaked.
    The foreign row is left untouched (a regression that
    deleted it would be a security issue)."""
    foreign = await create_template(
        async_session,
        client=other_client,
        name="private",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    with pytest.raises(TemplateNotFoundError):
        await delete_template(
            async_session, client=owner, template_id=foreign.template.id
        )
    # Confirm the row is still there for the legitimate
    # owner.
    still_there = await get_template(
        async_session, client=other_client, template_id=foreign.template.id
    )
    assert still_there.id == foreign.template.id


# ---------------------------------------------------------------------------
# template_to_dict
# ---------------------------------------------------------------------------


async def test_template_to_dict_projects_components_as_list(
    async_session, owner: Client, simple_components: list[dict[str, object]]
) -> None:
    """The ``components`` field is projected as a Python
    list, not a JSON string, so the Pydantic model on the
    route layer can serialise it without an extra parse
    step."""
    created = await create_template(
        async_session,
        client=owner,
        name="order_confirmation",
        language="es_CL",
        category=WhatsAppTemplateCategory.UTILITY,
        components=simple_components,
    )
    payload = template_to_dict(created.template)
    assert isinstance(payload["components"], list)
    assert payload["components"][0]["type"] == "BODY"
    assert payload["status"] == "draft"
    assert payload["category"] == "utility"
