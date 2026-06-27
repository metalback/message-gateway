"""Unit tests for :class:`app.adapters.failover.FailoverProvider` (issue #11).

The tests cover the failover router contract:

- A single-provider chain behaves like a plain
  :class:`BaseProvider` (no extra hop, no surprise).
- A retryable error from the primary (5xx, 429) makes the
  router try the next provider.
- A non-retryable error from the primary (validation / 4xx)
  short-circuits the chain: the request is malformed and a
  different upstream would fail the same way.
- A validation error from a *fallback* also short-circuits
  the rest of the chain – the next provider is not given a
  chance to fail the same way.
- When every provider raises a retryable error, the *last*
  one is surfaced (the freshest signal of the upstream's
  state).
- The :class:`SendResult` carries the *actual* provider name
  that handled the call (so the messaging service can record
  it on the persisted row).
- :meth:`get_status` uses the provider that originally
  accepted the message, not the chain's primary.
- The constructor refuses empty chains and chains with
  duplicate providers.

The HTTP layer is not exercised here: the router is a
pure-Python abstraction over :class:`BaseProvider`, so a stub
provider is the right level of test. The end-to-end
integration with the registry's real adapters lives in
:mod:`tests.adapters.test_failover_registry` and
:mod:`tests.services.test_messaging`.
"""

from __future__ import annotations

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


class _ScriptedProvider(BaseProvider):
    """A controllable :class:`BaseProvider` for failover tests.

    The constructor accepts a list of *outcomes* – the router will
    consume them in order:

    - A :class:`SendResult` is returned verbatim.
    - An :class:`Exception` instance is raised.

    The double also records every call so the test can assert on
    the routing decision the chain made.
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
        self.status_calls: list[str] = []

    async def send(self, *, to: str, body: str, **kwargs: object) -> SendResult:
        self.send_calls.append({"to": to, "body": body, **kwargs})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_status(self, provider_msg_id: str) -> str:
        self.status_calls.append(provider_msg_id)
        return f"status:{self.name}:{provider_msg_id}"


def _ok(*, name: str, provider_msg_id: str = "ok-1") -> _ScriptedProvider:
    """Build a provider that always returns a successful SendResult."""
    return _ScriptedProvider(
        name=name,
        outcomes=[SendResult(provider_msg_id=provider_msg_id, raw={"from": name})],
    )


def _fails_with(
    *, name: str, error: ProviderError
) -> _ScriptedProvider:
    """Build a provider that raises ``error`` on the first call."""
    return _ScriptedProvider(name=name, outcomes=[error])


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_failover_rejects_empty_chain() -> None:
    """An empty chain is a programming error caught at
    construction time; the router would not know which
    upstream to dispatch to."""
    with pytest.raises(ValueError):
        FailoverProvider([])


def test_failover_rejects_duplicate_providers() -> None:
    """A chain with the same provider twice does not improve
    availability and would double-count the cost of the call.
    The constructor rejects it so the misconfiguration
    surfaces at boot rather than at the first send."""
    primary = _ok(name="meta_whatsapp")
    with pytest.raises(ValueError):
        FailoverProvider([primary, _ok(name="meta_whatsapp")])


def test_failover_name_lists_chain_in_order() -> None:
    """The synthetic name joins the chain with ``+`` so logs
    and the ``Message.provider`` column make the failover
    topology obvious without re-querying the registry."""
    chain = FailoverProvider(
        [_ok(name="meta_whatsapp"), _ok(name="twilio_whatsapp")]
    )
    assert chain.name == "meta_whatsapp+twilio_whatsapp"


def test_failover_primary_and_fallbacks_accessors() -> None:
    """The :attr:`primary` and :attr:`fallbacks` accessors
    expose the chain order so an operator (or a health check)
    can introspect the configuration."""
    primary = _ok(name="meta_whatsapp")
    fallback_a = _ok(name="twilio_whatsapp")
    fallback_b = _ok(name="gupshup_whatsapp")
    chain = FailoverProvider([primary, fallback_a, fallback_b])

    assert chain.primary is primary
    assert chain.fallbacks == [fallback_a, fallback_b]
    assert chain.providers() == [primary, fallback_a, fallback_b]


def test_retryable_errors_constant_is_frozen() -> None:
    """The retryable-error tuple is a module-level contract:
    tests that care about which error types trigger failover
    can import it and assert on its identity rather than on
    the value. Guarding against accidental mutation keeps
    the contract stable across the rest of the suite."""
    assert ProviderUnavailableError in RETRYABLE_ERRORS
    assert ProviderRateLimitError in RETRYABLE_ERRORS
    # Validation is *not* retryable – a malformed message would
    # fail the same way against every upstream.
    assert ProviderValidationError not in RETRYABLE_ERRORS


# ---------------------------------------------------------------------------
# send() — primary-only / single-element chain
# ---------------------------------------------------------------------------


async def test_single_provider_chain_returns_result_unchanged() -> None:
    """A one-element chain is the no-op case: the router
    must return the underlying provider's result verbatim,
    including its ``provider_name``."""
    primary = _ScriptedProvider(
        name="meta_whatsapp",
        outcomes=[SendResult(provider_msg_id="wamid.1", raw={})],
    )
    chain = FailoverProvider([primary])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.provider_msg_id == "wamid.1"
    assert result.provider_name == "meta_whatsapp"
    assert primary.send_calls == [{"to": "+56912345678", "body": "hola"}]


async def test_primary_succeeds_and_fallback_is_never_called() -> None:
    """When the primary succeeds, the fallback must not see
    the call – a fallback invocation would burn the
    upstream's rate-limit budget for nothing."""
    primary = _ok(name="meta_whatsapp", provider_msg_id="wamid.1")
    fallback = _ok(name="twilio_whatsapp", provider_msg_id="twilio.1")
    chain = FailoverProvider([primary, fallback])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.provider_msg_id == "wamid.1"
    assert result.provider_name == "meta_whatsapp"
    assert primary.send_calls
    assert fallback.send_calls == []


