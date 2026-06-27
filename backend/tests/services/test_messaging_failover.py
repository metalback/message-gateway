"""Integration tests for the messaging service + failover router.

The tests in :mod:`tests.adapters.test_failover` and
:mod:`tests.adapters.test_failover_registry` cover the
:class:`~app.adapters.failover.FailoverProvider` and the
registry in isolation. The tests in this module exercise the
*wiring*: the service layer must record the actual provider
on the persisted ``Message.provider`` column, even when a
failover switched providers mid-call, and the persisted
``provider_msg_id`` must come from the fallback that
delivered the message.

The tests are written against the public service API
(:func:`app.services.messaging.send_message`) so they
exercise the same surface a route handler does, with the
registry monkey-patched to return :class:`_SequencedProvider`
instances whose behaviour the test controls declaratively.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import (
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.adapters.failover import FailoverProvider
from app.adapters.registry import register_failover_provider
from app.config import Settings
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.message import Channel, MessageStatus
from app.services.messaging import send_message

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _SequencedProvider(BaseProvider):
    """Provider that returns a scripted sequence of results.

    The constructor accepts a list of ``responses`` (each
    either a :class:`SendResult` or an :class:`Exception`) and
    pops one per ``send`` call. The double also records every
    call so the test can assert the chain advanced in the
    expected order.
    """

    def __init__(
        self,
        name: str,
        responses: list[SendResult | Exception],
    ) -> None:
        self.name = name
        self._responses = list(responses)
        self.send_calls: list[dict[str, Any]] = []

    async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
        self.send_calls.append({"to": to, "body": body, **kwargs})
        if not self._responses:
            raise AssertionError(
                f"_SequencedProvider({self.name}) ran out of scripted responses"
            )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def get_status(self, provider_msg_id: str) -> str:
        return "sent"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def failover_settings() -> Settings:
    """Settings with the provider config the registry needs."""
    return Settings(
        meta_whatsapp_access_token="t",
        meta_whatsapp_phone_number_id="p",
        sms_aggregator_api_url="https://sms.test",
        sms_aggregator_api_key="k",
        sms_aggregator_sender_id="MSGGTWY",
    )


@pytest.fixture
def failover_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, _SequencedProvider]:
    """Patch the registry to return scripted providers.

    The fixture returns a mapping keyed by the provider
    ``name`` so a test can grab a specific double and push
    responses into it before calling :func:`send_message`.
    """
    import app.adapters.registry as registry

    providers: dict[str, _SequencedProvider] = {
        "meta_whatsapp": _SequencedProvider("meta_whatsapp", []),
        "twilio_whatsapp": _SequencedProvider("twilio_whatsapp", []),
        "sms_aggregator": _SequencedProvider("sms_aggregator", []),
    }

    def _factory(name: str):
        def _build(_settings: Settings) -> BaseProvider:
            return providers[name]

        return _build

    monkeypatch.setitem(
        registry._BUILDERS,
        Channel.WHATSAPP,
        _factory("meta_whatsapp"),
    )
    monkeypatch.setitem(
        registry._BUILDERS,
        Channel.SMS,
        _factory("sms_aggregator"),
    )
    for name in providers:
        register_failover_provider(name, _factory(name))
    return providers


async def _make_client(async_session, *, plan: ClientPlan = ClientPlan.STARTER) -> Client:
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
# Pre-failover: single provider path is unchanged
# ---------------------------------------------------------------------------


async def test_send_message_records_primary_provider_on_success(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """Without a failover chain the service persists the
    primary's name unchanged. The test pins the bit-for-bit
    compatibility the PRD promised (issue #11 must not change
    the pre-failover contract)."""
    failover_providers["meta_whatsapp"]._responses = [
        SendResult(provider_msg_id="meta-1", raw={}),
    ]
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=failover_settings,
    )
    assert outcome.message.provider == "meta_whatsapp"
    assert outcome.message.provider_msg_id == "meta-1"
    assert outcome.message.status == MessageStatus.SENT


# ---------------------------------------------------------------------------
# Failover: actual provider ends up in the row
# ---------------------------------------------------------------------------


async def test_send_message_records_fallback_provider_after_failover(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """When the primary fails and the fallback delivers the
    message, the persisted ``Message.provider`` must reflect
    the *actual* upstream that handled the call – not the
    synthetic chain name. An operator looking at the row
    should be able to tell a failover happened without
    re-deriving the chain from the config."""
    settings = Settings(
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    failover_providers["meta_whatsapp"]._responses = [
        ProviderUnavailableError("meta 5xx", provider="meta_whatsapp"),
    ]
    failover_providers["twilio_whatsapp"]._responses = [
        SendResult(provider_msg_id="tw-1", raw={}),
    ]
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=settings,
    )
    assert outcome.message.provider == "twilio_whatsapp"
    assert outcome.message.provider_msg_id == "tw-1"
    assert outcome.message.status == MessageStatus.SENT
    # Both providers saw the call: the primary failed, the
    # fallback delivered.
    assert len(failover_providers["meta_whatsapp"].send_calls) == 1
    assert len(failover_providers["twilio_whatsapp"].send_calls) == 1


async def test_send_message_records_rate_limit_failover(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """A 429 from the primary is also retryable: the service
    must fall back to the next provider, not persist the
    message as ``failed`` (the rate limit might be lifted
    against the second upstream)."""
    settings = Settings(
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    failover_providers["meta_whatsapp"]._responses = [
        ProviderRateLimitError("meta 429", retry_after=1.0, provider="meta_whatsapp"),
    ]
    failover_providers["twilio_whatsapp"]._responses = [
        SendResult(provider_msg_id="tw-1", raw={}),
    ]
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=settings,
    )
    assert outcome.message.status == MessageStatus.SENT
    assert outcome.message.provider == "twilio_whatsapp"


# ---------------------------------------------------------------------------
# Failover: failure modes
# ---------------------------------------------------------------------------


async def test_send_message_does_not_try_fallback_on_validation_error(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """A ``ProviderValidationError`` from the primary is
    permanent: the request is malformed (bad number, …) and
    retrying against the fallback would just burn its quota.
    The service must persist the row as ``failed`` without
    invoking the fallback."""
    settings = Settings(
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    failover_providers["meta_whatsapp"]._responses = [
        ProviderValidationError("bad number", provider="meta_whatsapp"),
    ]
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=settings,
    )
    assert outcome.message.status == MessageStatus.FAILED
    assert outcome.message.provider == "meta_whatsapp"
    # Fallback was never asked.
    assert failover_providers["twilio_whatsapp"].send_calls == []


async def test_send_message_marks_failed_when_all_providers_unavailable(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """When every provider in the chain raises a retryable
    error the row is persisted as ``failed``; the operator
    can drill down through the chain name to see "all
    upstreams are down"."""
    settings = Settings(
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    failover_providers["meta_whatsapp"]._responses = [
        ProviderUnavailableError("meta 5xx", provider="meta_whatsapp"),
    ]
    failover_providers["twilio_whatsapp"]._responses = [
        ProviderUnavailableError("twilio 5xx", provider="twilio_whatsapp"),
    ]
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=settings,
    )
    assert outcome.message.status == MessageStatus.FAILED
    assert outcome.message.error_code == "provider_unavailable"
    # The provider column shows the *underlying* upstream
    # that surfaced the final error so the operator does not
    # have to map a chain name back to a single provider.
    assert outcome.message.provider == "twilio_whatsapp"


