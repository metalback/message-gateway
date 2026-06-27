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

The HTTP layer is exercised through the
:class:`FakeProvider` so the suite never opens a real TCP
connection. The provider implements the
:class:`app.adapters.base.BaseProvider` contract and lets
the test inject the response it wants to simulate.
"""

from __future__ import annotations

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
    InvalidMessageError,
    MessageNotFoundError,
    compute_message_cost,
    get_message_status,
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
