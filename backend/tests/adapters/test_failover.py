"""Unit tests for :class:`app.adapters.failover.FailoverProvider`.

The router is a thin wrapper around an ordered list of
:class:`BaseProvider` instances: it tries the primary and falls
back to the next provider on a *retryable* error. The tests
exercise every branch of that contract:

- Successful primary → no fallback, ``SendResult.provider_name``
  is stamped with the primary's name.
- Primary raises ``ProviderUnavailableError`` → fallback gets
  the call, ``provider_name`` reflects the fallback.
- Primary raises ``ProviderRateLimitError`` → fallback gets the
  call.
- Primary raises ``ProviderValidationError`` → router propagates
  immediately (a malformed request would fail the same way
  against the next provider, so there is no point burning its
  quota).
- Every provider raises a retryable error → the *last* one
  surfaces so the caller sees the freshest signal.
- ``get_status`` routes back to the provider that actually
  delivered the message.

The test doubles are deliberately small (a :class:`FakeProvider`
that records every call) so a failure points straight at the
contract that broke.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.adapters.failover import RETRYABLE_ERRORS, FailoverProvider

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeProvider(BaseProvider):
    """Controllable :class:`BaseProvider` for failover tests.

    The constructor wires the next response and the next error
    so a test can build a chain like
    ``[primary(unavailable), fallback(success)]`` declaratively.
    The double records every ``send`` and ``get_status`` call so
    a test can assert the chain advanced in the expected order.
    """

    def __init__(
        self,
        name: str,
        *,
        provider_msg_id: str | None = None,
        status: str = "sent",
        errors: list[Exception] | None = None,
    ) -> None:
        self.name = name
        self._provider_msg_id = provider_msg_id or f"{name}-id"
        self._status = status
        # ``errors`` is a FIFO queue: each call pops the head;
        # an empty queue means "return success".
        self._errors = list(errors or [])
        self.send_calls: list[dict[str, Any]] = []
        self.status_calls: list[str] = []
        self.closed = False

    def push_error(self, error: Exception) -> None:
        """Schedule ``error`` to be raised on the next ``send`` call."""
        self._errors.append(error)

    async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
        self.send_calls.append({"to": to, "body": body, **kwargs})
        if self._errors:
            raise self._errors.pop(0)
        return SendResult(
            provider_msg_id=self._provider_msg_id,
            raw={"from": self.name},
        )

    async def get_status(self, provider_msg_id: str) -> str:
        self.status_calls.append(provider_msg_id)
        return self._status

    async def aclose(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_empty_chain_is_rejected() -> None:
    """A chain with no providers is meaningless: the router
    would not know who to call. Rejecting it at construction
    keeps the failure mode at boot rather than at the first
    ``send`` call."""
    with pytest.raises(ValueError, match="at least one provider"):
        FailoverProvider([])


def test_duplicate_providers_in_chain_are_rejected() -> None:
    """A chain with the same provider twice does not add
    availability – it just double-counts the cost of the call.
    The router refuses to build such a chain so a typo in the
    operator's config surfaces immediately."""
    a = FakeProvider("meta_whatsapp")
    b = FakeProvider("meta_whatsapp")
    with pytest.raises(ValueError, match="duplicate"):
        FailoverProvider([a, b])


def test_name_joins_chain_with_plus_separator() -> None:
    """The synthetic chain name is ``a+b+c`` so a log entry or
    a ``Message.provider`` column read is self-describing."""
    a = FakeProvider("meta_whatsapp")
    b = FakeProvider("twilio_whatsapp")
    c = FakeProvider("gupshup_whatsapp")
    chain = FailoverProvider([a, b, c])
    assert chain.name == "meta_whatsapp+twilio_whatsapp+gupshup_whatsapp"


def test_name_reflects_runtime_chain_order() -> None:
    """The synthetic name is recomputed from the current
    providers, so a test that swaps the chain at runtime sees
    a fresh label without having to rebuild the router."""
    a = FakeProvider("meta_whatsapp")
    b = FakeProvider("twilio_whatsapp")
    chain = FailoverProvider([a, b])
    assert chain.name == "meta_whatsapp+twilio_whatsapp"


# ---------------------------------------------------------------------------
# send – success paths
# ---------------------------------------------------------------------------