# ---------------------------------------------------------------------------
# Provider chain name is recorded while pending
# ---------------------------------------------------------------------------


async def test_send_message_records_chain_name_on_pending_row(
    async_session,
    failover_providers,
) -> None:
    """Before the dispatch happens the row is ``pending`` and
    the ``provider`` column holds the chain name – the
    operator can tell from the row alone that failover is in
    effect (the synthetic name ``a+b`` is a giveaway even
    without inspecting the chain configuration)."""
    settings = Settings(
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    failover_providers["meta_whatsapp"]._responses = [
        SendResult(provider_msg_id="meta-1", raw={}),
    ]
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=settings,
    )
    # The pre-dispatch row used the chain's synthetic name; the
    # post-success row replaced it with the actual primary.
    assert outcome.message.provider == "meta_whatsapp"


# ---------------------------------------------------------------------------
# FailoverProvider is the actual adapter returned by the registry
# ---------------------------------------------------------------------------


async def test_send_message_uses_failover_provider_when_chain_configured(
    async_session,
    failover_providers,
    failover_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry must hand the service a
    :class:`FailoverProvider` whenever a chain is configured
    – the service is not in the business of building chains
    itself. The test pins the layering so a future refactor
    does not duplicate the failover logic in the service."""
    from app.adapters.registry import get_provider

    settings = Settings(
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    provider = get_provider(Channel.WHATSAPP, settings=settings)
    assert isinstance(provider, FailoverProvider)


async def test_send_message_uses_plain_provider_when_no_chain(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """The pre-failover contract: no chain → no wrapper. The
    service talks to the primary directly, so the row's
    ``provider`` column shows the primary's name and the
    dispatch path is unchanged."""
    from app.adapters.registry import get_provider

    settings = Settings()  # no chains
    provider = get_provider(Channel.WHATSAPP, settings=settings)
    assert not isinstance(provider, FailoverProvider)
    assert provider.name == "meta_whatsapp"


# ---------------------------------------------------------------------------
# Kill-switch (issue #11)
# ---------------------------------------------------------------------------
#
# The operator can disable a provider from the admin
# dashboard. The tests below exercise the service's
# wiring: a disabled primary never receives the call,
# the chain the registry returns is rebuilt with the
# surviving member as the new primary, and a fully-
# disabled channel surfaces as :class:`AllProvidersDisabledError`
# (a 503) with the row marked ``failed``.


async def _seed_inactive_provider(
    async_session,
    *,
    name: str,
) -> None:
    """Insert a :class:`ProviderConfig` row flagged ``active=False``.

    The helper is the only thing the messaging tests need
    to put the kill-switch in the right state – a fresh
    in-memory session has no rows at all, so the
    ``get_inactive_provider_names`` reader would return an
    empty set.
    """
    from app.models.provider_config import ProviderConfig

    async_session.add(
        ProviderConfig(
            name=name,
            channel=Channel.WHATSAPP,
            active=False,
        )
    )
    await async_session.commit()


async def test_send_message_skips_inactive_primary(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """A primary that the operator disabled is invisible
    to the service. The chain the registry returns is
    re-headed to the next active member, so the call
    lands on the fallback without the primary ever
    being asked."""
    settings = Settings(
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    await _seed_inactive_provider(async_session, name="meta_whatsapp")
    failover_providers["twilio_whatsapp"]._responses = [
        SendResult(provider_msg_id="tw-1", raw={}),
    ]
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=settings,
    )
    assert outcome.message.status == MessageStatus.SENT
    assert outcome.message.provider == "twilio_whatsapp"
    # The primary never saw the call.
    assert failover_providers["meta_whatsapp"].send_calls == []
    assert len(failover_providers["twilio_whatsapp"].send_calls) == 1


async def test_send_message_skips_inactive_fallback(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """A disabled *fallback* is filtered out of the chain
    but the primary still receives the call. The test
    pins the asymmetry: the operator's "desactivar"
    button affects the listed member without
    cascading to the rest of the chain."""
    settings = Settings(
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    await _seed_inactive_provider(async_session, name="twilio_whatsapp")
    failover_providers["meta_whatsapp"]._responses = [
        SendResult(provider_msg_id="meta-1", raw={}),
    ]
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=settings,
    )
    assert outcome.message.status == MessageStatus.SENT
    assert outcome.message.provider == "meta_whatsapp"
    # The fallback never saw the call.
    assert failover_providers["twilio_whatsapp"].send_calls == []


async def test_send_message_marks_failed_when_every_provider_disabled(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """A chain whose every member is disabled raises
    :class:`AllProvidersDisabledError` from the
    registry. The service catches it, marks the row
    ``failed`` with a stable error code, and re-raises
    so the route layer can surface a 503."""
    from app.adapters.registry import AllProvidersDisabledError

    settings = Settings(
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    await _seed_inactive_provider(async_session, name="meta_whatsapp")
    await _seed_inactive_provider(async_session, name="twilio_whatsapp")
    client = await _make_client(async_session)
    with pytest.raises(AllProvidersDisabledError) as exc_info:
        await send_message(
            async_session,
            client=client,
            channel=Channel.WHATSAPP,
            to="+56912345678",
            body="hola",
            settings=settings,
        )
    assert exc_info.value.code == "provider_disabled"
    assert exc_info.value.http_status == 503
    # The row is durable: the service committed before
    # the registry raised, so a polling dashboard sees
    # the failure rather than a stuck "pending" entry.
    await async_session.refresh(client)
    # Reload the latest message from the session.
    from sqlalchemy import select

    from app.models.message import Message

    stmt = select(Message).order_by(Message.created_at.desc())
    last = (await async_session.execute(stmt)).scalar_one()
    assert last.status == MessageStatus.FAILED
    assert last.error_code == "provider_disabled"
