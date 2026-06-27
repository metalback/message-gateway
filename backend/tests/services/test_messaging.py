"""Unit tests for the messaging service layer (issue #4).

The tests cover:

- :func:`app.services.messaging.compute_message_cost` – the
  cost / fee computation is the only pure helper the service
  exposes; a unit test gives the future billing service a
  stable contract to build on.
- :func:`app.services.messaging.send_message` – persistence
  + dispatch + status update, with the provider mocked
  through the ``FakeProvider`` defined below.
- :func:`app.services.messaging.send_batch` – the per-item
  outcome list and the hard cap on the number of items.
- :func:`app.services.messaging.get_message_status` – the
  status refresh path, including the cross-tenant access
  guard.
- :func:`app.services.messaging.list_messages` – the
  paginated / filterable history used by the dashboard's
  "Historial y consumo" view, including the cross-tenant
  guard, the ``channel`` / ``status`` / date range filters
  and the ``has_more`` / ``total`` accounting.

The HTTP layer is exercised through the
:class:`FakeProvider` so the suite never opens a real TCP
connection. The provider implements the
:class:`app.adapters.base.BaseProvider` contract and lets
the test inject the response it wants to simulate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import (
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.config import Settings
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.message import Channel, Message, MessageStatus
from app.services.messaging import (
    BatchTooLargeError,
    InvalidListFilterError,
    InvalidMessageError,
    MessageNotFoundError,
    compute_message_cost,
    daily_message_counts,
    get_message_status,
    iter_messages_for_export,
    list_messages,
    message_status_summary,
    render_messages_csv,
    send_batch,
    send_message,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeProvider(BaseProvider):
    """A controllable :class:`BaseProvider` for the service tests.

    The constructor accepts a response template
    (``provider_msg_id`` + ``status``) and an optional error
    to raise. The double records every call so the test can
    assert the service layer delegated the right arguments.
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        provider_msg_id: str = "fake-1",
        status: str = "sent",
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self._provider_msg_id = provider_msg_id
        self._status = status
        self._error = error
        self.send_calls: list[dict[str, Any]] = []
        self.status_calls: list[str] = []

    async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
        self.send_calls.append({"to": to, "body": body, **kwargs})
        if self._error is not None:
            raise self._error
        return SendResult(provider_msg_id=self._provider_msg_id, raw={})

    async def get_status(self, provider_msg_id: str) -> str:
        self.status_calls.append(provider_msg_id)
        return self._status