# ---------------------------------------------------------------------------
# send() — fallback on retryable errors
# ---------------------------------------------------------------------------


async def test_5xx_from_primary_triggers_fallback() -> None:
    """A 5xx-class failure from the primary (rendered as a
    :class:`ProviderUnavailableError`) is a transient
    upstream outage; the router must try the next provider
    instead of letting the caller see the failure."""
    primary = _fails_with(
        name="meta_whatsapp",
        error=ProviderUnavailableError("meta 502", provider="meta_whatsapp"),
    )
    fallback = _ok(name="twilio_whatsapp", provider_msg_id="twilio.1")
    chain = FailoverProvider([primary, fallback])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.provider_msg_id == "twilio.1"
    assert result.provider_name == "twilio_whatsapp"
    assert primary.send_calls
    assert fallback.send_calls == [{"to": "+56912345678", "body": "hola"}]


async def test_429_from_primary_triggers_fallback() -> None:
    """A 429 (rate limit) on the primary does not mean the
    fallback is also throttled – the next provider has its
    own quota. The router must try it."""
    primary = _fails_with(
        name="meta_whatsapp",
        error=ProviderRateLimitError("meta 429", provider="meta_whatsapp"),
    )
    fallback = _ok(name="twilio_whatsapp", provider_msg_id="twilio.1")
    chain = FailoverProvider([primary, fallback])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.provider_msg_id == "twilio.1"
    assert result.provider_name == "twilio_whatsapp"


async def test_fallback_also_fails_with_retryable_then_uses_next() -> None:
    """When the first fallback *also* fails with a retryable
    error, the router continues down the chain. The third
    provider succeeds and the result must reflect that."""
    primary = _fails_with(
        name="meta_whatsapp",
        error=ProviderUnavailableError("meta down", provider="meta_whatsapp"),
    )
    first_fallback = _fails_with(
        name="twilio_whatsapp",
        error=ProviderRateLimitError("twilio 429", provider="twilio_whatsapp"),
    )
    last_resort = _ok(name="gupshup_whatsapp", provider_msg_id="gup.1")
    chain = FailoverProvider([primary, first_fallback, last_resort])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.provider_msg_id == "gup.1"
    assert result.provider_name == "gupshup_whatsapp"
    assert primary.send_calls and first_fallback.send_calls and last_resort.send_calls


