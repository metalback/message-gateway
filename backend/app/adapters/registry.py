"""Provider registry.

The platform integrates with multiple SMS / WhatsApp providers;
each implementation lives behind the :class:`BaseProvider`
interface declared in :mod:`app.adapters.base`. This module
exposes a small registry the rest of the codebase uses to look
up the right adapter for a given channel.

The registry is deliberately tiny – a plain module-level
mapping – because the MVP has a fixed set of providers and
shipping a plugin loader would be over-engineering. A new
provider is a three-step change:

1. Implement :class:`BaseProvider` in a new module.
2. Register a factory in :data:`_BUILDERS` below (keyed by
   the provider's :attr:`BaseProvider.name`).
3. Reference it from configuration (``Settings``).

Two levels of routing live in this module:

- A *channel* map (:data:`_BUILDERS`) names the *primary*
  provider for each channel – this is the pre-failover
  behaviour the MVP shipped with and the default the platform
  reverts to when no failover chain is configured.
- A *failover* map (:data:`_FAILOVER_BUILDERS`) names any
  extra providers that can be used as fallbacks. The
  :class:`~app.adapters.failover.FailoverProvider` stitches
  them together at :func:`get_provider` time.

The mapping for primaries is keyed by
:class:`~app.models.message.Channel` because the routing
decision is "given a channel, which provider owns it?".
Adding a new channel is therefore a breaking change that
requires an Alembic migration anyway, so the registry does
not need to support dynamic registration.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from app.adapters.base import BaseProvider
from app.adapters.errors import ProviderError
from app.adapters.failover import FailoverProvider
from app.models.message import Channel

if TYPE_CHECKING:
    from app.config import Settings


# ---------------------------------------------------------------------------
# Provider factories
# ---------------------------------------------------------------------------
#
# A factory receives the runtime :class:`Settings` and returns a
# fully-configured adapter. The factories are kept module-private
# so the rest of the codebase only ever calls :func:`get_provider`
# – a public surface that wraps the failover logic without
# exposing it to every call site.


def _build_meta_whatsapp(settings: Settings) -> BaseProvider:
    """Instantiate the Meta Cloud API provider from ``settings``."""
    from app.adapters.meta_whatsapp import MetaWhatsAppProvider

    return MetaWhatsAppProvider(
        access_token=settings.meta_whatsapp_access_token,
        phone_number_id=settings.meta_whatsapp_phone_number_id,
        api_base=settings.meta_whatsapp_api_base,
        api_version=settings.meta_whatsapp_api_version,
        timeout=settings.provider_timeout_seconds,
    )


def _build_sms_aggregator(settings: Settings) -> BaseProvider:
    """Instantiate the SMS aggregator provider from ``settings``."""
    from app.adapters.sms_aggregator import SmsAggregatorProvider

    return SmsAggregatorProvider(
        api_url=settings.sms_aggregator_api_url,
        api_key=settings.sms_aggregator_api_key,
        sender_id=settings.sms_aggregator_sender_id,
        timeout=settings.provider_timeout_seconds,
    )


# Channel → primary-factory mapping. The keys must match the
# values of :class:`app.models.message.Channel` exactly. A typo
# would surface as a ``KeyError`` at the first send – acceptable
# failure mode for a configuration mistake.
_BUILDERS: dict[Channel, Callable[[Settings], BaseProvider]] = {
    Channel.WHATSAPP: _build_meta_whatsapp,
    Channel.SMS: _build_sms_aggregator,
}


# Fallback-builder map keyed by ``BaseProvider.name``. The chain
# resolver in :func:`_resolve_provider` uses this to look up the
# factory for every name that appears in
# ``Settings.provider_failover_chains``. An empty map means the
# platform only knows the two primaries above – the MVP's pre-
# failover surface area. A new fallback provider is added by
# registering a factory here (or via
# :func:`register_failover_provider` from a test).
_FAILOVER_BUILDERS: dict[str, Callable[[Settings], BaseProvider]] = {
    "meta_whatsapp": _build_meta_whatsapp,
    "sms_aggregator": _build_sms_aggregator,
}


def register_failover_provider(
    name: str,
    factory: Callable[[Settings], BaseProvider],
) -> None:
    """Register an additional fallback provider by name.

    Public escape hatch used by tests (and by future "drop a new
    Twilio adapter" tasks) to plug a new provider into the
    failover chain without editing the registry. The MVP ships
    only the two primaries above, so the helper is a no-op for
    production callers – but keeping it on the module means
    tests can extend the chain without monkey-patching
    module-level globals.
    """
    _FAILOVER_BUILDERS[name] = factory


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_provider(
    channel: Channel | str,
    *,
    settings: Settings,
    inactive: Iterable[str] | None = None,
) -> BaseProvider:
    """Return the provider that owns ``channel``.

    The function honours the failover configuration in
    ``settings.provider_failover_chains``: when a chain is
    configured for the channel the call returns a
    :class:`~app.adapters.failover.FailoverProvider` that wraps
    the named providers in order; otherwise it returns the
    primary directly (the pre-failover behaviour, kept
    bit-for-bit compatible with earlier releases).

    ``channel`` may be passed as a :class:`Channel` enum member
    (the production path) or as a plain string (useful for
    defence-in-depth: a malformed request never reaches the
    registry in the first place, but if it does we want a
    clear error rather than a ``KeyError``).

    ``inactive`` is the kill-switch set (issue #11) – a
    collection of provider names the operator has flipped
    off on the admin dashboard. The registry filters them
    out of the chain *before* constructing the
    :class:`FailoverProvider`, so the resulting wrapper only
    contains providers that are eligible to receive traffic.
    A primary that the operator disabled is dropped from the
    chain head; the next active provider becomes the new
    primary. ``None`` (or an empty iterable) keeps the
    pre-kill-switch behaviour bit-for-bit.

    Raises :class:`UnsupportedChannelError` for channels the
    platform does not know about, and
    :class:`AllProvidersDisabledError` when every provider in
    the chain is in the kill-switch set. The route layer
    maps the channel error to a 422 and the all-disabled
    error to a 503 (the platform is healthy but cannot
    fulfil the request through the configured providers).
    """
    if isinstance(channel, str):
        try:
            channel = Channel(channel)
        except ValueError as exc:
            raise UnsupportedChannelError(
                f"channel {channel!r} is not supported by any provider",
                channel=channel,  # type: ignore[arg-type]
            ) from exc
    try:
        primary_factory = _BUILDERS[channel]
    except KeyError as exc:
        raise UnsupportedChannelError(
            f"channel {channel!r} is not supported by any provider",
            channel=channel,
        ) from exc

    inactive_set: set[str] = set(inactive or ())
    chain_setting = settings.provider_failover_chains.get(channel.value)
    primary = primary_factory(settings)
    if not chain_setting:
        # No chain configured: the kill-switch either keeps
        # the primary in place or returns nothing. The single-
        # provider path is the common case for a deployment
        # that has not opted into failover.
        if primary.name in inactive_set:
            raise AllProvidersDisabledError(
                f"channel {channel.value!r} has no active provider "
                f"({primary.name!r} is disabled)",
                channel=channel,
            )
        return primary
    # The primary must be the chain's first element – the
    # failover contract is "primary first, then fallbacks". We
    # drop a duplicate from the operator's list rather than
    # erroring out, which mirrors the chain validator in
    # :class:`FailoverProvider` (a duplicate does not add
    # availability, but rejecting the whole config would block
    # the platform on a typo).
    primary_name = primary.name
    chain_providers: list[BaseProvider] = [primary]
    for name in chain_setting:
        if name == primary_name:
            continue
        try:
            factory = _FAILOVER_BUILDERS[name]
        except KeyError as exc:
            raise UnsupportedProviderError(
                f"provider {name!r} is not registered for failover routing",
                name=name,
            ) from exc
        chain_providers.append(factory(settings))
    # Apply the kill-switch: drop every provider the
    # operator disabled, then drop the prepended primary if
    # it is also disabled. The list is *rebuilt* in order
    # (a surviving fallback may need to take the head of
    # the chain).
    active_providers: list[BaseProvider] = [
        provider
        for provider in chain_providers
        if provider.name not in inactive_set
    ]
    if not active_providers:
        raise AllProvidersDisabledError(
            f"channel {channel.value!r} has no active provider "
            f"(disabled names: {sorted(inactive_set & {p.name for p in chain_providers})!r})",
            channel=channel,
        )
    if len(active_providers) == 1:
        return active_providers[0]
    return FailoverProvider(active_providers)


def supported_channels() -> list[Channel]:
    """Return the channels the registry knows how to route.

    Exposed for the OpenAPI schema (so the docs can document
    the legal ``channel`` values) and for tests that want to
    iterate over the registry without hard-coding the enum.
    """
    return list(_BUILDERS.keys())


class UnsupportedChannelError(ValueError):
    """Raised when the platform has no provider for the given channel.

    The exception is a :class:`ValueError` (rather than a
    provider-specific exception) because the failure is a
    configuration error, not an upstream outage. The route
    layer maps it to a 422 response.
    """

    def __init__(self, message: str, *, channel: Channel) -> None:
        super().__init__(message)
        self.channel = channel


class UnsupportedProviderError(ValueError):
    """Raised when a failover chain references an unknown provider.

    A :class:`ValueError` (configuration error) rather than a
    provider-specific exception, mirroring
    :class:`UnsupportedChannelError`. The route layer does not
    surface this directly: the validator in
    :class:`app.config.Settings` rejects a malformed chain at
    boot, so the only path that reaches this exception is a
    test that exercises the registry in isolation.
    """

    def __init__(self, message: str, *, name: str) -> None:
        super().__init__(message)
        self.name = name


class AllProvidersDisabledError(ProviderError):
    """Raised when every provider in a channel is disabled.

    Subclasses :class:`ProviderError` so the route layer's
    existing ``_raise_provider_error`` helper maps the
    failure to a stable HTTP response (503 Service
    Unavailable, ``code="provider_disabled"``) without a
    new exception branch. The exception carries the
    channel so the operator's logs can be filtered to
    "all providers disabled for X" without having to
    re-parse the message string.

    The 503 (rather than 502 like the other
    :class:`ProviderError` subclasses) is intentional:
    the upstream providers may well be healthy – the
    platform itself is the one refusing to dispatch,
    because the operator has flipped every kill-switch
    off. 503 is the closest match in the HTTP vocabulary
    ("I cannot fulfil the request right now") without
    being a 5xx from the upstream.
    """

    http_status = 503
    code = "provider_disabled"

    def __init__(self, message: str, *, channel: Channel) -> None:
        super().__init__(message, provider=None)
        self.channel = channel


__all__ = (
    "AllProvidersDisabledError",
    "UnsupportedChannelError",
    "UnsupportedProviderError",
    "get_provider",
    "register_failover_provider",
    "supported_channels",
)