@pytest.fixture
def messaging_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings with the provider config the registry needs.

    The default ``Settings()`` already declares the keys; the
    fixture is here so a future change to the defaults does
    not silently mask a regression.
    """
    return Settings(
        meta_whatsapp_access_token="t",
        meta_whatsapp_phone_number_id="p",
        sms_aggregator_api_url="https://sms.test",
        sms_aggregator_api_key="k",
        sms_aggregator_sender_id="MSGGTWY",
    )


@pytest.fixture
def fake_providers(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeProvider]:
    """Patch the registry so :func:`get_provider` returns
    :class:`FakeProvider` instances.

    The mapping is keyed by the registry builder (so a
    subsequent call from any module goes through the same
    patch). Tests can then swap a single provider for an
    error-returning one.
    """
    import app.adapters.registry as registry

    whatsapp = FakeProvider(name="meta_whatsapp")
    sms = FakeProvider(name="sms_aggregator")
    monkeypatch.setitem(
        registry._BUILDERS,
        Channel.WHATSAPP,
        lambda settings: whatsapp,
    )
    monkeypatch.setitem(
        registry._BUILDERS,
        Channel.SMS,
        lambda settings: sms,
    )
    return {"meta_whatsapp": whatsapp, "sms_aggregator": sms}


async def _make_client(
    async_session,
    *,
    plan: ClientPlan = ClientPlan.STARTER,
) -> Client:
    client = Client(
        name="Acme",
        email="ops@acme.cl",
        rut="12345678-5",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=plan,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(client)
    await async_session.flush()
    return client


# ---------------------------------------------------------------------------
# compute_message_cost
# ---------------------------------------------------------------------------


def test_compute_message_cost_starter_sms() -> None:
    """The MVP cost numbers are documented in the service
    module; the test pins the contract so a refactor does not
    silently change what the customer is billed."""
    cost, fee = compute_message_cost(channel=Channel.SMS, plan=ClientPlan.STARTER)
    assert (cost, fee) == (25, 5)


def test_compute_message_cost_growth_whatsapp() -> None:
    cost, fee = compute_message_cost(channel=Channel.WHATSAPP, plan=ClientPlan.GROWTH)
    assert (cost, fee) == (80, 3)


def test_compute_message_cost_enterprise_has_lowest_markup() -> None:
    """Enterprise customers pay the lowest markup; the test
    guards against a refactor that flips the pricing tiers."""
    _, starter_fee = compute_message_cost(channel=Channel.SMS, plan=ClientPlan.STARTER)
    _, growth_fee = compute_message_cost(channel=Channel.SMS, plan=ClientPlan.GROWTH)
    _, enterprise_fee = compute_message_cost(channel=Channel.SMS, plan=ClientPlan.ENTERPRISE)
    assert enterprise_fee < growth_fee < starter_fee


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


async def test_send_message_persists_and_returns_outcome(
    async_session, fake_providers, messaging_settings
) -> None:
    """A successful send persists the row, calls the provider
    with the canonical phone number and returns a
    :class:`SendOutcome` with the provider message id."""
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56 9 1234 5678",
        body="hola",
        settings=messaging_settings,
    )

    assert outcome.provider_msg_id == "fake-1"
    assert outcome.message.status == MessageStatus.SENT
    assert outcome.message.to_number == "+56912345678"
    assert outcome.message.body == "hola"
    assert outcome.message.provider == "meta_whatsapp"
    assert outcome.message.cost_clp == 80
    assert outcome.message.fee_clp == 5

    # The provider received the canonical form.
    assert fake_providers["meta_whatsapp"].send_calls == [
        {"to": "+56912345678", "body": "hola"}
    ]


async def test_send_message_records_latency_ms(
    async_session, fake_providers, messaging_settings
) -> None:
    """A successful send records the wall-clock duration of the
    provider call in the ``latency_ms`` column. The field is
    ``None`` for a failed dispatch so the per-provider average
    reflects successful round-trips, not the time it took a
    request to fail."""
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=messaging_settings,
    )
    # The fake provider returns immediately, so the
    # measured latency is a small positive float (a real
    # call would be in the tens to hundreds of ms range).
    assert outcome.message.latency_ms is not None
    assert outcome.message.latency_ms >= 0.0
    assert outcome.message.latency_ms < 5_000.0  # sanity bound


async def test_send_message_leaves_latency_null_on_failure(
    async_session, fake_providers, messaging_settings
) -> None:
    """A failed dispatch does not populate ``latency_ms``.

    The admin breakdown's ``AVG`` skips ``NULL`` rows, so a
    failed call that took 10 seconds to fail does not skew the
    per-provider average. The operator gets a quality-of-
    service metric (how long does a successful round-trip
    take?) rather than a mixed signal that conflates success
    and failure."""
    fake_providers["meta_whatsapp"]._error = ProviderValidationError(
        "bad number", provider="meta_whatsapp"
    )
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=messaging_settings,
    )
    assert outcome.message.status == MessageStatus.FAILED
    assert outcome.message.latency_ms is None


async def test_send_message_marks_failed_on_provider_error(
    async_session, fake_providers, messaging_settings
) -> None:
    """A :class:`ProviderError` from the upstream is recorded
    on the row as ``failed`` (so an operator can inspect the
    failure) but the row itself is still persisted – the
    worker / ops team still need to know *what* was
    attempted."""
    fake_providers["meta_whatsapp"]._error = ProviderValidationError(
        "bad number", provider="meta_whatsapp"
    )
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=messaging_settings,
    )

    assert outcome.provider_msg_id is None
    assert outcome.message.status == MessageStatus.FAILED
    assert outcome.message.error_code == "provider_validation"
    assert outcome.message.error_message == "bad number"


async def test_send_message_rejects_invalid_destination(
    async_session, fake_providers, messaging_settings
) -> None:
    """A non-Chilean number is rejected at the service layer
    with a stable code, so the route layer can surface a
    422 without inspecting the provider response."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidMessageError) as exc:
        await send_message(
            async_session,
            client=client,
            channel=Channel.SMS,
            to="not-a-number",
            body="hola",
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_destination"
    assert not fake_providers["sms_aggregator"].send_calls


async def test_send_message_rejects_empty_body(
    async_session, fake_providers, messaging_settings
) -> None:
    """An empty / whitespace-only body is rejected at the
    service layer so the upstream is not asked to send a
    no-op message."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidMessageError) as exc:
        await send_message(
            async_session,
            client=client,
            channel=Channel.SMS,
            to="+56912345678",
            body="   ",
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_body"


async def test_send_message_rejects_oversized_body(
    async_session, fake_providers, messaging_settings
) -> None:
    """A body longer than the channel's per-message cap is
    rejected at the service layer with a stable code."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidMessageError) as exc:
        await send_message(
            async_session,
            client=client,
            channel=Channel.SMS,
            to="+56912345678",
            body="a" * 2000,
            settings=messaging_settings,
        )
    assert exc.value.code == "body_too_long"


async def test_send_message_routes_to_sms_aggregator_for_sms(
    async_session, fake_providers, messaging_settings
) -> None:
    """The routing decision is "given a channel, which
    provider owns it?"; this test pins the mapping so a
    future swap of providers does not break the test
    silently."""
    client = await _make_client(async_session)
    await send_message(
        async_session,
        client=client,
        channel=Channel.SMS,
        to="+56912345678",
        body="hola",
        settings=messaging_settings,
    )
    assert fake_providers["sms_aggregator"].send_calls
    assert not fake_providers["meta_whatsapp"].send_calls


# ---------------------------------------------------------------------------
# send_batch
# ---------------------------------------------------------------------------


async def test_send_batch_returns_one_outcome_per_item(
    async_session, fake_providers, messaging_settings
) -> None:
    """The batch endpoint advertises a per-item outcome
    list; the test pins the ordering and the per-item
    shape."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "whatsapp", "to": "+56922222222", "body": "dos"},
        ],
        settings=messaging_settings,
    )
    assert len(outcome.results) == 2
    assert all(item.message.status == MessageStatus.SENT for item in outcome.results)


async def test_send_batch_rejects_empty_input(
    async_session, fake_providers, messaging_settings
) -> None:
    """An empty batch is a 422 at the route layer; the
    service layer surfaces a stable code so the mapping is
    obvious."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidMessageError):
        await send_batch(
            async_session,
            client=client,
            items=[],
            settings=messaging_settings,
        )


async def test_send_batch_rejects_oversized_input(
    async_session, fake_providers, messaging_settings
) -> None:
    """The hard cap on a single batch is enforced at the
    service layer so a malicious client cannot enqueue
    thousands of rows by accident."""
    client = await _make_client(async_session)
    items = [
        {"channel": "sms", "to": "+56911111111", "body": f"msg-{i}"}
        for i in range(501)
    ]
    with pytest.raises(BatchTooLargeError):
        await send_batch(
            async_session,
            client=client,
            items=items,
            settings=messaging_settings,
        )


async def test_send_batch_rejects_non_list_input(
    async_session, fake_providers, messaging_settings
) -> None:
    """``items`` must be a list; a non-list raises
    :class:`InvalidMessageError` so the route layer can
    surface a 422 without crashing on a malformed body."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidMessageError):
        await send_batch(
            async_session,
            client=client,
            items="not-a-list",  # type: ignore[arg-type]
            settings=messaging_settings,
        )


# ---------------------------------------------------------------------------
# get_message_status
# ---------------------------------------------------------------------------


async def test_get_message_status_returns_terminal_row_unchanged(
    async_session, fake_providers, messaging_settings
) -> None:
    """A row in a terminal state (delivered / failed) is
    returned as-is; the upstream is not consulted on every
    read."""
    client = await _make_client(async_session)
    message = Message(
        client_id=client.id,
        provider="meta_whatsapp",
        channel=Channel.WHATSAPP,
        to_number="+56912345678",
        body="hola",
        status=MessageStatus.DELIVERED,
        provider_msg_id="wamid.1234",
    )
    async_session.add(message)
    await async_session.commit()

    loaded = await get_message_status(
        async_session,
        client=client,
        message_id=message.id,
        settings=messaging_settings,
    )
    assert loaded.status == MessageStatus.DELIVERED
    assert not fake_providers["meta_whatsapp"].status_calls


async def test_get_message_status_returns_pending_row_without_provider_id(
    async_session, fake_providers, messaging_settings
) -> None:
    """A row that has not yet been handed to the upstream
    (``status=pending`` and ``provider_msg_id=None``) is
    returned as-is; the upstream cannot be consulted without
    a message id."""
    client = await _make_client(async_session)
    message = Message(
        client_id=client.id,
        provider="meta_whatsapp",
        channel=Channel.WHATSAPP,
        to_number="+56912345678",
        body="hola",
        status=MessageStatus.PENDING,
        provider_msg_id=None,
    )
    async_session.add(message)
    await async_session.commit()

    loaded = await get_message_status(
        async_session,
        client=client,
        message_id=message.id,
        settings=messaging_settings,
    )
    assert loaded.status == MessageStatus.PENDING
    assert not fake_providers["meta_whatsapp"].status_calls


async def test_get_message_status_refreshes_in_flight_messages(
    async_session, fake_providers, messaging_settings
) -> None:
    """A row that is still ``sent`` (the upstream acknowledged
    the message but the recipient has not yet confirmed
    delivery) is refreshed against the provider."""
    fake_providers["meta_whatsapp"]._status = "delivered"
    client = await _make_client(async_session)
    message = Message(
        client_id=client.id,
        provider="meta_whatsapp",
        channel=Channel.WHATSAPP,
        to_number="+56912345678",
        body="hola",
        status=MessageStatus.SENT,
        provider_msg_id="wamid.1234",
    )
    async_session.add(message)
    await async_session.commit()

    loaded = await get_message_status(
        async_session,
        client=client,
        message_id=message.id,
        settings=messaging_settings,
    )
    assert loaded.status == MessageStatus.DELIVERED
    assert fake_providers["meta_whatsapp"].status_calls == ["wamid.1234"]


async def test_get_message_status_maps_provider_status_strings(
    async_session, fake_providers, messaging_settings
) -> None:
    """The service layer maps provider-specific status
    strings to the platform's vocabulary; this test pins
    the mapping for the four most common values."""
    cases = [
        ("delivered", MessageStatus.DELIVERED),
        ("read", MessageStatus.DELIVERED),
        ("sent", MessageStatus.SENT),
        ("failed", MessageStatus.FAILED),
        ("queued", MessageStatus.QUEUED),
        ("weird-string", MessageStatus.UNKNOWN),
    ]
    for index, (provider_value, expected) in enumerate(cases):
        fake_providers["meta_whatsapp"]._status = provider_value
        client = Client(
            name=f"Acme-{index}",
            email=f"ops-{index}@acme.cl",
            rut=f"1234567{index}-5",
            password_hash="hashed",
            api_key_hash="also-hashed",
            api_key_last4="abcd",
            plan=ClientPlan.STARTER,
            status=ClientStatus.ACTIVE,
        )
        async_session.add(client)
        await async_session.flush()
        message = Message(
            client_id=client.id,
            provider="meta_whatsapp",
            channel=Channel.WHATSAPP,
            to_number="+56912345678",
            body="hola",
            status=MessageStatus.SENT,
            provider_msg_id=f"wamid.{provider_value}",
        )
        async_session.add(message)
        await async_session.commit()

        loaded = await get_message_status(
            async_session,
            client=client,
            message_id=message.id,
            settings=messaging_settings,
        )
        assert loaded.status == expected, f"{provider_value} -> {expected}"


async def test_get_message_status_returns_cached_row_on_provider_error(
    async_session, fake_providers, messaging_settings
) -> None:
    """A provider outage does not propagate to the caller –
    the platform's contract is "best-effort status refresh",
    not "guaranteed up-to-date". A 502 to the dashboard
    because the upstream is down would be a worse outcome
    than a stale row."""
    fake_providers["meta_whatsapp"]._error = ProviderUnavailableError(
        "down", provider="meta_whatsapp"
    )
    client = await _make_client(async_session)
    message = Message(
        client_id=client.id,
        provider="meta_whatsapp",
        channel=Channel.WHATSAPP,
        to_number="+56912345678",
        body="hola",
        status=MessageStatus.SENT,
        provider_msg_id="wamid.1234",
    )
    async_session.add(message)
    await async_session.commit()

    loaded = await get_message_status(
        async_session,
        client=client,
        message_id=message.id,
        settings=messaging_settings,
    )
    assert loaded.status == MessageStatus.SENT


async def test_get_message_status_404_for_unknown_message(
    async_session, fake_providers, messaging_settings
) -> None:
    """An unknown message id is a 404 with a stable code; the
    caller can branch on ``code`` rather than parsing the
    free-text ``detail``."""
    client = await _make_client(async_session)
    with pytest.raises(MessageNotFoundError) as exc:
        await get_message_status(
            async_session,
            client=client,
            message_id="not-a-uuid",
            settings=messaging_settings,
        )
    assert exc.value.code == "message_not_found"


async def test_get_message_status_rejects_empty_id(
    async_session, fake_providers, messaging_settings
) -> None:
    client = await _make_client(async_session)
    with pytest.raises(MessageNotFoundError):
        await get_message_status(
            async_session,
            client=client,
            message_id="",
            settings=messaging_settings,
        )


async def test_get_message_status_does_not_leak_other_clients(
    async_session, fake_providers, messaging_settings
) -> None:
    """A message id that exists but belongs to a different
    client is reported as ``not_found`` (not ``forbidden``)
    so the existence of another tenant's resource is not
    leaked."""
    other = Client(
        name="Other",
        email="other@acme.cl",
        rut="98765432-1",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(other)
    await async_session.flush()
    async_session.add(
        Message(
            client_id=other.id,
            provider="meta_whatsapp",
            channel=Channel.WHATSAPP,
            to_number="+56912345678",
            body="hola",
            status=MessageStatus.SENT,
            provider_msg_id="wamid.1234",
        )
    )
    await async_session.commit()

    stmt = select(Message)
    foreign = (await async_session.execute(stmt)).scalars().first()
    assert foreign is not None

    me = Client(
        name="Me",
        email="me@acme.cl",
        rut="12345678-5",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(me)
    await async_session.commit()

    with pytest.raises(MessageNotFoundError):
        await get_message_status(
            async_session,
            client=me,
            message_id=foreign.id,
            settings=messaging_settings,
        )


# ---------------------------------------------------------------------------
# list_messages
# ---------------------------------------------------------------------------


async def _seed_message(
    async_session,
    *,
    client_id: str,
    channel: Channel,
    status: MessageStatus,
    created_at: datetime,
    body: str = "hola",
) -> Message:
    """Insert a single :class:`Message` row for the history tests.

    The helper isolates the fields the ``list_messages`` path
    actually reads (``client_id``, ``channel``, ``status``,
    ``created_at``); the rest fall back to the model's
    defaults. ``provider`` is set explicitly because the
    column is non-nullable and the test does not care about
    the value.
    """
    message = Message(
        client_id=client_id,
        provider="meta_whatsapp" if channel is Channel.WHATSAPP else "sms_aggregator",
        channel=channel,
        to_number="+56912345678",
        body=body,
        status=status,
        cost_clp=0,
        fee_clp=0,
        created_at=created_at,
    )
    async_session.add(message)
    await async_session.commit()
    return message


async def test_list_messages_returns_newest_first(
    async_session, fake_providers, messaging_settings
) -> None:
    """The history is ordered by ``created_at`` descending so
    the dashboard does not have to re-sort on the client. The
    test seeds three rows with deterministic timestamps and
    asserts the response order matches the descending order.
    """
    client = await _make_client(async_session)
    now = datetime.now(tz=UTC)
    newest = await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.SMS,
        status=MessageStatus.SENT,
        created_at=now,
    )
    middle = await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.WHATSAPP,
        status=MessageStatus.DELIVERED,
        created_at=now - timedelta(minutes=5),
    )
    oldest = await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.SMS,
        status=MessageStatus.FAILED,
        created_at=now - timedelta(hours=1),
    )

    page = await list_messages(
        async_session, client=client, settings=messaging_settings
    )
    assert [m.id for m in page.items] == [newest.id, middle.id, oldest.id]
    assert page.total == 3
    assert page.has_more is False
    assert page.limit == 50
    assert page.offset == 0


async def test_list_messages_filters_by_channel(
    async_session, fake_providers, messaging_settings
) -> None:
    """The ``channel`` filter narrows the result set to the
    single channel the dashboard's filter chip selected. The
    test pins the SQL semantics so a refactor cannot silently
    start returning rows from the other channel."""
    client = await _make_client(async_session)
    now = datetime.now(tz=UTC)
    await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.SMS,
        status=MessageStatus.SENT,
        created_at=now,
    )
    keep = await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.WHATSAPP,
        status=MessageStatus.SENT,
        created_at=now - timedelta(minutes=1),
    )

    page = await list_messages(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        settings=messaging_settings,
    )
    assert [m.id for m in page.items] == [keep.id]
    assert page.total == 1


async def test_list_messages_filters_by_status(
    async_session, fake_providers, messaging_settings
) -> None:
    """The ``status`` filter narrows the result set to a single
    :class:`MessageStatus`. The test exercises a non-default
    status (``FAILED``) so the assertion cannot be satisfied
    by accident by a "match anything" implementation."""
    client = await _make_client(async_session)
    now = datetime.now(tz=UTC)
    await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.SMS,
        status=MessageStatus.SENT,
        created_at=now,
    )
    failed = await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.SMS,
        status=MessageStatus.FAILED,
        created_at=now - timedelta(minutes=1),
    )

    page = await list_messages(
        async_session,
        client=client,
        status=MessageStatus.FAILED,
        settings=messaging_settings,
    )
    assert [m.id for m in page.items] == [failed.id]
    assert page.total == 1


async def test_list_messages_filters_by_date_range(
    async_session, fake_providers, messaging_settings
) -> None:
    """``since`` and ``until`` are inclusive bounds on
    ``created_at``. The test seeds one message inside the
    window, one before, one after, and asserts only the
    in-window row is returned."""
    client = await _make_client(async_session)
    now = datetime.now(tz=UTC)
    inside = await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.SMS,
        status=MessageStatus.SENT,
        created_at=now,
    )
    await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.SMS,
        status=MessageStatus.SENT,
        created_at=now - timedelta(days=3),
    )
    await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.SMS,
        status=MessageStatus.SENT,
        created_at=now + timedelta(days=3),
    )

    page = await list_messages(
        async_session,
        client=client,
        since=now - timedelta(hours=1),
        until=now + timedelta(hours=1),
        settings=messaging_settings,
    )
    assert [m.id for m in page.items] == [inside.id]
    assert page.total == 1


async def test_list_messages_pagination_has_more_and_total(
    async_session, fake_providers, messaging_settings
) -> None:
    """``has_more`` is ``True`` when there is at least one
    additional row after the current page, ``False``
    otherwise. The test seeds 7 rows and pages through them
    with a ``limit`` of 3 to exercise the boundary on every
    page.
    """
    client = await _make_client(async_session)
    base = datetime.now(tz=UTC)
    for i in range(7):
        await _seed_message(
            async_session,
            client_id=client.id,
            channel=Channel.SMS,
            status=MessageStatus.SENT,
            created_at=base - timedelta(minutes=i),
        )

    first = await list_messages(
        async_session, client=client, limit=3, offset=0, settings=messaging_settings
    )
    assert len(first.items) == 3
    assert first.total == 7
    assert first.has_more is True

    second = await list_messages(
        async_session, client=client, limit=3, offset=3, settings=messaging_settings
    )
    assert len(second.items) == 3
    assert second.has_more is True

    last = await list_messages(
        async_session, client=client, limit=3, offset=6, settings=messaging_settings
    )
    assert len(last.items) == 1
    assert last.has_more is False
    assert last.total == 7


async def test_list_messages_does_not_leak_other_clients(
    async_session, fake_providers, messaging_settings
) -> None:
    """A second client's history must not bleed into the first
    client's page. The test seeds 3 rows for ``other`` and 2
    rows for ``me`` and asserts the call from ``me`` only
    returns ``me``'s rows (and the ``total`` count reflects
    the same).
    """
    other = Client(
        name="Other",
        email="other@acme.cl",
        rut="98765432-1",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(other)
    await async_session.commit()
    me = await _make_client(async_session)

    now = datetime.now(tz=UTC)
    for i in range(3):
        await _seed_message(
            async_session,
            client_id=other.id,
            channel=Channel.SMS,
            status=MessageStatus.SENT,
            created_at=now - timedelta(minutes=i),
        )
    for i in range(2):
        await _seed_message(
            async_session,
            client_id=me.id,
            channel=Channel.SMS,
            status=MessageStatus.SENT,
            created_at=now - timedelta(minutes=10 + i),
        )

    page = await list_messages(
        async_session, client=me, settings=messaging_settings
    )
    assert page.total == 2
    assert {m.client_id for m in page.items} == {me.id}


async def test_list_messages_rejects_unknown_channel_filter(
    async_session, fake_providers, messaging_settings
) -> None:
    """A ``channel`` value the platform does not know about
    surfaces as :class:`InvalidListFilterError` so the route
    layer can map it onto a 422 instead of silently returning
    an empty list."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidListFilterError) as exc:
        await list_messages(
            async_session,
            client=client,
            channel="carrier-pigeon",
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_channel"


async def test_list_messages_rejects_unknown_status_filter(
    async_session, fake_providers, messaging_settings
) -> None:
    """A ``status`` value the platform does not know about
    surfaces as :class:`InvalidListFilterError`."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidListFilterError) as exc:
        await list_messages(
            async_session,
            client=client,
            status="teleported",
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_status"


async def test_list_messages_rejects_inverted_date_range(
    async_session, fake_providers, messaging_settings
) -> None:
    """``since`` must be earlier than (or equal to) ``until``;
    a range where the lower bound is after the upper bound is
    a 422. The test pins the contract so a future "swap the
    bounds silently" refactor does not slip through."""
    client = await _make_client(async_session)
    now = datetime.now(tz=UTC)
    with pytest.raises(InvalidListFilterError) as exc:
        await list_messages(
            async_session,
            client=client,
            since=now,
            until=now - timedelta(days=1),
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_date_range"


async def test_list_messages_rejects_non_positive_limit(
    async_session, fake_providers, messaging_settings
) -> None:
    """A ``limit`` of zero (or a negative value) is rejected at
    the service layer so a buggy dashboard cannot accidentally
    request an empty page."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidListFilterError) as exc:
        await list_messages(
            async_session,
            client=client,
            limit=0,
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_limit"


async def test_list_messages_caps_limit_to_hard_maximum(
    async_session, fake_providers, messaging_settings
) -> None:
    """The service silently caps ``limit`` at the hard
    maximum so a curious operator cannot ask the API for an
    unbounded page. The dashboard does not currently send
    values above the cap, but the cap protects against a
    future regression in the call site."""
    from app.services.messaging import _LIST_HARD_LIMIT

    client = await _make_client(async_session)
    page = await list_messages(
        async_session,
        client=client,
        limit=_LIST_HARD_LIMIT * 5,
        settings=messaging_settings,
    )
    assert page.limit == _LIST_HARD_LIMIT


async def test_list_messages_empty_history_returns_empty_page(
    async_session, fake_providers, messaging_settings
) -> None:
    """A customer who has never sent a message gets a
    well-formed empty page (``items=[]``, ``total=0``,
    ``has_more=False``) so the dashboard can render an
    "empty state" without special-casing the API."""
    client = await _make_client(async_session)
    page = await list_messages(
        async_session, client=client, settings=messaging_settings
    )
    assert page.items == []
    assert page.total == 0
    assert page.has_more is False


# ---------------------------------------------------------------------------
# iter_messages_for_export + render_messages_csv
# ---------------------------------------------------------------------------
#
# The "Historial y consumo" dashboard lets the customer download
# their history as a CSV file (PRD user story #18). The
# service-layer entry point is :func:`iter_messages_for_export` and
# the wire format is owned by :func:`render_messages_csv`. The
# tests below pin both contracts so a refactor in either half does
# not silently change the file a customer downloads.


async def test_iter_messages_for_export_returns_full_history(
    async_session, fake_providers, messaging_settings
) -> None:
    """A small history is returned in full, newest first, with
    the right ``total`` count and a ``truncated=False`` flag
    (the result is well under the hard cap). The test seeds
    three rows with deterministic timestamps and asserts the
    ordering and the count."""
    client = await _make_client(async_session)
    now = datetime.now(tz=UTC)
    newest = await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.SMS,
        status=MessageStatus.SENT,
        created_at=now,
    )
    middle = await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.WHATSAPP,
        status=MessageStatus.DELIVERED,
        created_at=now - timedelta(minutes=5),
    )
    oldest = await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.SMS,
        status=MessageStatus.FAILED,
        created_at=now - timedelta(minutes=10),
    )

    export = await iter_messages_for_export(
        async_session, client=client, settings=messaging_settings
    )
    assert [m.id for m in export.items] == [newest.id, middle.id, oldest.id]
    assert export.total == 3
    assert export.truncated is False


async def test_iter_messages_for_export_respects_filters(
    async_session, fake_providers, messaging_settings
) -> None:
    """The export honours the same ``channel`` / ``status`` /
    date range filters the list endpoint does. The test seeds
    one WhatsApp / one SMS row and asks for WhatsApp only;
    the result is the single WhatsApp row."""
    client = await _make_client(async_session)
    now = datetime.now(tz=UTC)
    await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.SMS,
        status=MessageStatus.SENT,
        created_at=now,
    )
    keep = await _seed_message(
        async_session,
        client_id=client.id,
        channel=Channel.WHATSAPP,
        status=MessageStatus.SENT,
        created_at=now - timedelta(minutes=1),
    )

    export = await iter_messages_for_export(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        settings=messaging_settings,
    )
    assert [m.id for m in export.items] == [keep.id]
    assert export.total == 1


async def test_iter_messages_for_export_does_not_leak_other_clients(
    async_session, fake_providers, messaging_settings
) -> None:
    """The export must be tenant-scoped, exactly like
    :func:`list_messages`. The test seeds two rows for a
    second client and one row for ``me`` and asserts only
    ``me``'s row is in the result."""
    other = Client(
        name="Other",
        email="other@acme.cl",
        rut="98765432-1",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(other)
    await async_session.commit()
    me = await _make_client(async_session)

    now = datetime.now(tz=UTC)
    for i in range(2):
        await _seed_message(
            async_session,
            client_id=other.id,
            channel=Channel.SMS,
            status=MessageStatus.SENT,
            created_at=now - timedelta(minutes=i),
        )
    mine = await _seed_message(
        async_session,
        client_id=me.id,
        channel=Channel.SMS,
        status=MessageStatus.SENT,
        created_at=now - timedelta(minutes=10),
    )

    export = await iter_messages_for_export(
        async_session, client=me, settings=messaging_settings
    )
    assert [m.id for m in export.items] == [mine.id]
    assert export.total == 1


async def test_iter_messages_for_export_marks_truncated_when_capped(
    async_session, fake_providers, messaging_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the result would exceed :data:`_EXPORT_HARD_LIMIT`
    rows the service returns the cap-sized slice and flips
    ``truncated=True`` so the route layer can surface a
    header. The test lowers the cap to a small number so the
    assertion does not have to seed thousands of rows."""
    from app.services import messaging as messaging_module

    monkeypatch.setattr(messaging_module, "_EXPORT_HARD_LIMIT", 2)
    client = await _make_client(async_session)
    base = datetime.now(tz=UTC)
    for i in range(3):
        await _seed_message(
            async_session,
            client_id=client.id,
            channel=Channel.SMS,
            status=MessageStatus.SENT,
            created_at=base - timedelta(minutes=i),
        )

    export = await iter_messages_for_export(
        async_session, client=client, settings=messaging_settings
    )
    assert len(export.items) == 2
    assert export.total == 3
    assert export.truncated is True


def test_render_messages_csv_includes_header_and_rows() -> None:
    """The wire format is the standard RFC-4180 CSV: a header
    row, one row per message, and a ``\\r\\n`` line terminator.
    The test builds a small in-memory :class:`Message` and
    asserts the exact byte-for-byte output so a refactor in
    the column shape (or a stray ``\\n``) is caught here
    rather than at a customer's spreadsheet."""
    from app.models.message import Message as MessageModel

    message = MessageModel(
        id="m-1",
        client_id="c-1",
        provider="meta_whatsapp",
        channel=Channel.WHATSAPP,
        to_number="+56912345678",
        body='hola "amigo"',
        status=MessageStatus.DELIVERED,
        provider_msg_id="p-1",
        cost_clp=80,
        fee_clp=5,
        created_at=datetime(2026, 6, 15, 10, 0, tzinfo=UTC),
    )
    rendered = render_messages_csv([message])
    expected = (
        "id,created_at,channel,status,to_number,body,provider,"
        "provider_msg_id,error_code,error_message,cost_clp,fee_clp\r\n"
        'm-1,2026-06-15T10:00:00+00:00,whatsapp,delivered,+56912345678,'
        '"hola ""amigo""",meta_whatsapp,p-1,,,80,5\r\n'
    )
    assert rendered == expected


def test_render_messages_csv_handles_empty_iterable() -> None:
    """An empty input still produces a well-formed CSV with
    just the header row, so the route layer never has to
    branch on ``items=[]``."""
    rendered = render_messages_csv([])
    assert rendered == (
        "id,created_at,channel,status,to_number,body,provider,"
        "provider_msg_id,error_code,error_message,cost_clp,fee_clp\r\n"
    )


def test_render_messages_csv_emits_empty_cells_for_optional_fields() -> None:
    """A row whose ``provider_msg_id`` / ``error_code`` /
    ``error_message`` are ``None`` must serialise as empty
    cells – not the literal ``"None"`` ``str(None)`` would
    produce. A spreadsheet that opens a CSV with ``None``
    strings in the cells looks like garbage to the customer.
    """
    from app.models.message import Message as MessageModel

    message = MessageModel(
        id="m-2",
        client_id="c-1",
        provider="sms_aggregator",
        channel=Channel.SMS,
        to_number="+56987654321",
        body="hola",
        status=MessageStatus.FAILED,
        provider_msg_id=None,
        error_code="rate_limited",
        error_message="too many",
        cost_clp=25,
        fee_clp=5,
        created_at=datetime(2026, 6, 16, 12, 30, tzinfo=UTC),
    )
    rendered = render_messages_csv([message])
    # The provider_msg_id column is empty; the error columns
    # carry the values.
    row = rendered.splitlines()[1]
    assert row.endswith(",rate_limited,too many,25,5")
    # No literal ``None`` in the row – a quick safety net
    # against an accidental ``str(None)`` regression.
    assert "None" not in row


# ---------------------------------------------------------------------------
# daily_message_counts
# ---------------------------------------------------------------------------
#
# The "Historial y consumo" dashboard's bar chart is backed by
# :func:`daily_message_counts`. The tests pin the aggregation
# contract: per-day, per-channel counts, ordered by day, the
# cross-tenant guard, the date range filters and the error
# paths. The function is pure database, so the seed
# pattern matches the rest of the list-messaging tests.


async def _seed_message_at(
    async_session,
    *,
    client_id: str,
    channel: Channel,
    status: MessageStatus,
    when: datetime,
    body: str = "hola",
) -> Message:
    """Same helper as :func:`_seed_message` but lets the test
    pin a specific ``created_at`` *and* propagate the value
    to ``sent_at`` so the date-based aggregations can use
    either field interchangeably.
    """
    message = Message(
        client_id=client_id,
        provider="meta_whatsapp" if channel is Channel.WHATSAPP else "sms_aggregator",
        channel=channel,
        to_number="+56912345678",
        body=body,
        status=status,
        cost_clp=0,
        fee_clp=0,
        created_at=when,
        sent_at=when,
    )
    async_session.add(message)
    await async_session.commit()
    return message


async def test_daily_message_counts_groups_by_day_and_channel(
    async_session, fake_providers, messaging_settings
) -> None:
    """Two SMS messages on Monday, three WhatsApp messages on
    Tuesday and one SMS on Tuesday produce three :class:`DailyMessageCount`
    rows: ``(mon, sms, 2)``, ``(tue, sms, 1)`` and
    ``(tue, whatsapp, 3)``. The function never collapses
    channels into a single row per day, so the dashboard
    can colour the stacked bars without a second query."""
    client = await _make_client(async_session)
    monday = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    tuesday = monday + timedelta(days=1)
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.SMS,
        status=MessageStatus.SENT, when=monday,
    )
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.SMS,
        status=MessageStatus.SENT, when=monday + timedelta(hours=1),
    )
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.SMS,
        status=MessageStatus.SENT, when=tuesday,
    )
    for i in range(3):
        await _seed_message_at(
            async_session, client_id=client.id, channel=Channel.WHATSAPP,
            status=MessageStatus.SENT, when=tuesday + timedelta(minutes=i),
        )

    rows = await daily_message_counts(
        async_session,
        client=client,
        since=monday - timedelta(days=1),
        until=tuesday + timedelta(days=1),
        settings=messaging_settings,
    )
    # Three buckets, ordered by day then channel.
    assert [(r.day, r.channel, r.count) for r in rows.items] == [
        (monday.date(), "sms", 2),
        (tuesday.date(), "sms", 1),
        (tuesday.date(), "whatsapp", 3),
    ]
    # The resolved window is echoed back so the route layer
    # can put it in the response.
    assert rows.since == monday - timedelta(days=1)
    assert rows.until == tuesday + timedelta(days=1)


async def test_daily_message_counts_does_not_leak_other_clients(
    async_session, fake_providers, messaging_settings
) -> None:
    """The aggregation is tenant-scoped, just like
    :func:`list_messages`. The test seeds a row for a second
    customer and asserts the second customer's day does not
    appear in the response."""
    other = Client(
        name="Other",
        email="other@acme.cl",
        rut="98765432-1",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(other)
    await async_session.commit()
    me = await _make_client(async_session)

    when = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    await _seed_message_at(
        async_session, client_id=other.id, channel=Channel.SMS,
        status=MessageStatus.SENT, when=when,
    )
    await _seed_message_at(
        async_session, client_id=me.id, channel=Channel.SMS,
        status=MessageStatus.SENT, when=when,
    )

    rows = await daily_message_counts(
        async_session,
        client=me,
        since=when - timedelta(days=1),
        until=when + timedelta(days=1),
        settings=messaging_settings,
    )
    assert len(rows.items) == 1
    assert rows.items[0].count == 1
    assert rows.items[0].day == when.date()
    assert rows.items[0].channel == "sms"


async def test_daily_message_counts_filters_by_channel(
    async_session, fake_providers, messaging_settings
) -> None:
    """The ``channel`` filter narrows the result to a single
    channel. The test seeds both channels and asks for
    WhatsApp only; the SMS row never makes it into the
    response."""
    client = await _make_client(async_session)
    when = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.SMS,
        status=MessageStatus.SENT, when=when,
    )
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.WHATSAPP,
        status=MessageStatus.SENT, when=when,
    )

    rows = await daily_message_counts(
        async_session,
        client=client,
        since=when - timedelta(days=1),
        until=when + timedelta(days=1),
        channel=Channel.WHATSAPP,
        settings=messaging_settings,
    )
    assert len(rows.items) == 1
    assert rows.items[0].day == when.date()
    assert rows.items[0].channel == "whatsapp"
    assert rows.items[0].count == 1


