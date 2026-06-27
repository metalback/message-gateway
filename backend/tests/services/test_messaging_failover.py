"""Unit tests for the failover integration in the messaging service (issue #11).

The tests cover the end-to-end flow when the registry returns a
:class:`~app.adapters.failover.FailoverProvider` instead of a
single-provider adapter:

- The ``Message.provider`` column records the *actual* upstream
  that handled the call (taken from
  :attr:`app.adapters.base.SendResult.provider_name`), so an
  operator can tell a failover happened just by reading the row.
- The failover is transparent to the rest of the service: the
  same :func:`app.services.messaging.send_message` entry point
  works for both single-provider and multi-provider chains.
- A retryable error from the primary is hidden from the caller –
  the message is still marked ``sent`` because the fallback
  succeeded.
- A validation error from the primary is surfaced as
  ``failed`` (the request is malformed, the fallback would fail
  the same way).

The tests use the same :class:`FakeProvider` pattern as the
existing service suite (see
:mod:`tests.services.test_messaging`) so a reader familiar
with the file can scan the new cases without learning a new
fixture. The provider stub exposes a configurable
``failover_target`` so a single test can wire up a chain
where the primary raises and the fallback succeeds.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import (
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.config import Settings
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.message import Channel, Message, MessageStatus
from app.services.messaging import send_message

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _ScriptedProvider(BaseProvider):
    """A :class:`BaseProvider` whose ``send`` outcome is configurable per call.

    The constructor accepts two outcomes – the first is used for the
    initial ``send`` call, the second is used for any subsequent
    ``send`` call. The double also records every call so the test
    can assert on the routing decision the chain made.
    """

    def __init__(
        self,
        *,
        name: str,
        outcomes: list[SendResult | BaseException],
    ) -> None:
        self.name = name
        self._outcomes = list(outcomes)
        self.send_calls: list[dict[str, Any]] = []

    async def send(self, *, to: str, body: str, **kwargs: object) -> SendResult:
        self.send_calls.append({"to": to, "body": body, **kwargs})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_status(self, provider_msg_id: str) -> str:
        return "sent"


@pytest.fixture
def failover_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings with the failover chain configured for ``whatsapp``.

    The chain names ``meta_whatsapp`` as the primary and
    ``twilio_whatsapp`` (a provider the test registers through
    :func:`register_failover_provider`) as the fallback. The
    :class:`FailoverProvider` is created at the registry level, so
    the service layer sees a regular :class:`BaseProvider` and
    exercises the same code path it does for any other send.
    """
    settings = Settings(
        meta_whatsapp_access_token="t",
        meta_whatsapp_phone_number_id="p",
        sms_aggregator_api_url="https://sms.test",
        sms_aggregator_api_key="k",
        sms_aggregator_sender_id="MSGGTWY",
        provider_failover_chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    return settings


@pytest.fixture
def scripted_providers(
    monkeypatch: pytest.MonkeyPatch, failover_settings: Settings
) -> dict[str, _ScriptedProvider]:
    """Patch the primaries so :func:`get_provider` returns
    :class:`_ScriptedProvider` instances.

    The fixture also wires the ``twilio_whatsapp`` fallback
    through :func:`register_failover_provider` and stores it
    under the ``twilio_whatsapp`` key. The same pattern is
    used by the existing :func:`fake_providers` fixture; the
    new fixture is opt-in so the failover tests do not bleed
    into the rest of the suite.
    """
    import app.adapters.registry as registry

    meta = _ScriptedProvider(
        name="meta_whatsapp",
        outcomes=[SendResult(provider_msg_id="wamid-1", raw={})],
    )
    sms = _ScriptedProvider(
        name="sms_aggregator",
        outcomes=[SendResult(provider_msg_id="agg-1", raw={})],
    )
    twilio = _ScriptedProvider(
        name="twilio_whatsapp",
        outcomes=[SendResult(provider_msg_id="twilio-1", raw={})],
    )
    monkeypatch.setitem(
        registry._BUILDERS,
        Channel.WHATSAPP,
        lambda _settings: meta,
    )
    monkeypatch.setitem(
        registry._BUILDERS,
        Channel.SMS,
        lambda _settings: sms,
    )
    monkeypatch.setitem(
        registry._FAILOVER_BUILDERS,
        "twilio_whatsapp",
        lambda _settings: twilio,
    )
    providers: dict[str, _ScriptedProvider] = {
        "meta_whatsapp": meta,
        "sms_aggregator": sms,
        "twilio_whatsapp": twilio,
    }
    return providers


async def _make_client(async_session) -> Client:
    """Build and persist a minimal :class:`Client` for the test."""
    client = Client(
        name="Acme",
        email="ops@acme.cl",
        rut="12345678-5",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(client)
    await async_session.flush()
    return client


# ---------------------------------------------------------------------------
# Happy path: no failover triggered
# ---------------------------------------------------------------------------


async def test_send_message_uses_primary_when_chain_is_configured(
    async_session, scripted_providers, failover_settings
) -> None:
    """A successful primary dispatch must look identical to
    the pre-failover flow: the row is marked ``sent`` with
    ``provider=meta_whatsapp`` and the fallback is not
    called. The test pins the no-op behaviour of the chain
    so a future change does not silently route every
    message through a redundant fallback."""
    client = await _make_client(async_session)
    outcome = await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56912345678",
        body="hola",
        settings=failover_settings,
    )

    assert outcome.message.status == MessageStatus.SENT
    assert outcome.message.provider == "meta_whatsapp"
    assert outcome.provider_msg_id == "wamid-1"
    assert scripted_providers["meta_whatsapp"].send_calls
    assert scripted_providers["twilio_whatsapp"].send_calls == []


# ---------------------------------------------------------------------------
# Failover: retryable error from the primary
# ---------------------------------------------------------------------------


async def test_send_message_records_actual_provider_after_failover(
    async_session, scripted_providers, failover_settings
) -> None:
    """When the primary fails with a retryable error
    (5xx-class), the fallback handles the dispatch and the
    ``Message.provider`` column reflects the *actual*
    upstream that accepted the message. An operator
    looking at the row can tell the failover happened
    without re-running the request."""
    scripted_providers["meta_whatsapp"]._outcomes = [
        ProviderUnavailableError("meta 502", provider="meta_whatsapp")
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

    assert outcome.message.status == MessageStatus.SENT
    assert outcome.message.provider == "twilio_whatsapp"
    assert outcome.provider_msg_id == "twilio-1"
    assert outcome.message.error_code is None
    assert outcome.message.error_message is None


async def test_send_message_fallback_receives_canonical_destination(
    async_session, scripted_providers, failover_settings
) -> None:
    """The canonicalisation step (raw ``+56 9 1234 5678``
    becomes ``+56912345678``) must run *before* the
    failover, so the fallback sees the same number the
    primary would have. A misroute that left the canonical
    form for the primary only would surface as a 400 from
    the fallback – the test pins the contract."""
    scripted_providers["meta_whatsapp"]._outcomes = [
        ProviderUnavailableError("meta down", provider="meta_whatsapp")
    ]
    client = await _make_client(async_session)
    await send_message(
        async_session,
        client=client,
        channel=Channel.WHATSAPP,
        to="+56 9 1234 5678",
        body="hola",
        settings=failover_settings,
    )

    assert scripted_providers["twilio_whatsapp"].send_calls == [
        {"to": "+56912345678", "body": "hola"}
    ]


# ---------------------------------------------------------------------------
# Failover: non-retryable error from the primary
# ---------------------------------------------------------------------------


async def test_send_message_marks_failed_on_validation_error_with_chain(
    async_session, scripted_providers, failover_settings
) -> None:
    """A validation error (4xx-class) from the primary
    short-circuits the chain: the fallback must not see
    the call (a bad number would fail the same way), and
    the row is marked ``failed`` with the primary's error
    code so an operator can debug the request."""
    scripted_providers["meta_whatsapp"]._outcomes = [
        ProviderValidationError("bad number", provider="meta_whatsapp")
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

    assert outcome.message.status == MessageStatus.FAILED
    assert outcome.message.provider == "meta_whatsapp"
    assert outcome.message.error_code == "provider_validation"
    assert outcome.message.error_message == "bad number"
    assert outcome.provider_msg_id is None
    assert scripted_providers["twilio_whatsapp"].send_calls == []


# ---------------------------------------------------------------------------
# Multiple fallbacks
# ---------------------------------------------------------------------------


async def test_send_message_walks_full_chain_until_success(
    async_session, scripted_providers, failover_settings
) -> None:
    """When two providers fail with retryable errors and a
    third one is configured, the chain walks through all of
    them. The MVP ships with only two providers per channel
    (primary + one fallback), but the test pins the
    "walk-the-whole-chain" contract so a future Twilio
    integration does not have to re-derive it."""
    # Register an extra fallback in front of the existing
    # one. The chain becomes:
    #   meta_whatsapp -> twilio_a -> twilio_whatsapp
    import app.adapters.registry as registry

    twilio_a = _ScriptedProvider(
        name="twilio_a_whatsapp",
        outcomes=[
            ProviderUnavailableError("twilio_a down", provider="twilio_a_whatsapp")
        ],
    )
    registry._FAILOVER_BUILDERS["twilio_a_whatsapp"] = lambda _s: twilio_a
    failover_settings.provider_failover_chains = {
        "whatsapp": ["meta_whatsapp", "twilio_a_whatsapp", "twilio_whatsapp"]
    }
    scripted_providers["meta_whatsapp"]._outcomes = [
        ProviderUnavailableError("meta down", provider="meta_whatsapp")
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

    assert outcome.message.status == MessageStatus.SENT
    assert outcome.message.provider == "twilio_whatsapp"
    assert outcome.provider_msg_id == "twilio-1"
    assert scripted_providers["meta_whatsapp"].send_calls
    assert twilio_a.send_calls
    assert scripted_providers["twilio_whatsapp"].send_calls


# ---------------------------------------------------------------------------
# Provider name passthrough on the persisted row
# ---------------------------------------------------------------------------


async def test_send_message_provider_column_reflects_underlying_provider(
    async_session, scripted_providers, failover_settings
) -> None:
    """The ``Message.provider`` column is the *audit trail*
    of the routing decision. The test asserts the value is
    the underlying provider's name (not the wrapper's
    ``"meta_whatsapp+twilio_whatsapp"`` synthetic name) so
    an operator can read the row without having to resolve
    the chain from the configuration."""
    scripted_providers["meta_whatsapp"]._outcomes = [
        ProviderUnavailableError("meta 502", provider="meta_whatsapp")
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

    # Persist + reload to make sure the column made it to the DB.
    from sqlalchemy import select

    stmt = select(Message).where(Message.id == outcome.message.id)
    row = (await async_session.execute(stmt)).scalar_one()
    assert row.provider == "twilio_whatsapp"
    # And the synthetic wrapper name never leaks.
    assert "+" not in row.provider


# ---------------------------------------------------------------------------
# No chain configured (regression)
# ---------------------------------------------------------------------------


async def test_send_message_without_failover_chain_uses_provider_name(
    async_session, scripted_providers, failover_settings
) -> None:
    """A ``Settings`` instance with an *empty* chain map
    keeps the pre-failover behaviour bit-for-bit. The
    ``provider_failover_chains`` test path uses
    :class:`Settings` instances directly so the regression
    is checked without depending on environment variables."""
    failover_settings.provider_failover_chains = {}
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
    assert outcome.message.status == MessageStatus.SENT
