"""Integration tests for the messaging service + ``routing_log`` (issue #11).

The tests in :mod:`tests.adapters.test_failover` cover
the :class:`~app.adapters.failover.FailoverProvider` in
isolation; the tests in
:mod:`tests.services.test_messaging_failover` cover the
provider-name column on :class:`app.models.message.Message`.
The tests in this module cover the *audit log*: every
provider attempt the chain walks must land as a row in
:class:`app.models.routing_log.RoutingLog` so the admin
dashboard can render the "intentos por proveedor" chart
and the per-message trace view.

The tests use the same ``_SequencedProvider`` double the
failover tests use, so a failure points straight at the
wiring that broke.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import (
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.adapters.registry import register_failover_provider
from app.config import Settings
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.message import Channel, MessageStatus
from app.models.routing_log import RoutingLog, RoutingLogOutcome
from app.services.messaging import send_message


class _SequencedProvider(BaseProvider):
    """Provider that returns a scripted sequence of results.

    Mirrors the double in
    :mod:`tests.services.test_messaging_failover` so the
    tests can drive every branch (success / unavailable
    / rate-limit / validation) declaratively. The double
    pops one response per ``send`` call.
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

    The fixture mirrors the one in
    :mod:`tests.services.test_messaging_failover` so the
    tests can be read side-by-side. The
    :class:`FailoverProvider` built by
    :func:`app.adapters.registry.get_provider` calls
    ``meta_whatsapp`` first, falls back to
    ``twilio_whatsapp``; ``sms_aggregator`` is the
    primary for SMS and the test exercises it through
    the single-provider path.
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
# Single provider path: one audit row per dispatch
# ---------------------------------------------------------------------------


async def test_routing_log_records_one_row_per_dispatch_on_success(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """A successful send through the primary writes
    *one* :class:`RoutingLog` row with
    ``outcome="success"`` and the primary's name. The
    audit log captures the single attempt the chain
    walked; the dashboard's per-provider chart counts
    it as one delivery through ``meta_whatsapp``."""
    failover_providers["meta_whatsapp"]._responses = [
        SendResult(provider_msg_id="meta-1", raw={}),
    ]
    client = await _make_client(async_session)
    await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=failover_settings,
    )
    # Re-read the audit log to confirm the row was
    # written and committed.
    rows = (
        await async_session.execute(select(RoutingLog))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].provider_attempted == "meta_whatsapp"
    assert rows[0].outcome == RoutingLogOutcome.SUCCESS
    assert rows[0].message_id is not None
    assert rows[0].latency_ms >= 0
    assert rows[0].error_code is None
    assert rows[0].error_message is None


# ---------------------------------------------------------------------------
# Failover path: one row per attempt
# ---------------------------------------------------------------------------


async def test_routing_log_records_one_row_per_attempt_on_failover(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """When the primary fails and the fallback
    delivers, the audit log records *two* rows: the
    primary's failure (with ``outcome="failure"`` and
    the upstream's error code) and the fallback's
    success. The per-message trace view stitches them
    together via the ``message_id`` foreign key."""
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
    await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=settings,
    )
    rows = (
        await async_session.execute(
            select(RoutingLog).order_by(RoutingLog.attempted_at)
        )
    ).scalars().all()
    assert len(rows) == 2
    by_provider = {r.provider_attempted: r for r in rows}
    assert set(by_provider) == {"meta_whatsapp", "twilio_whatsapp"}
    assert by_provider["meta_whatsapp"].outcome == RoutingLogOutcome.FAILURE
    assert by_provider["meta_whatsapp"].error_code == "provider_unavailable"
    assert by_provider["meta_whatsapp"].error_message == "meta 5xx"
    assert by_provider["twilio_whatsapp"].outcome == RoutingLogOutcome.SUCCESS
    assert by_provider["twilio_whatsapp"].error_code is None
    # Both rows reference the same Message – the
    # per-message trace view stitches them together.
    message_id = by_provider["meta_whatsapp"].message_id
    assert message_id is not None
    assert by_provider["twilio_whatsapp"].message_id == message_id


async def test_routing_log_records_validation_error_outcome(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """A :class:`ProviderValidationError` from the
    primary is a *permanent* failure: the chain does
    not try the fallback. The audit log still records
    the attempt with ``outcome="validation_error"``
    so the dashboard can chart bad-input errors
    separately from upstreams that are merely
    unavailable."""
    settings = Settings(
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    failover_providers["meta_whatsapp"]._responses = [
        ProviderValidationError("bad number", provider="meta_whatsapp"),
    ]
    client = await _make_client(async_session)
    await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=settings,
    )
    rows = (
        await async_session.execute(select(RoutingLog))
    ).scalars().all()
    # Only one row: a validation error short-circuits
    # the chain.
    assert len(rows) == 1
    assert rows[0].provider_attempted == "meta_whatsapp"
    assert rows[0].outcome == RoutingLogOutcome.VALIDATION_ERROR
    assert rows[0].error_code == "provider_validation"
    assert rows[0].error_message == "bad number"
    # The fallback was never asked.
    assert failover_providers["twilio_whatsapp"].send_calls == []


async def test_routing_log_records_rate_limit_failover(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """A 429 from the primary is a retryable error: the
    chain advances to the fallback. The audit log
    records the rate-limit attempt with
    ``outcome="failure"`` and the upstream's
    ``provider_rate_limited`` error code so the
    per-provider chart can attribute the rate-limit
    to the right upstream."""
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
    await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=settings,
    )
    rows = (
        await async_session.execute(select(RoutingLog))
    ).scalars().all()
    assert len(rows) == 2
    by_provider = {r.provider_attempted: r for r in rows}
    assert by_provider["meta_whatsapp"].outcome == RoutingLogOutcome.FAILURE
    assert by_provider["meta_whatsapp"].error_code == "provider_rate_limited"


async def test_routing_log_records_all_retries_when_chain_fails(
    async_session,
    failover_providers,
    failover_settings,
) -> None:
    """When every provider in the chain raises a
    retryable error the audit log captures the whole
    chain: two failure rows and no success row. The
    message is persisted as ``failed`` (the existing
    pre-routing-log behaviour), and the operator can
    drill down through the per-message trace to see
    *every* upstream that was tried."""
    settings = Settings(
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    failover_providers["meta_whatsapp"]._responses = [
        ProviderUnavailableError("meta 5xx", provider="meta_whatsapp"),
    ]
    failover_providers["twilio_whatsapp"]._responses = [
        ProviderUnavailableError("tw 5xx", provider="twilio_whatsapp"),
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
    rows = (
        await async_session.execute(select(RoutingLog))
    ).scalars().all()
    assert len(rows) == 2
    assert all(r.outcome == RoutingLogOutcome.FAILURE for r in rows)
    assert {r.provider_attempted for r in rows} == {
        "meta_whatsapp",
        "twilio_whatsapp",
    }