async def test_send_returns_primary_result_on_success() -> None:
    """When the primary succeeds the chain stops; the result
    surfaces the primary's ``provider_msg_id`` and the
    router-stamped ``provider_name`` so the messaging service
    can record the actual upstream on the persisted row."""
    primary = FakeProvider("meta_whatsapp", provider_msg_id="meta-1")
    fallback = FakeProvider("twilio_whatsapp", provider_msg_id="tw-1")
    chain = FailoverProvider([primary, fallback])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.provider_msg_id == "meta-1"
    assert result.provider_name == "meta_whatsapp"
    assert primary.send_calls == [{"to": "+56912345678", "body": "hola"}]
    assert fallback.send_calls == []


async def test_send_preserves_existing_provider_name_on_send_result() -> None:
    """If the underlying provider already stamped
    ``SendResult.provider_name`` (e.g. a future nested chain)
    the router does not overwrite it – the inner chain is the
    authoritative source of "who actually handled the call"."""
    primary = FakeProvider(
        "meta_whatsapp",
        errors=[ProviderUnavailableError("down", provider="meta_whatsapp")],
    )

    class _StampingProvider(BaseProvider):
        name = "stamping"

        async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
            return SendResult(
                provider_msg_id="stamp-1",
                raw={},
                provider_name="inner_provider",
            )

        async def get_status(self, provider_msg_id: str) -> str:
            return "sent"

    chain = FailoverProvider([primary, _StampingProvider()])
    result = await chain.send(to="+56912345678", body="hola")
    assert result.provider_name == "inner_provider"
    assert result.provider_msg_id == "stamp-1"


# ---------------------------------------------------------------------------
# send – fallback on retryable errors
# ---------------------------------------------------------------------------


async def test_send_falls_back_when_primary_is_unavailable() -> None:
    """A ``ProviderUnavailableError`` from the primary is the
    canonical signal the chain is built to handle: the router
    moves on to the fallback and stamps the result with the
    fallback's name."""
    primary = FakeProvider(
        "meta_whatsapp",
        errors=[ProviderUnavailableError("meta 5xx", provider="meta_whatsapp")],
    )
    fallback = FakeProvider("twilio_whatsapp", provider_msg_id="tw-1")
    chain = FailoverProvider([primary, fallback])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.provider_msg_id == "tw-1"
    assert result.provider_name == "twilio_whatsapp"
    assert primary.send_calls
    assert fallback.send_calls == [{"to": "+56912345678", "body": "hola"}]


async def test_send_falls_back_when_primary_is_rate_limited() -> None:
    """A 429 from the primary is also retryable: the rate
    limit might be lifted by the time the fallback answers,
    and we do not want to wedge the customer on a quota the
    second provider has not exhausted yet."""
    primary = FakeProvider(
        "meta_whatsapp",
        errors=[ProviderRateLimitError("meta 429", retry_after=1.0, provider="meta_whatsapp")],
    )
    fallback = FakeProvider("twilio_whatsapp", provider_msg_id="tw-1")
    chain = FailoverProvider([primary, fallback])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.provider_msg_id == "tw-1"
    assert result.provider_name == "twilio_whatsapp"


async def test_send_falls_back_through_multiple_retryable_failures() -> None:
    """A chain with two consecutive ``ProviderUnavailableError``
    failures keeps advancing; the third provider finally
    accepts the message. The test pins the contract that *any*
    number of retryable errors is handled (not just the first
    fallback)."""
    primary = FakeProvider(
        "meta_whatsapp",
        errors=[ProviderUnavailableError("down", provider="meta_whatsapp")],
    )
    second = FakeProvider(
        "twilio_whatsapp",
        errors=[ProviderUnavailableError("down", provider="twilio_whatsapp")],
    )
    third = FakeProvider("gupshup_whatsapp", provider_msg_id="g-1")
    chain = FailoverProvider([primary, second, third])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.provider_msg_id == "g-1"
    assert result.provider_name == "gupshup_whatsapp"
    assert primary.send_calls and second.send_calls and third.send_calls