async def test_all_providers_fail_raises_last_retryable() -> None:
    """When every provider raises a retryable error the
    router surfaces the *last* one (the freshest signal of
    the upstream's state). The exception's ``provider``
    attribute matches the failing provider so an operator
    can correlate the failure with a dashboard chart."""
    primary = _fails_with(
        name="meta_whatsapp",
        error=ProviderUnavailableError("meta 502", provider="meta_whatsapp"),
    )
    fallback = _fails_with(
        name="twilio_whatsapp",
        error=ProviderUnavailableError("twilio 502", provider="twilio_whatsapp"),
    )
    chain = FailoverProvider([primary, fallback])

    with pytest.raises(ProviderUnavailableError) as exc_info:
        await chain.send(to="+56912345678", body="hola")
    assert exc_info.value.provider == "twilio_whatsapp"
    assert "twilio 502" in str(exc_info.value)


# ---------------------------------------------------------------------------
# send() — non-retryable errors short-circuit
# ---------------------------------------------------------------------------


async def test_validation_error_from_primary_does_not_failover() -> None:
    """A :class:`ProviderValidationError` (4xx-class) means
    the request itself is malformed; the next provider would
    reject the same payload. The router must propagate the
    error without trying the fallback, otherwise a
    misconfigured destination would burn the fallback's
    rate-limit budget for nothing."""
    primary = _fails_with(
        name="meta_whatsapp",
        error=ProviderValidationError("bad number", provider="meta_whatsapp"),
    )
    fallback = _ok(name="twilio_whatsapp", provider_msg_id="twilio.1")
    chain = FailoverProvider([primary, fallback])

    with pytest.raises(ProviderValidationError) as exc_info:
        await chain.send(to="+56912345678", body="hola")
    assert exc_info.value.provider == "meta_whatsapp"
    assert fallback.send_calls == []


async def test_validation_error_from_fallback_short_circuits_chain() -> None:
    """If the fallback raises a validation error, the
    remaining providers in the chain must not be tried –
    the error is permanent, not transient."""
    primary = _fails_with(
        name="meta_whatsapp",
        error=ProviderUnavailableError("meta 502", provider="meta_whatsapp"),
    )
    first_fallback = _fails_with(
        name="twilio_whatsapp",
        error=ProviderValidationError("bad template", provider="twilio_whatsapp"),
    )
    last_resort = _ok(name="gupshup_whatsapp", provider_msg_id="gup.1")
    chain = FailoverProvider([primary, first_fallback, last_resort])

    with pytest.raises(ProviderValidationError) as exc_info:
        await chain.send(to="+56912345678", body="hola")
    assert exc_info.value.provider == "twilio_whatsapp"
    assert last_resort.send_calls == []


# ---------------------------------------------------------------------------
# send() — metadata passthrough
# ---------------------------------------------------------------------------


async def test_send_result_preserves_underlying_raw_payload() -> None:
    """The router must surface the *underlying* provider's
    raw response so the messaging service can persist the
    provider-specific fields it cares about (Meta's
    ``messages[0].id``, the SMS aggregator's
    ``message_id`` …). The router is a thin dispatch
    helper, not a response normaliser."""
    raw_payload = {"messages": [{"id": "wamid.HBgLMTY1M"}]}
    primary = _ScriptedProvider(
        name="meta_whatsapp",
        outcomes=[SendResult(provider_msg_id="wamid.HBgLMTY1M", raw=raw_payload)],
    )
    chain = FailoverProvider([primary])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.raw == raw_payload
    assert result.provider_name == "meta_whatsapp"


async def test_send_result_inherits_provider_name_when_underlying_sets_it() -> None:
    """If the underlying provider already populated
    ``provider_name`` on its :class:`SendResult`, the router
    must not overwrite it – the underlying provider's name
    is the most accurate signal of who actually handled the
    call. (A custom provider that returns a
    :class:`SendResult` with a different ``provider_name``
    wins over the router's chain name.)"""
    primary = _ScriptedProvider(
        name="meta_whatsapp",
        outcomes=[
            SendResult(
                provider_msg_id="wamid.1",
                raw={},
                provider_name="meta_whatsapp_business_account",
            )
        ],
    )
    chain = FailoverProvider([primary])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.provider_name == "meta_whatsapp_business_account"


