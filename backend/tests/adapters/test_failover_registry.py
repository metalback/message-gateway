"""Integration tests for the provider registry's failover path.

These tests exercise :func:`app.adapters.registry.get_provider`
together with :class:`~app.adapters.failover.FailoverProvider`
to pin the contract between the configuration layer
(``Settings.provider_failover_chains``), the registry
(:func:`get_provider`) and the failover router.

The tests are deliberately written against the *public* API
(``get_provider`` + ``provider_failover_chains``) so they
exercise the same surface the platform uses in production.
The internal factory maps are monkey-patched only to inject
deterministic stand-ins (the registry's own builders hit
external services).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.base import BaseProvider, SendResult
from app.adapters.failover import FailoverProvider
from app.adapters.registry import (
    UnsupportedProviderError,
    get_provider,
)
from app.config import Settings
from app.models.message import Channel

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubProvider(BaseProvider):
    """Provider double that records its identity for assertions.

    The tests use this to confirm the registry returned the
    chain it was supposed to (primary first, fallbacks in
    order) without going through any of the concrete adapter
    constructors – those need real credentials.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
        return SendResult(provider_msg_id=f"{self.name}-1", raw={})

    async def get_status(self, provider_msg_id: str) -> str:
        return "sent"


@pytest.fixture
def stub_providers(monkeypatch: pytest.MonkeyPatch) -> dict[str, _StubProvider]:
    """Replace the registry's primary / fallback builders with
    deterministic :class:`_StubProvider` instances.

    Returns a mapping keyed by the builder's ``name`` so a test
    can look up the double it expects to appear in the chain
    without holding a separate variable.
    """
    import app.adapters.registry as registry

    stubs: dict[str, _StubProvider] = {
        "meta_whatsapp": _StubProvider("meta_whatsapp"),
        "twilio_whatsapp": _StubProvider("twilio_whatsapp"),
        "sms_aggregator": _StubProvider("sms_aggregator"),
        "twilio_sms": _StubProvider("twilio_sms"),
    }

    def _factory(name: str):
        def _build(_settings: Settings) -> BaseProvider:
            return stubs[name]

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
    registry.register_failover_provider("meta_whatsapp", _factory("meta_whatsapp"))
    registry.register_failover_provider("twilio_whatsapp", _factory("twilio_whatsapp"))
    registry.register_failover_provider("sms_aggregator", _factory("sms_aggregator"))
    registry.register_failover_provider("twilio_sms", _factory("twilio_sms"))
    return stubs


def _settings(**chains: list[str]) -> Settings:
    """Build a :class:`Settings` with a
    ``provider_failover_chains`` populated from ``chains``."""
    return Settings(provider_failover_chains=dict(chains))


# ---------------------------------------------------------------------------
# Pre-failover behaviour
# ---------------------------------------------------------------------------