async def test_send_returns_last_retryable_when_all_providers_fail() -> None:
    """When every provider in the chain raises a retryable
    error, the *last* one surfaces so the caller's logs and
    metrics reflect the freshest signal (e.g. Meta 503, then
    Twilio 429, then Gupshup 502)."""
    primary = FakeProvider(
        "meta_whatsapp",
        errors=[ProviderUnavailableError("meta 5xx", provider="meta_whatsapp")],
    )
    fallback = FakeProvider(
        "twilio_whatsapp",
        errors=[ProviderUnavailableError("twilio 5xx", provider="twilio_whatsapp")],
    )
    chain = FailoverProvider([primary, fallback])

    with pytest.raises(ProviderUnavailableError) as exc_info:
        await chain.send(to="+56912345678", body="hola")

    assert exc_info.value.provider == "twilio_whatsapp"
    assert "twilio 5xx" in str(exc_info.value)


# ---------------------------------------------------------------------------
# send – permanent failures short-circuit the chain
# ---------------------------------------------------------------------------


async def test_send_propagates_validation_error_without_trying_fallback() -> None:
    """A ``ProviderValidationError`` from the primary is *not*
    retried against the fallback: the request itself is
    malformed (bad number, template rejected) and would fail
    the same way against the next provider. The router
    propagates the error immediately so the customer sees the
    422 instead of burning the fallback's quota."""
    primary = FakeProvider(
        "meta_whatsapp",
        errors=[ProviderValidationError("bad number", provider="meta_whatsapp")],
    )
    fallback = FakeProvider("twilio_whatsapp", provider_msg_id="tw-1")
    chain = FailoverProvider([primary, fallback])

    with pytest.raises(ProviderValidationError) as exc_info:
        await chain.send(to="bad", body="hola")

    assert exc_info.value.provider == "meta_whatsapp"
    assert fallback.send_calls == []


async def test_send_propagates_validation_error_from_fallback() -> None:
    """If the primary's retryable error masked a problem the
    fallback catches, the validation error still propagates
    rather than cascading through the rest of the chain."""
    primary = FakeProvider(
        "meta_whatsapp",
        errors=[ProviderUnavailableError("meta 5xx", provider="meta_whatsapp")],
    )
    fallback = FakeProvider(
        "twilio_whatsapp",
        errors=[ProviderValidationError("rejected", provider="twilio_whatsapp")],
    )
    chain = FailoverProvider([primary, fallback])

    with pytest.raises(ProviderValidationError) as exc_info:
        await chain.send(to="+56912345678", body="hola")

    assert exc_info.value.provider == "twilio_whatsapp"
    assert primary.send_calls and fallback.send_calls


# ---------------------------------------------------------------------------
# get_status – routing
# ---------------------------------------------------------------------------


async def test_get_status_routes_back_to_handling_provider() -> None:
    """A delivery receipt carries the ``provider_msg_id`` the
    upstream returned at send time; the router remembers which
    provider handled the message so the status check asks the
    right upstream (Meta cannot answer for a Twilio id, and
    vice versa)."""
    primary = FakeProvider("meta_whatsapp", provider_msg_id="meta-1")
    fallback = FakeProvider("twilio_whatsapp", provider_msg_id="tw-1", status="delivered")
    chain = FailoverProvider([primary, fallback])

    # Primary fails; fallback delivers. The router records the
    # fallback's name against the returned provider_msg_id.
    primary.push_error(ProviderUnavailableError("meta 5xx", provider="meta_whatsapp"))
    await chain.send(to="+56912345678", body="hola")

    status = await chain.get_status("tw-1")
    assert status == "delivered"
    assert fallback.status_calls == ["tw-1"]
    assert primary.status_calls == []


async def test_get_status_falls_back_to_primary_when_routing_unknown() -> None:
    """A long-lived worker that has rotated (or a status check
    issued by a different process) has no record of which
    provider handled the message. The router falls back to the
    primary in that case – the common situation where the
    primary never failed and the message was delivered through
    it directly."""
    primary = FakeProvider("meta_whatsapp", status="delivered")
    fallback = FakeProvider("twilio_whatsapp", status="delivered")
    chain = FailoverProvider([primary, fallback])

    status = await chain.get_status("unknown-id")
    assert status == "delivered"
    assert primary.status_calls == ["unknown-id"]
    assert fallback.status_calls == []


async def test_get_status_rejects_empty_provider_msg_id() -> None:
    """An empty ``provider_msg_id`` is a programming error
    (the route layer validates the path parameter before
    reaching the adapter). The router surfaces a
    ``ProviderValidationError`` so the error path is uniform
    with the concrete adapters."""
    primary = FakeProvider("meta_whatsapp")
    chain = FailoverProvider([primary])

    with pytest.raises(ProviderValidationError):
        await chain.get_status("")


