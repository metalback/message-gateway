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
2. Add a factory to :data:`_BUILDERS` below.
3. Reference it from configuration (``Settings``).

The mapping is keyed by :class:`~app.models.message.Channel`
because the routing decision is "given a channel, which
provider owns it?". Adding a new channel is therefore a
breaking change that requires an Alembic migration anyway, so
the registry does not need to support dynamic registration.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from app.adapters.base import BaseProvider
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
# – a public surface that the future "fallback" logic can extend
# without breaking every call site.


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


# Channel → factory mapping. The keys must match the values of
# :class:`app.models.message.Channel` exactly. A typo would
# surface as a ``KeyError`` at the first send – acceptable
# failure mode for a configuration mistake.
_BUILDERS: dict[Channel, Callable[[Settings], BaseProvider]] = {
    Channel.WHATSAPP: _build_meta_whatsapp,
    Channel.SMS: _build_sms_aggregator,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_provider(channel: Channel | str, *, settings: Settings) -> BaseProvider:
    """Return the provider that owns ``channel``.

    The function is intentionally simple: look the channel up in
    the registry, build the adapter from the runtime settings,
    return it. A more sophisticated implementation would honour
    fallback policies ("try Meta, fall back to a second
    WhatsApp provider"), but that lands in a follow-up task
    (see PRD follow-up list).

    ``channel`` may be passed as a :class:`Channel` enum member
    (the production path) or as a plain string (useful for
    defence-in-depth: a malformed request never reaches the
    registry in the first place, but if it does we want a
    clear error rather than a ``KeyError``).

    Raises :class:`UnsupportedChannelError` for channels the
    platform does not know about. The route layer maps the
    exception to a 422 response.
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
        builder = _BUILDERS[channel]
    except KeyError as exc:
        raise UnsupportedChannelError(
            f"channel {channel!r} is not supported by any provider",
            channel=channel,
        ) from exc
    return builder(settings)


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


__all__ = ("UnsupportedChannelError", "get_provider", "supported_channels")