def test_get_provider_returns_primary_when_chain_is_unset(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """When no chain is configured the registry must return
    the primary directly – the pre-failover behaviour, kept
    bit-for-bit compatible with earlier releases (no extra
    hop, no synthetic name in the ``Message.provider``
    column)."""
    settings = _settings()  # no chains
    provider = get_provider(Channel.WHATSAPP, settings=settings)
    assert provider is stub_providers["meta_whatsapp"]
    assert provider.name == "meta_whatsapp"


def test_get_provider_returns_primary_when_chain_is_empty(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """An empty list (``[]``) is the operator's way of saying
    "no fallback for this channel". The registry must honour
    it the same as a missing key – the primary is returned
    unchanged."""
    settings = _settings(whatsapp=[])
    provider = get_provider(Channel.WHATSAPP, settings=settings)
    assert provider is stub_providers["meta_whatsapp"]


# ---------------------------------------------------------------------------
# Failover chain construction
# ---------------------------------------------------------------------------


def test_get_provider_returns_failover_wrapper_when_chain_is_configured(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """A non-empty chain wraps the primary and the named
    fallbacks in a :class:`FailoverProvider` so a single
    ``send`` call transparently advances through the chain."""
    settings = _settings(whatsapp=["meta_whatsapp", "twilio_whatsapp"])
    provider = get_provider(Channel.WHATSAPP, settings=settings)
    assert isinstance(provider, FailoverProvider)
    assert provider.providers() == [
        stub_providers["meta_whatsapp"],
        stub_providers["twilio_whatsapp"],
    ]


def test_get_provider_primary_is_chain_first_element(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """The primary must be the *first* element of the chain –
    the failover contract is "primary first, then fallbacks".
    A swap would silently invert the routing policy."""
    settings = _settings(whatsapp=["meta_whatsapp", "twilio_whatsapp"])
    provider = get_provider(Channel.WHATSAPP, settings=settings)
    assert isinstance(provider, FailoverProvider)
    assert provider.primary is stub_providers["meta_whatsapp"]
    assert provider.fallbacks == [stub_providers["twilio_whatsapp"]]


def test_get_provider_chain_name_includes_every_member(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """The synthetic chain name lists every provider so an
    operator reading a log line or a ``Message.provider``
    column can tell a chain is in effect without re-deriving
    it from the configuration."""
    settings = _settings(whatsapp=["meta_whatsapp", "twilio_whatsapp"])
    provider = get_provider(Channel.WHATSAPP, settings=settings)
    assert provider.name == "meta_whatsapp+twilio_whatsapp"


def test_get_provider_drops_duplicate_primary_in_chain(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """A duplicate of the primary in the operator's chain is
    a no-op (it does not add availability). The registry
    drops the duplicate rather than erroring out, which
    mirrors the chain validator in :class:`FailoverProvider`
    – rejecting the whole config would block the platform on
    a typo."""
    settings = _settings(whatsapp=["meta_whatsapp", "twilio_whatsapp", "meta_whatsapp"])
    provider = get_provider(Channel.WHATSAPP, settings=settings)
    assert isinstance(provider, FailoverProvider)
    assert provider.providers() == [
        stub_providers["meta_whatsapp"],
        stub_providers["twilio_whatsapp"],
    ]


def test_get_provider_raises_for_unknown_provider_name(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """A chain entry that the registry does not know is a
    configuration error; surfacing it at boot (the
    :class:`Settings` validator) is preferred, but the
    registry still raises a typed error if it is asked
    directly."""
    settings = _settings(whatsapp=["meta_whatsapp", "ghost_provider"])
    with pytest.raises(UnsupportedProviderError) as exc_info:
        get_provider(Channel.WHATSAPP, settings=settings)
    assert exc_info.value.name == "ghost_provider"


# ---------------------------------------------------------------------------
# SMS channel
# ---------------------------------------------------------------------------


def test_get_provider_supports_failover_for_sms(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """The failover mechanism is channel-agnostic: the SMS
    aggregator can chain to a Twilio SMS fallback the same
    way the WhatsApp primary chains to a Twilio WhatsApp
    fallback. The test pins the cross-channel generality so a
    future refactor does not hard-code the channel list."""
    settings = _settings(sms=["sms_aggregator", "twilio_sms"])
    provider = get_provider(Channel.SMS, settings=settings)
    assert isinstance(provider, FailoverProvider)
    assert provider.providers() == [
        stub_providers["sms_aggregator"],
        stub_providers["twilio_sms"],
    ]


# ---------------------------------------------------------------------------
# Settings – JSON parsing
# ---------------------------------------------------------------------------


def test_settings_parses_failover_chains_from_json_string() -> None:
    """The :class:`Settings` field-validator accepts the
    JSON form so deployments can ship the chain through a
    single environment variable."""
    settings = Settings(
        provider_failover_chains='{"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]}',
    )
    assert settings.provider_failover_chains == {
        "whatsapp": ["meta_whatsapp", "twilio_whatsapp"],
    }


def test_settings_rejects_malformed_json() -> None:
    """A typo in the chain value is a hard configuration
    error: failing fast at boot is better than silently
    disabling failover at runtime."""
    with pytest.raises(ValueError, match="PROVIDER_FAILOVER_CHAINS"):
        Settings(provider_failover_chains="not a json")


def test_settings_rejects_non_object_json() -> None:
    """A JSON array (or scalar) is not a valid chain map; the
    validator rejects it so the operator notices the typo
    rather than discovering a missing failover in
    production."""
    with pytest.raises(ValueError, match="PROVIDER_FAILOVER_CHAINS"):
        Settings(provider_failover_chains='["meta_whatsapp"]')


def test_settings_empty_string_normalises_to_empty_dict() -> None:
    """An unset env var is the operator's way of saying "no
    failover". The validator normalises it to an empty dict
    so the rest of the platform does not have to special-case
    ``None`` vs ``""``."""
    settings = Settings(provider_failover_chains="")
    assert settings.provider_failover_chains == {}


def test_settings_default_is_empty_dict() -> None:
    """The default keeps the pre-failover behaviour: an
    empty chain means every channel maps to a single
    provider."""
    settings = Settings()
    assert settings.provider_failover_chains == {}


# ---------------------------------------------------------------------------
# Channel parsing
# ---------------------------------------------------------------------------


def test_get_provider_accepts_channel_as_string(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """Defence-in-depth: even though the route layer
    validates ``channel`` before reaching the registry, the
    registry also accepts the plain-string form so a
    malformed request never raises a ``KeyError`` (the
    exception layer would have to map ``KeyError`` → 422
    which is brittle)."""
    settings = _settings()
    provider = get_provider("whatsapp", settings=settings)
    assert provider is stub_providers["meta_whatsapp"]


def test_get_provider_rejects_unknown_channel_string(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """A channel the platform does not know is a 422 in the
    route layer; the registry surfaces a typed
    :class:`UnsupportedChannelError` for the mapper."""
    from app.adapters.registry import UnsupportedChannelError

    settings = _settings()
    with pytest.raises(UnsupportedChannelError):
        get_provider("telegram", settings=settings)


# ---------------------------------------------------------------------------
# Kill-switch (issue #11)
# ---------------------------------------------------------------------------
#
# The ``inactive`` set on :func:`get_provider` is the
# operator's manual disable. The tests below pin the
# contract between the dashboard ("desactivar" button) and
# the routing layer: the chain the registry returns must
# not include a disabled provider, and the synthetic
# chain name must reflect the surviving members only.


def test_get_provider_skips_inactive_primary(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """A chain whose primary is in the kill-switch set
    is re-headed: the next active member becomes the
    new primary. The result is a single-provider
    chain (no :class:`FailoverProvider` wrapper) so
    the call site does not pay the failover overhead
    for a chain with one survivor."""
    settings = _settings(whatsapp=["meta_whatsapp", "twilio_whatsapp"])
    provider = get_provider(
        Channel.WHATSAPP,
        settings=settings,
        inactive={"meta_whatsapp"},
    )
    # Single survivor → registry unwraps the chain.
    assert provider is stub_providers["twilio_whatsapp"]
    assert provider.name == "twilio_whatsapp"


def test_get_provider_filters_inactive_fallback(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """A chain whose *fallback* is disabled is returned
    as a single provider (the surviving primary). The
    registry unwraps the chain when only one member
    survives so the call site does not pay the
    failover overhead for a chain of one."""
    settings = _settings(whatsapp=["meta_whatsapp", "twilio_whatsapp"])
    provider = get_provider(
        Channel.WHATSAPP,
        settings=settings,
        inactive={"twilio_whatsapp"},
    )
    # Single survivor → registry unwraps the chain.
    assert provider is stub_providers["meta_whatsapp"]
    assert provider.name == "meta_whatsapp"


def test_get_provider_filters_multiple_inactive(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """Several inactive names at once: the registry
    filters *all* of them, preserving the order. A
    future iteration that wants to skip a chain in
    bulk (e.g. "disable the WhatsApp fleet for
    maintenance") should be a single call to
    :func:`get_provider` with a pre-populated set."""
    from app.adapters.registry import AllProvidersDisabledError

    settings = _settings(
        whatsapp=["meta_whatsapp", "twilio_whatsapp"],
        sms=["sms_aggregator", "twilio_sms"],
    )
    # All WhatsApp providers disabled: the registry
    # must surface :class:`AllProvidersDisabledError`
    # so the route layer can render a 503. The SMS
    # chain is unaffected (separate channel).
    with pytest.raises(AllProvidersDisabledError):
        get_provider(
            Channel.WHATSAPP,
            settings=settings,
            inactive={"meta_whatsapp", "twilio_whatsapp"},
        )


def test_get_provider_raises_when_every_provider_disabled(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """Every member in the kill-switch set: the
    registry raises :class:`AllProvidersDisabledError`
    (a :class:`ProviderError` subclass) so the route
    layer can surface a 503 with a stable error code
    rather than silently returning an empty chain."""
    from app.adapters.registry import AllProvidersDisabledError

    settings = _settings(whatsapp=["meta_whatsapp", "twilio_whatsapp"])
    with pytest.raises(AllProvidersDisabledError) as exc_info:
        get_provider(
            Channel.WHATSAPP,
            settings=settings,
            inactive={"meta_whatsapp", "twilio_whatsapp"},
        )
    assert exc_info.value.channel == Channel.WHATSAPP
    assert exc_info.value.http_status == 503
    assert exc_info.value.code == "provider_disabled"


def test_get_provider_raises_when_single_provider_disabled(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """A single-provider channel (no chain configured)
    whose only provider is disabled is the same
    edge case: :class:`AllProvidersDisabledError` so
    the route layer can render a 503 with the same
    error code the chain path uses."""
    from app.adapters.registry import AllProvidersDisabledError

    settings = _settings()  # no chains
    with pytest.raises(AllProvidersDisabledError) as exc_info:
        get_provider(
            Channel.WHATSAPP,
            settings=settings,
            inactive={"meta_whatsapp"},
        )
    assert exc_info.value.channel == Channel.WHATSAPP


def test_get_provider_empty_inactive_set_keeps_pre_kill_switch_behaviour(
    stub_providers: dict[str, _StubProvider],
) -> None:
    """``inactive=None`` and ``inactive=set()`` both
    keep the pre-kill-switch behaviour bit-for-bit.
    A deployment that has not opted into the
    kill-switch (the common case for the MVP) does
    not have to change its call site."""
    settings = _settings(whatsapp=["meta_whatsapp", "twilio_whatsapp"])
    chain_none = get_provider(Channel.WHATSAPP, settings=settings, inactive=None)
    chain_empty = get_provider(
        Channel.WHATSAPP, settings=settings, inactive=set()
    )
    assert isinstance(chain_none, FailoverProvider)
    assert isinstance(chain_empty, FailoverProvider)
    assert chain_none.providers() == chain_empty.providers()