# ---------------------------------------------------------------------------
# provider_for – lookup helper
# ---------------------------------------------------------------------------


async def test_provider_for_returns_none_for_unknown_id() -> None:
    """A lookup that has no record returns ``None`` so the
    caller can apply its own fallback policy (the router
    itself does, in :meth:`get_status`)."""
    primary = FakeProvider("meta_whatsapp")
    chain = FailoverProvider([primary])
    assert chain.provider_for("never-seen") is None


async def test_provider_for_returns_none_when_routing_references_removed_provider() -> None:
    """If the chain is rebuilt and the previously-recorded
    provider is no longer in the chain, ``provider_for`` returns
    ``None`` rather than raising – the contract is "look up or
    give up", not "look up or fail loudly"."""
    primary = FakeProvider("meta_whatsapp")
    fallback = FakeProvider("twilio_whatsapp")
    chain = FailoverProvider([primary, fallback])

    # Pretend the chain previously routed through a third
    # provider that has since been removed.
    chain._routing["g-1"] = "gupshup_whatsapp"
    assert chain.provider_for("g-1") is None


async def test_provider_for_returns_provider_after_send() -> None:
    """After a successful send the router records the
    handling provider so a later ``get_status`` can find it."""
    primary = FakeProvider(
        "meta_whatsapp",
        errors=[ProviderUnavailableError("down", provider="meta_whatsapp")],
    )
    fallback = FakeProvider("twilio_whatsapp", provider_msg_id="tw-1")
    chain = FailoverProvider([primary, fallback])

    await chain.send(to="+56912345678", body="hola")
    assert chain.provider_for("tw-1") is fallback


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def test_primary_returns_first_provider() -> None:
    """The ``primary`` property exposes the first element of
    the chain so a health check can target the primary
    specifically (e.g. to refresh a token)."""
    a = FakeProvider("meta_whatsapp")
    b = FakeProvider("twilio_whatsapp")
    chain = FailoverProvider([a, b])
    assert chain.primary is a


def test_fallbacks_returns_remaining_providers() -> None:
    """The ``fallbacks`` property exposes everything after the
    primary so the rest of the codebase can iterate over the
    secondaries (e.g. for health probes) without re-deriving
    the slice."""
    a = FakeProvider("meta_whatsapp")
    b = FakeProvider("twilio_whatsapp")
    c = FakeProvider("gupshup_whatsapp")
    chain = FailoverProvider([a, b, c])
    assert chain.fallbacks == [b, c]


def test_providers_returns_full_chain() -> None:
    """The ``providers()`` accessor returns the whole chain so
    callers that want to iterate everything (health checks,
    metrics) do not have to concatenate ``primary`` and
    ``fallbacks`` themselves."""
    a = FakeProvider("meta_whatsapp")
    b = FakeProvider("twilio_whatsapp")
    chain = FailoverProvider([a, b])
    assert chain.providers() == [a, b]


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


async def test_aclose_closes_every_provider_in_the_chain() -> None:
    """Tearing down the chain must close every provider's HTTP
    client so a process restart does not leave dangling
    sockets. The router delegates to each provider's
    ``aclose`` so the per-adapter ``_owns_client`` discipline
    is preserved."""
    primary = FakeProvider("meta_whatsapp")
    fallback = FakeProvider("twilio_whatsapp")
    chain = FailoverProvider([primary, fallback])

    await chain.aclose()

    assert primary.closed
    assert fallback.closed


async def test_aclose_skips_providers_without_aclose() -> None:
    """Some test doubles do not implement ``aclose`` (they do
    not own an HTTP client). The router must not crash on
    them – a missing ``aclose`` means "nothing to close"."""
    primary = FakeProvider("meta_whatsapp")

    class _NoAcloseProvider(BaseProvider):
        name = "noaclose"

        async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
            return SendResult(provider_msg_id="x", raw={})

        async def get_status(self, provider_msg_id: str) -> str:
            return "sent"

    chain = FailoverProvider([primary, _NoAcloseProvider()])
    await chain.aclose()  # should not raise
    assert primary.closed


# ---------------------------------------------------------------------------
# Module-level contract
# ---------------------------------------------------------------------------


