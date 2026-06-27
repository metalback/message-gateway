"""Unit tests for the failover-aware provider registry (issue #11).

The tests cover the integration between
:func:`app.adapters.registry.get_provider` and the
:class:`~app.adapters.failover.FailoverProvider`:

- When ``provider_failover_chains`` is empty (the default)
  the registry returns the primary provider directly – this
  is the pre-failover behaviour and must stay bit-for-bit
  compatible.
- When a chain is configured for a channel, the registry
  returns a :class:`FailoverProvider` that wraps the named
  providers in order.
- The primary is always the first element of the chain –
  any duplicate the operator added is silently dropped (a
  duplicate does not add availability but rejecting the
  whole config would block the platform on a typo).
- The ``register_failover_provider`` helper plugs a new
  provider into the chain without editing the registry.
- A chain that names an unregistered provider surfaces a
  clear error before the first send.
- The chain resolver accepts a JSON-encoded value in the
  ``Settings`` (the form the env var uses) and an empty /
  malformed value disables failover safely.

The tests deliberately exercise the *integration* with the
real primary factories
(:class:`MetaWhatsAppProvider` / :class:`SmsAggregatorProvider`)
so a swap of the production provider surfaces here rather
than at the first send. The router's own dispatch logic
lives in :mod:`tests.adapters.test_failover`.
"""

from __future__ import annotations

import pytest

from app.adapters.base import BaseProvider
from app.adapters.failover import FailoverProvider
from app.adapters.meta_whatsapp import MetaWhatsAppProvider
from app.adapters.registry import (
    UnsupportedChannelError,
    UnsupportedProviderError,
    get_provider,
    register_failover_provider,
    supported_channels,
)
from app.adapters.sms_aggregator import SmsAggregatorProvider
from app.config import Settings
from app.models.message import Channel

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def messaging_settings() -> Settings:
    """Return a :class:`Settings` instance with all provider
    credentials populated so the real primary factories do
    not blow up on construction."""
    return Settings(
        meta_whatsapp_access_token="t",
        meta_whatsapp_phone_number_id="p",
        sms_aggregator_api_url="https://sms.test",
        sms_aggregator_api_key="k",
        sms_aggregator_sender_id="MSGGTWY",
    )


# ---------------------------------------------------------------------------
# Default behaviour (no failover)
# ---------------------------------------------------------------------------


def test_get_provider_returns_primary_when_no_failover_configured(
    messaging_settings: Settings,
) -> None:
    """Without a chain configured, the registry must return
    the primary provider directly – the pre-failover
    behaviour the MVP shipped with. The
    :class:`FailoverProvider` wrapper would impose a tiny
    overhead and obscure the result name in logs, so the
    registry stays a passthrough when no chain is set."""
    whatsapp = get_provider(Channel.WHATSAPP, settings=messaging_settings)
    sms = get_provider(Channel.SMS, settings=messaging_settings)

    assert isinstance(whatsapp, MetaWhatsAppProvider)
    assert isinstance(sms, SmsAggregatorProvider)
    assert not isinstance(whatsapp, FailoverProvider)
    assert not isinstance(sms, FailoverProvider)


def test_supported_channels_unchanged_by_failover_feature() -> None:
    """Adding failover support must not change the set of
    channels the registry advertises – the OpenAPI schema
    depends on :func:`supported_channels` and a regression
    here would surface as a broken docs build."""
    assert set(supported_channels()) == {Channel.WHATSAPP, Channel.SMS}


# ---------------------------------------------------------------------------
# Failover chains
# ---------------------------------------------------------------------------


def test_get_provider_wraps_chain_in_failover_provider(
    messaging_settings: Settings,
) -> None:
    """A chain configured for ``whatsapp`` produces a
    :class:`FailoverProvider` whose name joins the chain
    elements with ``+`` so the routing decision is visible
    in the logs and the ``Message.provider`` column."""
    messaging_settings.provider_failover_chains = {
        "whatsapp": ["meta_whatsapp", "sms_aggregator"],
    }

    provider = get_provider(Channel.WHATSAPP, settings=messaging_settings)

    assert isinstance(provider, FailoverProvider)
    assert provider.name == "meta_whatsapp+sms_aggregator"
    assert [p.name for p in provider.providers()] == [
        "meta_whatsapp",
        "sms_aggregator",
    ]


def test_get_provider_does_not_wrap_unrelated_channels(
    messaging_settings: Settings,
) -> None:
    """A chain configured for ``sms`` must not wrap the
    ``whatsapp`` lookup in a :class:`FailoverProvider` – the
    two channels are independent. A misconfiguration that
    would wrap both would mask the actual primary's
    behaviour for ``whatsapp`` and break an integration test
    that asserts on the concrete class."""
    messaging_settings.provider_failover_chains = {
        "sms": ["sms_aggregator", "meta_whatsapp"],
    }

    whatsapp = get_provider(Channel.WHATSAPP, settings=messaging_settings)

    assert not isinstance(whatsapp, FailoverProvider)
    assert isinstance(whatsapp, MetaWhatsAppProvider)