async def test_daily_message_counts_uses_default_window_when_unset(
    async_session, fake_providers, messaging_settings, monkeypatch
) -> None:
    """When the caller omits both ``since`` and ``until`` the
    function falls back to a 31-day window ending "now". The
    test pins ``now`` to a deterministic instant via a
    monkey-patched module function and seeds one row inside
    the window plus one outside, asserting only the in-window
    row is returned."""
    from app.services import messaging as messaging_module

    fixed_now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        messaging_module,
        "_daily_default_range",
        lambda now=None: (fixed_now - timedelta(days=30), fixed_now),
    )
    client = await _make_client(async_session)
    inside = await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.SMS,
        status=MessageStatus.SENT, when=fixed_now - timedelta(days=2),
    )
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.SMS,
        status=MessageStatus.SENT, when=fixed_now - timedelta(days=60),
    )

    rows = await daily_message_counts(
        async_session, client=client, settings=messaging_settings
    )
    assert len(rows.items) == 1
    assert rows.items[0].count == 1
    assert rows.items[0].day == inside.created_at.date()
    # The default window is echoed back so the dashboard can
    # render the chart axis labels.
    assert rows.since == fixed_now - timedelta(days=30)
    assert rows.until == fixed_now


async def test_daily_message_counts_rejects_inverted_range(
    async_session, fake_providers, messaging_settings
) -> None:
    """An inverted ``since`` / ``until`` is rejected with a
    422-friendly :class:`InvalidListFilterError` so the
    dashboard can surface a useful inline error."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidListFilterError) as exc:
        await daily_message_counts(
            async_session,
            client=client,
            since=datetime(2030, 1, 1, tzinfo=UTC),
            until=datetime(2020, 1, 1, tzinfo=UTC),
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_date_range"


async def test_daily_message_counts_rejects_oversized_range(
    async_session, fake_providers, messaging_settings
) -> None:
    """A range wider than the hard cap is rejected before the
    database is hit so a curious operator cannot force a
    full-table aggregation."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidListFilterError) as exc:
        await daily_message_counts(
            async_session,
            client=client,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            until=datetime(2026, 1, 1, tzinfo=UTC),
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_date_range"


async def test_daily_message_counts_empty_history_returns_empty_list(
    async_session, fake_providers, messaging_settings
) -> None:
    """A customer who has never sent a message gets a well-
    formed empty list so the dashboard can render the
    "todavía no has enviado mensajes este mes" empty state
    without a special-case branch."""
    client = await _make_client(async_session)
    rows = await daily_message_counts(
        async_session,
        client=client,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 30, tzinfo=UTC),
        settings=messaging_settings,
    )
    assert rows.items == []
    assert rows.since == datetime(2026, 6, 1, tzinfo=UTC)
    assert rows.until == datetime(2026, 6, 30, tzinfo=UTC)