async def test_send_forwards_extra_kwargs_to_underlying_providers() -> None:
    """Keyword arguments to ``send`` (``template``,
    ``media_url`` …, depending on the channel) are part of
    the contract the messaging service relies on. The
    router must pass them through verbatim so a future
    template-aware provider does not lose the ``template``
    field on the way through the chain."""
    primary = _ok(name="meta_whatsapp", provider_msg_id="wamid.1")
    chain = FailoverProvider([primary])

    await chain.send(
        to="+56912345678", body="hola", template="hello_v1", media_url="https://x/y.png"
    )

    assert primary.send_calls == [
        {
            "to": "+56912345678",
            "body": "hola",
            "template": "hello_v1",
            "media_url": "https://x/y.png",
        }
    ]


# ---------------------------------------------------------------------------
# get_status()
# ---------------------------------------------------------------------------


async def test_get_status_uses_underlying_provider_for_known_id() -> None:
    """After a successful send, the router records which
    provider handled the message so a later
    :meth:`get_status` call can ask the *same* upstream for
    the delivery receipt. The Meta Cloud API cannot answer
    for a Twilio id (and vice versa), so a misroute would
    surface as a 404."""
    primary = _fails_with(
        name="meta_whatsapp",
        error=ProviderUnavailableError("meta 502", provider="meta_whatsapp"),
    )
    fallback = _ok(name="twilio_whatsapp", provider_msg_id="twilio.1")
    chain = FailoverProvider([primary, fallback])

    # First call fails over to the fallback.
    await chain.send(to="+56912345678", body="hola")
    status = await chain.get_status("twilio.1")

    assert status == "status:twilio_whatsapp:twilio.1"
    assert primary.status_calls == []
    assert fallback.status_calls == ["twilio.1"]


async def test_get_status_falls_back_to_primary_for_unknown_id() -> None:
    """If the router has no record of the id (a delivery
    receipt that arrives long after the worker rotated, or
    an id from a different process), the call falls back to
    the primary – the common case where the primary never
    failed and the message was never routed through a
    fallback."""
    primary = _ok(name="meta_whatsapp", provider_msg_id="wamid.1")
    fallback = _ok(name="twilio_whatsapp", provider_msg_id="twilio.1")
    chain = FailoverProvider([primary, fallback])

    status = await chain.get_status("wamid.from.another.process")

    assert status == "status:meta_whatsapp:wamid.from.another.process"
    assert primary.status_calls == ["wamid.from.another.process"]
    assert fallback.status_calls == []


async def test_get_status_rejects_empty_id_without_calling_providers() -> None:
    """An empty id is a validation error raised before any
    provider is invoked. The router mirrors the contract of
    the concrete adapters – calling a provider with an
    empty id would surface as a 400 from the upstream, but
    the platform's contract is to fail fast at the
    boundary."""
    primary = _ok(name="meta_whatsapp", provider_msg_id="wamid.1")
    chain = FailoverProvider([primary])

    with pytest.raises(ProviderValidationError):
        await chain.get_status("")
    assert primary.status_calls == []


async def test_get_status_propagates_provider_failure() -> None:
    """If the upstream the router routes the status check to
    fails, the failure propagates – the status refresh is
    a best-effort helper and a 5xx from the upstream is
    surfaced to the caller unchanged."""

    class _FailingGetStatus(BaseProvider):
        name = "meta_whatsapp"

        async def send(self, *, to: str, body: str, **kwargs: object) -> SendResult:
            raise AssertionError("send should not be called by get_status")  # pragma: no cover

        async def get_status(self, provider_msg_id: str) -> str:
            raise ProviderUnavailableError(
                "down", provider="meta_whatsapp"
            )

    chain = FailoverProvider([_FailingGetStatus()])
    with pytest.raises(ProviderUnavailableError):
        await chain.get_status("wamid.1")


# ---------------------------------------------------------------------------
# provider_for()
# ---------------------------------------------------------------------------