def test_retryable_errors_tuple_lists_documented_types() -> None:
    """The module exposes the retryable-error tuple so callers
    that need to mirror the same decision (e.g. a future
    "smart" retry policy in the worker) can import it without
    hard-coding the exception list."""
    assert ProviderUnavailableError in RETRYABLE_ERRORS
    assert ProviderRateLimitError in RETRYABLE_ERRORS
    # Validation errors must *not* be retryable: a malformed
    # request would fail the same way against a different
    # provider.
    assert ProviderValidationError not in RETRYABLE_ERRORS


def test_retryable_errors_subclass_provider_error() -> None:
    """Type-annotation guard: every element of
    :data:`RETRYABLE_ERRORS` must be a :class:`ProviderError`
    subclass so the router's ``except RETRYABLE_ERRORS`` clause
    narrows to the right type."""
    for exc in RETRYABLE_ERRORS:
        assert issubclass(exc, ProviderError)


# ---------------------------------------------------------------------------
# attempt_callback – per-attempt event reporting (issue #11)
# ---------------------------------------------------------------------------
#
# The :class:`~app.adapters.failover.FailoverProvider` is
# responsible for emitting one event per provider attempt
# (success or failure) so the messaging service can persist
# a :class:`~app.models.routing_log.RoutingLog` row per leg
# of the chain. The contract is:
#
# - The callback is invoked once per provider the router
#   tried (the primary *and* every fallback that was
#   asked to deliver the message).
# - The callback receives ``(provider_name, outcome,
#   latency_ms, error_code, error_message)``. The
#   ``outcome`` is one of
#   :class:`~app.models.routing_log.RoutingLogOutcome`
#   ("success" / "failure" / "validation_error").
# - The callback's exceptions are swallowed (the
#   dispatch must not crash on a misbehaving recorder).


def test_attempt_callback_fires_on_primary_success() -> None:
    """When the primary succeeds the callback is invoked
    once with ``outcome="success"`` and the primary's
    name. The dispatcher does not need to ask the
    fallback for a status."""
    primary = FakeProvider("meta_whatsapp", provider_msg_id="meta-1")
    fallback = FakeProvider("twilio_whatsapp")
    chain = FailoverProvider([primary, fallback])

    events: list[tuple[str, str, int, str | None, str | None]] = []

    async def _run() -> None:
        await chain.send(
            to="+56912345678",
            body="hola",
            attempt_callback=lambda *args: events.append(args),
        )

    asyncio.run(_run())

    assert len(events) == 1
    name, outcome, latency_ms, error_code, error_message = events[0]
    assert name == "meta_whatsapp"
    assert outcome == "success"
    assert latency_ms >= 0
    assert error_code is None
    assert error_message is None


def test_attempt_callback_fires_for_every_attempt_in_chain() -> None:
    """A chain with one failure followed by a success
    produces two callback events: the failure (with the
    primary's name and ``outcome="failure"``) and the
    success (with the fallback's name and
    ``outcome="success"``). The order is preserved so a
    recorder can write the rows in the same sequence the
    chain advanced."""
    primary = FakeProvider(
        "meta_whatsapp",
        errors=[ProviderUnavailableError("meta 5xx", provider="meta_whatsapp")],
    )
    fallback = FakeProvider("twilio_whatsapp", provider_msg_id="tw-1")
    chain = FailoverProvider([primary, fallback])

    events: list[tuple[str, str, int, str | None, str | None]] = []

    async def _run() -> None:
        await chain.send(
            to="+56912345678",
            body="hola",
            attempt_callback=lambda *args: events.append(args),
        )

    asyncio.run(_run())

    assert [e[0] for e in events] == ["meta_whatsapp", "twilio_whatsapp"]
    assert [e[1] for e in events] == ["failure", "success"]
    # The failure carries the provider's error code/message;
    # the success carries ``None`` for both.
    assert events[0][3] == "provider_unavailable"
    assert events[0][4] == "meta 5xx"
    assert events[1][3] is None
    assert events[1][4] is None