async def test_daily_message_counts_rejects_unknown_channel(
    async_session, fake_providers, messaging_settings
) -> None:
    """A bogus channel string is a 422 (not a silent empty
    list) so a typo in a future caller surfaces immediately."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidListFilterError) as exc:
        await daily_message_counts(
            async_session,
            client=client,
            channel="carrier-pigeon",
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_channel"


# ---------------------------------------------------------------------------
# message_status_summary
# ---------------------------------------------------------------------------
#
# The "Historial y consumo" dashboard's "desglose por estado"
# card is backed by :func:`message_status_summary`. The tests
# pin the aggregation contract: per-status counts ordered in
# the platform's lifecycle order, zero-filled for statuses with
# no traffic, the cross-tenant guard, the channel / date range
# filters and the headline counter derivation. The function is
# pure database, so the seed pattern matches the rest of the
# list-messaging tests.


async def test_message_status_summary_aggregates_per_status(
    async_session, fake_providers, messaging_settings
) -> None:
    """Three delivered messages, two sent, one failed and one
    pending produce a :class:`MessageStatusSummary` whose
    ``items`` list carries one row per :class:`MessageStatus`
    value (zero-filled for ``queued`` and ``unknown``) in the
    platform's lifecycle order. The headline counters and the
    summed cost / fee amounts are derived from the same
    aggregation."""
    client = await _make_client(async_session)
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    for _ in range(3):
        await _seed_message_at(
            async_session, client_id=client.id, channel=Channel.WHATSAPP,
            status=MessageStatus.DELIVERED, when=now,
            body="delivered",
        )
    for _ in range(2):
        await _seed_message_at(
            async_session, client_id=client.id, channel=Channel.SMS,
            status=MessageStatus.SENT, when=now, body="sent",
        )
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.SMS,
        status=MessageStatus.FAILED, when=now, body="failed",
    )
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.SMS,
        status=MessageStatus.PENDING, when=now, body="pending",
    )

    summary = await message_status_summary(
        async_session,
        client=client,
        since=now - timedelta(days=1),
        until=now + timedelta(days=1),
        settings=messaging_settings,
    )

    # The lifecycle order is platform-wide: delivered, sent,
    # queued, pending, failed, unknown. ``queued`` and
    # ``unknown`` are zero-filled because no row used them.
    assert [(row.status, row.count) for row in summary.items] == [
        (MessageStatus.DELIVERED, 3),
        (MessageStatus.SENT, 2),
        (MessageStatus.QUEUED, 0),
        (MessageStatus.PENDING, 1),
        (MessageStatus.FAILED, 1),
        (MessageStatus.UNKNOWN, 0),
    ]
    assert summary.total == 7
    assert summary.delivered == 3
    assert summary.failed == 1
    assert summary.pending == 1
    # ``delivery_rate`` is 3/7 ≈ 0.4286, clamped to the
    # closed interval ``[0.0, 1.0]``.
    assert summary.delivery_rate == pytest.approx(3 / 7)
    # The window is echoed back so the dashboard can render
    # "resumen del 14 al 16 de junio" without mirroring the
    # service's default-window logic.
    assert summary.since == now - timedelta(days=1)
    assert summary.until == now + timedelta(days=1)


async def test_message_status_summary_does_not_leak_other_clients(
    async_session, fake_providers, messaging_settings
) -> None:
    """The aggregation is tenant-scoped, just like
    :func:`list_messages` and :func:`daily_message_counts`.
    The test seeds rows for two customers and asserts the
    first customer only ever sees their own totals."""
    other = Client(
        name="Other",
        email="other@acme.cl",
        rut="98765432-1",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(other)
    await async_session.commit()
    me = await _make_client(async_session)

    when = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    await _seed_message_at(
        async_session, client_id=other.id, channel=Channel.SMS,
        status=MessageStatus.DELIVERED, when=when,
    )
    await _seed_message_at(
        async_session, client_id=me.id, channel=Channel.SMS,
        status=MessageStatus.DELIVERED, when=when,
    )

    summary = await message_status_summary(
        async_session,
        client=me,
        since=when - timedelta(days=1),
        until=when + timedelta(days=1),
        settings=messaging_settings,
    )
    assert summary.total == 1
    assert summary.delivered == 1
    assert summary.delivery_rate == 1.0


async def test_message_status_summary_filters_by_channel(
    async_session, fake_providers, messaging_settings
) -> None:
    """The ``channel`` filter narrows the result to a single
    channel. The test seeds both channels and asks for
    WhatsApp only; the SMS row never makes it into the
    response."""
    client = await _make_client(async_session)
    when = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.SMS,
        status=MessageStatus.SENT, when=when,
    )
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.WHATSAPP,
        status=MessageStatus.DELIVERED, when=when,
    )

    summary = await message_status_summary(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        since=when - timedelta(days=1),
        until=when + timedelta(days=1),
        settings=messaging_settings,
    )
    assert summary.total == 1
    assert summary.delivered == 1
    # The ``delivered`` row is the WhatsApp one; the SMS row
    # is filtered out.
    by_status = {row.status: row.count for row in summary.items}
    assert by_status[MessageStatus.DELIVERED] == 1
    assert by_status[MessageStatus.SENT] == 0


async def test_message_status_summary_uses_default_window_when_unset(
    async_session, fake_providers, messaging_settings, monkeypatch
) -> None:
    """When the caller omits both ``since`` and ``until`` the
    function falls back to a 31-day window ending "now". The
    test pins ``now`` to a deterministic instant via a
    monkey-patched module function and seeds one row inside
    the window plus one outside, asserting only the in-window
    row is returned."""
    from app.services import messaging as messaging_module

    fixed_now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(
        messaging_module,
        "_summary_default_range",
        lambda now=None: (fixed_now - timedelta(days=30), fixed_now),
    )
    client = await _make_client(async_session)
    inside = await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.SMS,
        status=MessageStatus.SENT, when=fixed_now - timedelta(days=2),
    )
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.SMS,
        status=MessageStatus.SENT, when=fixed_now - timedelta(days=60),
    )

    summary = await message_status_summary(
        async_session, client=client, settings=messaging_settings
    )
    assert summary.total == 1
    assert summary.delivered == 0
    # The default window is echoed back so the dashboard can
    # show "resumen del 16 de mayo al 15 de junio".
    assert summary.since == fixed_now - timedelta(days=30)
    assert summary.until == fixed_now
    # ``inside`` is only assigned so the test seeds a
    # in-window row explicitly; the assertion that follows
    # is the per-status total above.
    assert inside.status == MessageStatus.SENT


async def test_message_status_summary_rejects_inverted_range(
    async_session, fake_providers, messaging_settings
) -> None:
    """An inverted ``since`` / ``until`` is rejected with a
    422-friendly :class:`InvalidListFilterError` so the
    dashboard can surface a useful inline error."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidListFilterError) as exc:
        await message_status_summary(
            async_session,
            client=client,
            since=datetime(2030, 1, 1, tzinfo=UTC),
            until=datetime(2020, 1, 1, tzinfo=UTC),
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_date_range"


async def test_message_status_summary_rejects_oversized_range(
    async_session, fake_providers, messaging_settings
) -> None:
    """A range wider than the hard cap is rejected before the
    database is hit so a curious operator cannot force a
    full-table aggregation."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidListFilterError) as exc:
        await message_status_summary(
            async_session,
            client=client,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            until=datetime(2026, 1, 1, tzinfo=UTC),
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_date_range"


async def test_message_status_summary_empty_history_returns_zero_totals(
    async_session, fake_providers, messaging_settings
) -> None:
    """A customer who has never sent a message gets a
    well-formed response (every status zero-filled, every
    headline counter at zero, ``delivery_rate`` at 0.0) so
    the dashboard can render the "todavía no has enviado
    mensajes" empty state without a special-case branch."""
    client = await _make_client(async_session)
    summary = await message_status_summary(
        async_session,
        client=client,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 30, tzinfo=UTC),
        settings=messaging_settings,
    )
    assert summary.total == 0
    assert summary.delivered == 0
    assert summary.failed == 0
    assert summary.pending == 0
    assert summary.cost_clp == 0
    assert summary.fee_clp == 0
    assert summary.delivery_rate == 0.0
    # Every status row is present, all with a count of zero –
    # the dashboard iterates the list without checking for
    # ``None`` rows.
    assert {row.status for row in summary.items} == set(MessageStatus)
    assert all(row.count == 0 for row in summary.items)


