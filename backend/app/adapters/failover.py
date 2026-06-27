"""Failover provider router.

The platform wraps one or more :class:`~app.adapters.base.BaseProvider`
instances in a :class:`FailoverProvider` so a single ``send`` call
automatically falls back to the next provider in the chain when the
primary raises a *retryable* error.

The router is intentionally narrow: it implements the
:class:`BaseProvider` contract on top of an ordered list of providers
and lets the rest of the platform treat a "chain" exactly like a single
adapter.

Retryable vs. permanent failures
--------------------------------

- :class:`~app.adapters.errors.ProviderUnavailableError` and
  :class:`~app.adapters.errors.ProviderRateLimitError` are *retryable*:
  the next provider in the chain gets a chance to deliver the message.
- :class:`~app.adapters.errors.ProviderValidationError` is *permanent*:
  the request itself is malformed (bad number, template rejected, …) so
  retrying through a different upstream would fail the same way. The
  router propagates the validation error immediately.

The router is the only place that needs to know this distinction; the
concrete adapters keep raising the same exception types they always
have, and the messaging service keeps treating a failed dispatch as
``failed`` regardless of whether the failure came from the primary or a
fallback.

Why not just retry on the same provider?
----------------------------------------

PRD user story #19 asks for *fallback automatic entre proveedores*
("automatic failover between providers"). The goal is high
availability, not just a retry. Retrying the same Meta endpoint three
times in a row does not help if Meta is having a global outage; only
switching to a second upstream (e.g. Twilio WhatsApp, an international
SMS aggregator) does. The router exists to model that decision
explicitly.

Tracking which provider actually handled the call
--------------------------------------------------

The :class:`FailoverProvider` keeps a small ``provider_msg_id ->
provider_name`` map so a later :meth:`get_status` call can ask the
correct upstream for the delivery receipt. The map is also returned to
the caller via :attr:`SendResult.provider_name` so the messaging
service can record the actual provider on the persisted row (an
operator looking at "the message went to ``twilio_whatsapp`` instead
of ``meta_whatsapp``" can then tell that a failover happened).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)

#: Exception types that should trigger a fallback to the next provider.
#: Anything else (notably :class:`ProviderValidationError`) is treated
#: as a permanent failure and propagated without trying the next
#: upstream – the request itself is malformed and would fail the same
#: way against a different provider.
RETRYABLE_ERRORS: tuple[type[ProviderError], ...] = (
    ProviderUnavailableError,
    ProviderRateLimitError,
)


def _chain_name(providers: list[BaseProvider]) -> str:
    """Return a deterministic, human-readable name for a chain.

    The name joins the underlying providers with a ``+`` separator so a
    log entry or a ``Message.provider`` column read "``twilio_whatsapp``
    after ``meta_whatsapp`` went down" stays legible. Empty chains are
    rejected by the constructor so the helper does not have to handle
    that edge case.
    """
    return "+".join(provider.name for provider in providers)


class FailoverProvider(BaseProvider):
    """Adapter that routes a call to the first provider that succeeds.

    The constructor takes an ordered list of :class:`BaseProvider`
    instances; the first one is treated as the *primary* and the
    remaining ones as *fallbacks*. A :class:`ValueError` is raised if
    the list is empty or contains duplicates (a chain with the same
    provider twice would not provide extra availability – it would just
    double-count the cost of the call).

    The :attr:`name` attribute is a synthetic identifier the registry
    uses when the persistence layer needs a stable label for the
    routing decision (the label of the primary, the *whole* chain or a
    specific fallback; see :meth:`name`).
    """

    def __init__(self, providers: Iterable[BaseProvider]) -> None:
        providers_list = list(providers)
        if not providers_list:
            raise ValueError("FailoverProvider requires at least one provider")
        names = [provider.name for provider in providers_list]
        if len(set(names)) != len(names):
            raise ValueError(
                "FailoverProvider does not allow duplicate providers in a chain: "
                f"{names!r}"
            )
        self._providers = providers_list
        # Map ``provider_msg_id`` → name of the provider that actually
        # handled the call. Populated in :meth:`send` and consumed in
        # :meth:`get_status` so a later status check targets the
        # upstream that acknowledged the message. The map is bounded
        # by message volume and the process lifetime; a long-running
        # worker that forgets the mapping for an old message will fall
        # back to the primary (the common case for delivery receipts
        # that land well after the worker rotates).
        self._routing: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:  # type: ignore[override]
        """Synthetic chain name (``"primary+fallback"``).

        Exposed as a :class:`str` (rather than a plain class attribute)
        so the value is always derived from the current chain order.
        Tests that swap providers in and out of the chain therefore see
        a fresh label without having to re-instantiate the router.

        The base class declares ``name`` as a writeable class
        attribute; we override it with a read-only property because
        the chain's identity is a function of its runtime state, not
        a class-level constant. Mypy rightly flags the override
        (``Cannot override writeable attribute with read-only
        property``); the silence is intentional – the contract is
        still ``name: str``, just computed.
        """
        return _chain_name(self._providers)

    @property
    def primary(self) -> BaseProvider:
        """Return the first provider in the chain."""
        return self._providers[0]

    @property
    def fallbacks(self) -> list[BaseProvider]:
        """Return the providers after the primary, in order."""
        return list(self._providers[1:])

    def providers(self) -> list[BaseProvider]:
        """Return the full chain, primary first.

        Public counterpart to :attr:`primary` / :attr:`fallbacks` for
        callers that want to iterate the whole chain (e.g. a health
        check that pings every upstream).
        """
        return list(self._providers)

    def provider_for(self, provider_msg_id: str) -> BaseProvider | None:
        """Return the provider that handled ``provider_msg_id``.

        ``None`` is returned when the router has no record of the
        id – either the message was sent before the worker rotated or
        the id belongs to a different process. The caller is expected
        to fall back to :attr:`primary` in that case (the common
        situation: a delivery receipt that lands long after the
        message was dispatched).
        """
        provider_name = self._routing.get(provider_msg_id)
        if provider_name is None:
            return None
        for provider in self._providers:
            if provider.name == provider_name:
                return provider
        return None

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def send(self, *, to: str, body: str, **kwargs: object) -> SendResult:
        """Try the providers in order, falling back on retryable errors.

        Behaviour:

        - If the primary succeeds, return its :class:`SendResult` as-is.
        - If the primary raises a :class:`ProviderValidationError`,
          propagate it immediately: the request is malformed and would
          fail the same way against the next provider.
        - If the primary raises a *retryable* error
          (:class:`ProviderUnavailableError` or
          :class:`ProviderRateLimitError`), try the next provider in
          the chain.
        - If a fallback raises a validation error, propagate it
          (do not try the remaining fallbacks – the request is
          malformed).
        - If every provider in the chain raises a retryable error,
          raise the *last* one so the caller's logs and metrics reflect
          the most recent attempt.

        The first successful provider's name is recorded in
        ``self._routing`` and returned through
        :attr:`SendResult.provider_name` so the messaging service can
        record the actual provider on the persisted row.
        """
        last_retryable: ProviderError | None = None
        for provider in self._providers:
            try:
                result = await provider.send(to=to, body=body, **kwargs)
            except RETRYABLE_ERRORS as exc:
                # A retryable error from one provider is exactly the
                # signal the chain is built to handle – remember it
                # and continue to the next provider.
                last_retryable = exc
                continue
            except ProviderError:
                # A non-retryable provider error (validation, …) is
                # the caller's fault, not the upstream's. Re-raising
                # here means a "bad number" stays a 422 even when the
                # primary is down.
                raise
            # Success: pin the routing decision so a later
            # ``get_status`` call targets the right upstream, and
            # surface the provider's name through the SendResult so
            # the messaging service can record it.
            self._routing[result.provider_msg_id] = provider.name
            if result.provider_name is None:
                return SendResult(
                    provider_msg_id=result.provider_msg_id,
                    raw=result.raw,
                    provider_name=provider.name,
                )
            return result
        # Every provider raised a retryable error. The last one is the
        # most recent failure mode (e.g. Meta returned 503, then Twilio
        # returned 429); surface it so the caller sees the freshest
        # signal.
        assert last_retryable is not None  # guaranteed: chain is non-empty
        raise last_retryable

    async def get_status(self, provider_msg_id: str) -> str:
        """Return the status from the provider that handled the message.

        The router keeps a ``provider_msg_id`` → provider map so the
        status check goes back to the same upstream that accepted the
        message (Meta cannot answer for a Twilio id, and vice versa).
        When the map has no record (long-lived worker, cross-process
        routing), the call falls back to the primary – the common case
        where the primary never failed and the message was never
        routed through a fallback.

        An empty ``provider_msg_id`` is rejected as a validation error
        before any provider call is made (mirrors the contract of the
        concrete adapters, which all raise the same error in that
        case).
        """
        from app.adapters.errors import ProviderValidationError

        if not provider_msg_id:
            raise ProviderValidationError(
                "provider_msg_id is required",
                provider=self.name,
            )
        provider = self.provider_for(provider_msg_id)
        if provider is None:
            provider = self.primary
        return await provider.get_status(provider_msg_id)

    async def aclose(self) -> None:
        """Close the HTTP clients of every provider in the chain.

        Safe to call when an upstream provider shares its client with
        the rest of the platform: each underlying adapter is
        responsible for the ``_owns_client`` check and only tears down
        clients it created itself.
        """
        for provider in self._providers:
            close = getattr(provider, "aclose", None)
            if callable(close):
                await close()


__all__ = ("RETRYABLE_ERRORS", "FailoverProvider")
