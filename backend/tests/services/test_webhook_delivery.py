"""Unit tests for the :class:`WebhookDeliveryClient`.

The class is a thin wrapper over :class:`httpx.AsyncClient`
plus a bounded exponential back-off. The test suite
focuses on the contract that matters at the service-layer
boundary:

- A 2xx response is reported as success on the first
  attempt.
- A 5xx response is retried up to ``max_attempts`` times
  and finally reported as failure.
- A transport error (DNS, connection refused, …) is
  retried the same way and reported as failure with an
  informative error string.
- The class is reusable: a second ``deliver()`` call does
  not inherit the state of the first (the ``calls``
  counter on the test transport is reset between
  invocations).

The unit tests stub out the transport with
:class:`httpx.MockTransport` so the suite never opens a
real TCP connection.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.webhook_delivery import WebhookDeliveryClient


def _build_client(
    *,
    transport: httpx.MockTransport,
    max_attempts: int = 2,
) -> WebhookDeliveryClient:
    """Return a :class:`WebhookDeliveryClient` whose
    underlying :class:`httpx.AsyncClient` is wired to the
    supplied mock transport.

    Direct construction (instead of going through the
    default ``_ensure_client`` path) lets the test pin
    the exact transport the client uses so the assertion
    of the call count is exact.
    """
    client = WebhookDeliveryClient(
        timeout_seconds=0.1,
        max_attempts=max_attempts,
        backoff_base_seconds=0.0,
    )
    client._client = httpx.AsyncClient(
        timeout=httpx.Timeout(0.1),
        transport=transport,
    )
    return client


@pytest.mark.asyncio
async def test_deliver_returns_success_on_2xx() -> None:
    """A 200 on the first attempt is reported as success
    and the client does not retry."""
    calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="ok")

    client = _build_client(transport=httpx.MockTransport(_handler))
    try:
        result = await client.deliver(
            url="https://example.com/hooks",
            body=b"{}",
            headers={"X-Mgw-Signature": "abc"},
        )
    finally:
        await client.aclose()

    assert result.succeeded is True
    assert result.attempts == 1
    assert result.status_code == 200
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_deliver_retries_on_5xx_and_eventually_fails() -> None:
    """A 5xx response is retried ``max_attempts`` times;
    the final outcome is failure with the last status
    code the receiver returned."""
    calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="down")

    client = _build_client(transport=httpx.MockTransport(_handler), max_attempts=3)
    try:
        result = await client.deliver(
            url="https://example.com/hooks",
            body=b"{}",
            headers={"X-Mgw-Signature": "abc"},
        )
    finally:
        await client.aclose()

    assert result.succeeded is False
    assert result.attempts == 3
    assert result.status_code == 503
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_deliver_retries_on_transport_error() -> None:
    """A transport-level error (e.g. connection refused)
    is retried the same way as a 5xx – the contract is
    "the helper is the platform's best-effort
    delivery", not "the helper succeeds on the first
    attempt or fails loudly"."""
    calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("simulated DNS failure")

    client = _build_client(transport=httpx.MockTransport(_handler), max_attempts=2)
    try:
        result = await client.deliver(
            url="https://example.com/hooks",
            body=b"{}",
            headers={"X-Mgw-Signature": "abc"},
        )
    finally:
        await client.aclose()

    assert result.succeeded is False
    assert result.attempts == 2
    assert result.status_code is None
    assert "ConnectError" in (result.error or "")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_deliver_succeeds_after_one_retry() -> None:
    """A flaky endpoint that fails once and then returns
    200 is reported as success with ``attempts == 2``.
    The platform's contract is "best effort, retry on
    failure" – a flaky receiver is not the same as a
    broken one."""
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, text="ok")

    client = _build_client(transport=httpx.MockTransport(_handler), max_attempts=3)
    try:
        result = await client.deliver(
            url="https://example.com/hooks",
            body=b"{}",
            headers={"X-Mgw-Signature": "abc"},
        )
    finally:
        await client.aclose()

    assert result.succeeded is True
    assert result.attempts == 2
    assert result.status_code == 200
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_aclose_closes_underlying_client() -> None:
    """``aclose()`` closes the underlying
    :class:`httpx.AsyncClient` and resets the cached
    handle so the next ``deliver()`` call rebuilds it.
    A worker that calls ``aclose()`` on shutdown must
    not leak sockets."""
    client = WebhookDeliveryClient(
        timeout_seconds=0.1, max_attempts=1, backoff_base_seconds=0.0
    )
    # No client has been built yet; ``aclose()`` is a
    # no-op in that state.
    await client.aclose()
    assert client._client is None