def test_provider_for_returns_none_for_unknown_id() -> None:
    """A lookup for an id the router has never seen returns
    ``None``; the caller is expected to fall back to the
    primary. The accessor never raises so the status-refresh
    path stays side-effect free."""
    primary = _ok(name="meta_whatsapp", provider_msg_id="wamid.1")
    chain = FailoverProvider([primary])

    assert chain.provider_for("not-mapped") is None


def test_provider_for_returns_none_when_recorded_provider_was_removed() -> None:
    """If a recorded id points to a provider that is no
    longer in the chain (e.g. an operator removed the
    fallback), the lookup returns ``None`` so the caller
    falls back to the primary rather than crashing on a
    dangling reference."""
    primary = _ok(name="meta_whatsapp", provider_msg_id="wamid.1")
    fallback = _ok(name="twilio_whatsapp", provider_msg_id="twilio.1")
    chain = FailoverProvider([primary, fallback])
    chain._routing["stale.id"] = "ghost_provider"

    assert chain.provider_for("stale.id") is None


# ---------------------------------------------------------------------------
# aclose()
# ---------------------------------------------------------------------------


async def test_aclose_closes_every_provider_in_the_chain() -> None:
    """The router's :meth:`aclose` propagates to every
    provider in the chain so the application factory can
    clean up the connection pool at shutdown. Providers
    that share their HTTP client with the rest of the
    platform are responsible for not tearing it down
    (the concrete adapters already do this); the router
    just forwards the call."""

    closed: list[str] = []

    class _Closeable(BaseProvider):
        def __init__(self, name: str) -> None:
            self.name = name

        async def send(self, *, to: str, body: str, **kwargs: object) -> SendResult:
            raise AssertionError("not used in this test")  # pragma: no cover

        async def get_status(self, provider_msg_id: str) -> str:
            raise AssertionError("not used in this test")  # pragma: no cover

        async def aclose(self) -> None:
            closed.append(self.name)

    chain = FailoverProvider([_Closeable("meta_whatsapp"), _Closeable("twilio_whatsapp")])
    await chain.aclose()

    assert closed == ["meta_whatsapp", "twilio_whatsapp"]


async def test_aclose_tolerates_providers_without_close_method() -> None:
    """A provider that does not implement :meth:`aclose`
    (e.g. a test double or a future in-process stub) must
    not break the router's cleanup. The router calls
    :func:`getattr` with a default so the absence of the
    method is a silent no-op."""

    class _NoClose(BaseProvider):
        name = "stub"

        async def send(self, *, to: str, body: str, **kwargs: object) -> SendResult:
            raise AssertionError("not used in this test")  # pragma: no cover

        async def get_status(self, provider_msg_id: str) -> str:
            raise AssertionError("not used in this test")  # pragma: no cover

    chain = FailoverProvider([_NoClose()])
    # Should not raise.
    await chain.aclose()


# ---------------------------------------------------------------------------
# Custom ProviderError subclass
# ---------------------------------------------------------------------------


async def test_subclass_of_retryable_error_triggers_failover() -> None:
    """A custom subclass of :class:`ProviderUnavailableError`
    is still a retryable error – the ``except`` clause
    matches on the parent class so a deployment that ships
    a new error type (e.g. ``ProviderTimeoutError``) does
    not have to remember to extend ``RETRYABLE_ERRORS``."""

    class ProviderTimeoutError(ProviderUnavailableError):
        pass

    primary = _fails_with(
        name="meta_whatsapp",
        error=ProviderTimeoutError("timeout", provider="meta_whatsapp"),
    )
    fallback = _ok(name="twilio_whatsapp", provider_msg_id="twilio.1")
    chain = FailoverProvider([primary, fallback])

    result = await chain.send(to="+56912345678", body="hola")

    assert result.provider_name == "twilio_whatsapp"


# ---------------------------------------------------------------------------
# Base contract
# ---------------------------------------------------------------------------


def test_failover_provider_satisfies_base_contract() -> None:
    """The router is a :class:`BaseProvider` so the rest of
    the platform can treat it like any other adapter."""
    chain = FailoverProvider([_ok(name="meta_whatsapp")])
    assert isinstance(chain, BaseProvider)
    assert chain.name == "meta_whatsapp"