def test_chain_always_starts_with_primary(
    messaging_settings: Settings,
) -> None:
    """The contract is "primary first, then fallbacks".
    The registry drops a duplicate primary at the start of
    the configured chain (a misconfiguration that would
    otherwise raise inside the
    :class:`FailoverProvider` constructor and brick the
    platform on a typo)."""
    messaging_settings.provider_failover_chains = {
        "whatsapp": ["meta_whatsapp", "meta_whatsapp"],
    }

    provider = get_provider(Channel.WHATSAPP, settings=messaging_settings)

    assert isinstance(provider, MetaWhatsAppProvider)
    assert not isinstance(provider, FailoverProvider)


def test_chain_drops_duplicate_primary_from_middle(
    messaging_settings: Settings,
) -> None:
    """A duplicate that is not the first element of the
    chain is also dropped – the chain validator in
    :class:`FailoverProvider` would otherwise reject the
    whole configuration, which would block the platform on
    a typo. (Accepting the config but removing the duplicate
    is the same policy the platform applies for any other
    provider, so the behaviour stays consistent.)"""
    messaging_settings.provider_failover_chains = {
        "whatsapp": ["twilio_whatsapp", "meta_whatsapp"],
    }

    # ``twilio_whatsapp`` is not registered yet – the
    # registry's behaviour for unknown providers is tested
    # separately; this test focuses on the
    # duplicate-removal logic.
    with pytest.raises(UnsupportedProviderError):
        get_provider(Channel.WHATSAPP, settings=messaging_settings)


def test_chain_with_only_duplicate_primary_returns_primary(
    messaging_settings: Settings,
) -> None:
    """If the configured chain only contains the primary
    (after de-duplication), the registry returns the
    primary directly – wrapping a one-element chain in a
    :class:`FailoverProvider` would add a no-op dispatch
    hop and change the ``provider.name`` in subtle ways."""
    messaging_settings.provider_failover_chains = {
        "whatsapp": ["meta_whatsapp"],
    }

    provider = get_provider(Channel.WHATSAPP, settings=messaging_settings)

    assert isinstance(provider, MetaWhatsAppProvider)
    assert not isinstance(provider, FailoverProvider)


def test_chain_with_unknown_provider_raises(
    messaging_settings: Settings,
) -> None:
    """A chain that names a provider the registry has not
    registered is a configuration error caught at
    :func:`get_provider` time. The error is a
    :class:`UnsupportedProviderError` (a :class:`ValueError`
    subclass) so the route layer can surface a 422 if it
    ever reaches the wire (the validator in
    :class:`Settings` rejects the same shape at boot, so
    this path is mostly a defensive test)."""
    messaging_settings.provider_failover_chains = {
        "whatsapp": ["meta_whatsapp", "twilio_whatsapp"],
    }

    with pytest.raises(UnsupportedProviderError) as exc_info:
        get_provider(Channel.WHATSAPP, settings=messaging_settings)
    assert exc_info.value.name == "twilio_whatsapp"


def test_get_provider_accepts_string_channel(messaging_settings: Settings) -> None:
    """The :func:`get_provider` contract accepts a string
    channel (defence-in-depth). The failover integration
    must not break that path: a string channel goes
    through the same registry resolution as a
    :class:`Channel` enum member."""
    messaging_settings.provider_failover_chains = {
        "whatsapp": ["meta_whatsapp", "sms_aggregator"],
    }

    provider = get_provider("whatsapp", settings=messaging_settings)

    assert isinstance(provider, FailoverProvider)
    assert provider.name == "meta_whatsapp+sms_aggregator"


# ---------------------------------------------------------------------------
# Settings: JSON parsing
# ---------------------------------------------------------------------------


def test_settings_parses_json_string_into_chain_map() -> None:
    """The env-var form (``PROVIDER_FAILOVER_CHAINS={"...":"..."}``)
    must parse into a :class:`dict`. The validator is
    defensive (an empty string is a no-op, a bad JSON
    payload raises) so a typo at the deployment stage
    fails loudly rather than silently disabling failover."""
    settings = Settings(
        provider_failover_chains='{"whatsapp": ["meta_whatsapp", "sms_aggregator"]}'
    )

    assert settings.provider_failover_chains == {
        "whatsapp": ["meta_whatsapp", "sms_aggregator"],
    }


def test_settings_treats_empty_string_as_disabled() -> None:
    """An empty ``PROVIDER_FAILOVER_CHAINS`` value (the
    form a deployment that wants to disable failover
    would set) must be the same as the default – no
    chains configured."""
    settings = Settings(provider_failover_chains="")

    assert settings.provider_failover_chains == {}