async def test_message_status_summary_rejects_unknown_channel(
    async_session, fake_providers, messaging_settings
) -> None:
    """A bogus channel string is a 422 (not a silent empty
    list) so a typo in a future caller surfaces immediately."""
    client = await _make_client(async_session)
    with pytest.raises(InvalidListFilterError) as exc:
        await message_status_summary(
            async_session,
            client=client,
            channel="carrier-pigeon",
            settings=messaging_settings,
        )
    assert exc.value.code == "invalid_channel"


async def test_message_status_summary_sums_cost_and_fee(
    async_session, fake_providers, messaging_settings
) -> None:
    """The ``cost_clp`` / ``fee_clp`` fields of the response
    are the sum of the matching columns across every row in
    the window, regardless of the row's status. The test
    seeds rows with a mix of costs / fees and asserts the
    response carries the summed values verbatim."""
    client = await _make_client(async_session)
    when = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    await _seed_message_at(
        async_session, client_id=client.id, channel=Channel.WHATSAPP,
        status=MessageStatus.DELIVERED, when=when, body="d",
    )
    # Manually patch the cost / fee columns for each seeded
    # row so the assertion is independent of the cost /
    # markup numbers the service module hard-codes. The
    # values are arbitrary integers – only the sum matters.
    rows = list(
        (await async_session.execute(select(Message))).scalars().all()
    )
    rows[0].cost_clp = 100
    rows[0].fee_clp = 10
    await async_session.commit()

    summary = await message_status_summary(
        async_session,
        client=client,
        since=when - timedelta(days=1),
        until=when + timedelta(days=1),
        settings=messaging_settings,
    )
    assert summary.cost_clp == 100
    assert summary.fee_clp == 10