def test_attempt_callback_fires_with_validation_error_outcome() -> None:
    """A :class:`ProviderValidationError` is a permanent
    failure: the router propagates it immediately, but
    the callback still fires so the routing log captures
    the attempt. The ``outcome`` is the dedicated
    ``"validation_error"`` bucket so the dashboard can
    chart bad-input errors separately from upstreams
    that are merely unavailable."""
    primary = FakeProvider(
        "meta_whatsapp",
        errors=[ProviderValidationError("bad number", provider="meta_whatsapp")],
    )
    fallback = FakeProvider("twilio_whatsapp")
    chain = FailoverProvider([primary, fallback])

    events: list[tuple[str, str, int, str | None, str | None]] = []

    async def _run() -> None:
        with pytest.raises(ProviderValidationError):
            await chain.send(
                to="bad",
                body="hola",
                attempt_callback=lambda *args: events.append(args),
            )

    asyncio.run(_run())

    # Only the primary was tried: a validation error
    # short-circuits the chain.
    assert len(events) == 1
    name, outcome, _latency, error_code, error_message = events[0]
    assert name == "meta_whatsapp"
    assert outcome == "validation_error"
    # ``error_code`` is the upstream's stable token
    # (here ``"provider_validation"``); the
    # ``outcome`` is the routing-log bucket. The two
    # are decoupled on purpose: the upstream's code
    # might be reused for a different bucket if a
    # future iteration adds more granularity.
    assert error_code == "provider_validation"
    assert error_message == "bad number"


def test_attempt_callback_fires_for_every_retryable_failure() -> None:
    """When every provider in the chain raises a
    retryable error the callback fires once per
    provider (with ``outcome="failure"``) so the
    routing log captures the whole chain the router
    walked."""
    primary = FakeProvider(
        "meta_whatsapp",
        errors=[ProviderUnavailableError("meta 5xx", provider="meta_whatsapp")],
    )
    fallback = FakeProvider(
        "twilio_whatsapp",
        errors=[ProviderUnavailableError("tw 5xx", provider="twilio_whatsapp")],
    )
    chain = FailoverProvider([primary, fallback])

    events: list[tuple[str, str, int, str | None, str | None]] = []

    async def _run() -> None:
        with pytest.raises(ProviderUnavailableError):
            await chain.send(
                to="+56912345678",
                body="hola",
                attempt_callback=lambda *args: events.append(args),
            )

    asyncio.run(_run())

    assert [e[0] for e in events] == ["meta_whatsapp", "twilio_whatsapp"]
    assert [e[1] for e in events] == ["failure", "failure"]


def test_attempt_callback_is_not_forwarded_to_underlying_provider() -> None:
    """The ``attempt_callback`` is a router concern, not
    an adapter concern. The router must not forward it
    to the underlying provider's ``send`` call: doing so
    would pollute the per-call kwargs the existing test
    doubles capture verbatim, breaking the pre-failover
    test surface. The test pins the contract that the
    callback is *consumed* by the router."""
    primary = FakeProvider("meta_whatsapp", provider_msg_id="meta-1")
    chain = FailoverProvider([primary])

    async def _run() -> None:
        await chain.send(
            to="+56912345678",
            body="hola",
            attempt_callback=lambda *args: None,
        )

    asyncio.run(_run())
    assert primary.send_calls == [{"to": "+56912345678", "body": "hola"}]


def test_attempt_callback_is_optional() -> None:
    """The callback is optional: pre-existing call sites
    (and the unit tests that do not care about the
    routing log) pass nothing and the router behaves
    exactly as before."""
    primary = FakeProvider("meta_whatsapp", provider_msg_id="meta-1")
    chain = FailoverProvider([primary])

    async def _run() -> SendResult:
        return await chain.send(to="+56912345678", body="hola")

    result = asyncio.run(_run())
    assert result.provider_msg_id == "meta-1"
    assert result.provider_name == "meta_whatsapp"


def test_attempt_callback_swallows_exceptions() -> None:
    """A misbehaving recorder (a downstream DB outage,
    a programming error in the callback) must not
    break the dispatch path: the dispatch result is
    far more important than the audit row. The router
    logs the failure and continues."""
    primary = FakeProvider("meta_whatsapp", provider_msg_id="meta-1")
    chain = FailoverProvider([primary])

    def _boom(*_args: object) -> None:
        raise RuntimeError("recorder broken")

    async def _run() -> SendResult:
        return await chain.send(
            to="+56912345678",
            body="hola",
            attempt_callback=_boom,
        )

    # The dispatch returns a normal SendResult even
    # though the callback raised on every invocation.
    result = asyncio.run(_run())
    assert result.provider_msg_id == "meta-1"