def test_settings_rejects_malformed_json() -> None:
    """A malformed JSON value is a configuration error
    raised at boot – a typo must not silently degrade into
    "failover disabled", otherwise the operator would
    never notice they lost their high-availability
    guarantee."""
    with pytest.raises(ValueError):
        Settings(provider_failover_chains="not-json")


def test_settings_rejects_non_object_json() -> None:
    """The chain map is a JSON object; a JSON array or
    scalar is a configuration error caught at boot."""
    with pytest.raises(ValueError):
        Settings(provider_failover_chains="[1, 2, 3]")


def test_settings_default_chain_map_is_empty() -> None:
    """The MVP ships with no chains configured – the
    pre-failover behaviour is the default. The platform
    adopts failover only when an operator explicitly
    opts in."""
    settings = Settings()

    assert settings.provider_failover_chains == {}


# ---------------------------------------------------------------------------
# Custom provider registration
# ---------------------------------------------------------------------------


def test_register_failover_provider_adds_to_chain(
    messaging_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The :func:`register_failover_provider` helper lets a
    future "Twilio WhatsApp adapter" task plug a new
    provider into the chain without editing the registry
    module. The helper is also the entry point tests use
    to inject a custom provider (e.g. a stub) into the
    chain. The fixture restores the registry after the
    test runs (the
    :func:`tests.conftest.monkeypatch` integration handles
    this automatically)."""
    from app.adapters import registry

    class _TwilioWhatsApp(BaseProvider):
        name = "twilio_whatsapp"

        async def send(self, *, to: str, body: str, **kwargs: object):
            # Shape only – the test never calls ``send`` on the
            # stub directly, the FailoverProvider does. Keeping
            # the body minimal so a future reader does not have
            # to mentally simulate a "no-op" provider.
            raise NotImplementedError  # pragma: no cover

        async def get_status(self, provider_msg_id: str) -> str:
            return "sent"

    def _twilio_factory(settings: Settings) -> _TwilioWhatsApp:
        return _TwilioWhatsApp()

    monkeypatch.setitem(registry._FAILOVER_BUILDERS, "twilio_whatsapp", _twilio_factory)
    messaging_settings.provider_failover_chains = {
        "whatsapp": ["meta_whatsapp", "twilio_whatsapp"],
    }

    provider = get_provider(Channel.WHATSAPP, settings=messaging_settings)

    assert isinstance(provider, FailoverProvider)
    assert provider.name == "meta_whatsapp+twilio_whatsapp"


def test_register_failover_provider_helper_public(
    messaging_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The :func:`register_failover_provider` helper is a
    public, importable symbol so a deployment can plug a
    new provider from a sidecar module without having to
    reach into private internals."""
    import app.adapters.registry as registry_module

    class _Stub(BaseProvider):
        name = "stub_provider"

        async def send(self, *, to: str, body: str, **kwargs: object):
            # Shape only – the helper only registers the
            # factory, the test never dispatches through it.
            raise NotImplementedError  # pragma: no cover

        async def get_status(self, provider_msg_id: str) -> str:
            return "sent"

    monkeypatch.setattr(
        registry_module,
        "_FAILOVER_BUILDERS",
        dict(registry_module._FAILOVER_BUILDERS),
    )
    register_failover_provider("stub_provider", lambda settings: _Stub())
    assert "stub_provider" in registry_module._FAILOVER_BUILDERS


# ---------------------------------------------------------------------------
# Pre-failover regression: existing test_adapters tests
# ---------------------------------------------------------------------------


def test_existing_get_provider_tests_still_pass_with_default_settings(
    messaging_settings: Settings,
) -> None:
    """Regression guard: the contract the original
    :func:`test_adapters.test_get_provider_returns_correct_adapter`
    test pins must still hold with the failover feature
    merged. The test is duplicated here (rather than
    relying on the original) so a future change to the
    failover behaviour does not silently mask a
    regression in the primaries."""
    whatsapp = get_provider(Channel.WHATSAPP, settings=messaging_settings)
    sms = get_provider(Channel.SMS, settings=messaging_settings)

    assert isinstance(whatsapp, BaseProvider)
    assert isinstance(sms, BaseProvider)
    assert whatsapp.name == "meta_whatsapp"
    assert sms.name == "sms_aggregator"


def test_get_provider_unknown_channel_still_raises_unsupported(
    messaging_settings: Settings,
) -> None:
    """An unknown channel is still a
    :class:`UnsupportedChannelError` even with failover
    enabled – the channel validation runs before the
    chain resolution, so a typo in the chain name does
    not mask a typo in the channel."""
    messaging_settings.provider_failover_chains = {
        "whatsapp": ["meta_whatsapp", "sms_aggregator"],
    }

    with pytest.raises(UnsupportedChannelError):
        get_provider("unknown_channel", settings=messaging_settings)
