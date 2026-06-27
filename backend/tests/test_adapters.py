"""Unit tests for the provider adapter layer (issue #4).

The tests cover:

- :class:`app.adapters.errors` – every exception type the
  adapters raise, and the HTTP status they map to.
- :class:`app.adapters.meta_whatsapp.MetaWhatsAppProvider` –
  the request shape, success and failure paths, and the
  mapping from an :class:`httpx.Response` to a
  :class:`SendResult`.
- :class:`app.adapters.sms_aggregator.SmsAggregatorProvider` –
  same contract, different wire format.
- :func:`app.adapters.registry.get_provider` – the
  channel → adapter mapping.

The HTTP layer is exercised through a stub
:class:`httpx.AsyncClient` so the suite never opens a real
TCP connection (see the ``transport`` fixture in
:mod:`tests.test_adapters`).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.adapters.meta_whatsapp import MetaWhatsAppProvider
from app.adapters.registry import (
    UnsupportedChannelError,
    get_provider,
    supported_channels,
)
from app.adapters.sms_aggregator import SmsAggregatorProvider
from app.config import Settings
from app.models.message import Channel

# ---------------------------------------------------------------------------
# Base contract (regression tests for the abstract base class)
# ---------------------------------------------------------------------------


def test_base_provider_is_abstract() -> None:
    """``BaseProvider`` must remain abstract: every concrete
    provider is expected to implement ``send`` and ``get_status``.
    A non-abstract base would let silent no-op adapters slip in."""
    import inspect
    from abc import ABC

    assert inspect.isabstract(BaseProvider)
    assert issubclass(BaseProvider, ABC)
    assert BaseProvider.__abstractmethods__ == frozenset({"send", "get_status"})


def test_send_result_is_immutable() -> None:
    """``SendResult`` is a frozen dataclass; the router relies on
    the value being safe to pass around without worrying about
    downstream code mutating it."""
    result = SendResult(provider_msg_id="abc", raw={"foo": "bar"})
    assert result.provider_msg_id == "abc"
    assert result.raw == {"foo": "bar"}
    with pytest.raises((AttributeError, Exception)):
        result.provider_msg_id = "tampered"  # type: ignore[misc]


def test_concrete_provider_must_implement_contract() -> None:
    """Subclassing without implementing the contract should fail
    at instantiation time, not at request time."""

    class Incomplete(BaseProvider):
        name = "incomplete"

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_concrete_provider_implements_contract() -> None:
    """A complete subclass can be instantiated and round-trips a
    ``SendResult``; the abstract base is satisfied."""

    class Echo(BaseProvider):
        name = "echo"

        async def send(self, *, to: str, body: str, **_kwargs: object) -> SendResult:
            return SendResult(provider_msg_id=f"echo-{to}", raw={"to": to, "body": body})

        async def get_status(self, provider_msg_id: str) -> str:
            return "delivered" if provider_msg_id.startswith("echo-") else "unknown"

    async def _exercise() -> tuple[str, str]:
        provider = Echo()
        result = await provider.send(to="+56912345678", body="hola")
        status = await provider.get_status(result.provider_msg_id)
        return result.provider_msg_id, status

    provider_msg_id, status = asyncio.run(_exercise())
    assert provider_msg_id == "echo-+56912345678"
    assert status == "delivered"


# ---------------------------------------------------------------------------
# Shared HTTP transport stub
# ---------------------------------------------------------------------------


class _StubTransport(httpx.AsyncBaseTransport):
    """An :class:`httpx` transport that returns a pre-canned response.

    Each request is recorded in :attr:`requests` so the test
    can assert on the URL, headers and body the adapter
    actually sent. The response is a :class:`httpx.Response`
    that the adapter treats as if it came from the real
    upstream.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        body: dict[str, Any] | str | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self._body, dict):
            payload = json.dumps(self._body)
        else:
            payload = self._body or ""
        return httpx.Response(self.status_code, content=payload, request=request)


def _stub_client(transport: _StubTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="https://example.test")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_provider_error_default_http_status() -> None:
    """The base :class:`ProviderError` maps to 502 so a future
    subclass that forgets to override ``http_status`` does not
    silently surface a 200."""
    exc = ProviderError("boom", provider="meta_whatsapp")
    assert exc.http_status == 502
    assert exc.code == "provider_error"
    assert exc.provider == "meta_whatsapp"
    assert str(exc) == "boom"


def test_provider_unavailable_maps_to_502() -> None:
    """A 5xx-class upstream failure surfaces as a 502 to the
    caller because the platform itself is healthy but cannot
    fulfil the request through the configured provider."""
    exc = ProviderUnavailableError("down", provider="meta_whatsapp")
    assert exc.http_status == 502
    assert exc.code == "provider_unavailable"


def test_provider_validation_maps_to_422() -> None:
    """A 4xx-class upstream rejection surfaces as a 422 so the
    caller knows the request was well-formed at the platform
    edge but the upstream's own validator said no."""
    exc = ProviderValidationError("bad", provider="sms_aggregator")
    assert exc.http_status == 422
    assert exc.code == "provider_validation"


def test_provider_rate_limit_maps_to_429() -> None:
    """A 429 from the upstream surfaces as 429 to the caller so
    the worker can honour the retry-after hint."""
    exc = ProviderRateLimitError("slow down", provider="meta_whatsapp", retry_after=2.5)
    assert exc.http_status == 429
    assert exc.code == "provider_rate_limited"
    assert exc.retry_after == 2.5


# ---------------------------------------------------------------------------
# MetaWhatsAppProvider
# ---------------------------------------------------------------------------


def test_meta_whatsapp_provider_is_a_base_provider() -> None:
    """The adapter must satisfy the :class:`BaseProvider`
    contract so the registry can hand it to the service
    layer without a type check."""
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=httpx.AsyncClient()
    )
    assert isinstance(provider, BaseProvider)
    assert provider.name == "meta_whatsapp"
    assert provider.endpoint.endswith("/p/messages")


def test_meta_whatsapp_provider_requires_token_and_phone() -> None:
    """A missing access token / phone number id is a
    configuration error caught at construction time."""
    with pytest.raises(ValueError):
        MetaWhatsAppProvider(access_token="", phone_number_id="p", client=httpx.AsyncClient())
    with pytest.raises(ValueError):
        MetaWhatsAppProvider(access_token="t", phone_number_id="", client=httpx.AsyncClient())


def test_meta_whatsapp_send_posts_text_payload() -> None:
    """A successful send POSTs the expected JSON body, carries
    the bearer token, and returns the provider message id."""
    transport = _StubTransport(
        status_code=200,
        body={"messages": [{"id": "wamid.HBgLMTY1M"}]},
    )
    provider = MetaWhatsAppProvider(
        access_token="test-token",
        phone_number_id="12345",
        client=_stub_client(transport),
    )

    result = asyncio.run(provider.send(to="+56912345678", body="hola"))

    assert isinstance(result, SendResult)
    assert result.provider_msg_id == "wamid.HBgLMTY1M"
    assert result.raw == {"messages": [{"id": "wamid.HBgLMTY1M"}]}

    # Inspect the wire-level request.
    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert sent.method == "POST"
    assert sent.url.path.endswith("/12345/messages")
    assert sent.headers["authorization"] == "Bearer test-token"
    assert sent.headers["content-type"] == "application/json"
    payload = json.loads(sent.content)
    assert payload == {
        "messaging_product": "whatsapp",
        "to": "+56912345678",
        "type": "text",
        "text": {"body": "hola"},
    }


def test_meta_whatsapp_send_maps_5xx_to_unavailable() -> None:
    """A 500-class response becomes a
    :class:`ProviderUnavailableError` so the route layer can
    surface a 502 to the caller."""
    transport = _StubTransport(status_code=500, body={"error": "boom"})
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=_stub_client(transport)
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_meta_whatsapp_send_maps_429_to_rate_limited() -> None:
    """A 429 response becomes a
    :class:`ProviderRateLimitError` so the worker can honour
    the back-off."""
    transport = _StubTransport(status_code=429, body={"error": "rate_limited"})
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=_stub_client(transport)
    )

    with pytest.raises(ProviderRateLimitError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_meta_whatsapp_send_maps_4xx_to_validation() -> None:
    """A 4xx response becomes a
    :class:`ProviderValidationError` so the caller knows the
    request was well-formed but the upstream's own validator
    said no."""
    transport = _StubTransport(status_code=400, body={"error": "bad"})
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=_stub_client(transport)
    )

    with pytest.raises(ProviderValidationError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_meta_whatsapp_send_rejects_empty_inputs() -> None:
    """An empty ``to`` or ``body`` is a validation error
    before the HTTP call is made – the upstream would reject
    it too, but we fail fast at the adapter boundary."""
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=httpx.AsyncClient()
    )

    with pytest.raises(ProviderValidationError):
        asyncio.run(provider.send(to="", body="hola"))
    with pytest.raises(ProviderValidationError):
        asyncio.run(provider.send(to="+56912345678", body=""))


def test_meta_whatsapp_send_rejects_missing_message_id() -> None:
    """A 2xx response without ``messages[0].id`` is treated
    as an upstream failure – silently accepting it would
    leave the platform without a way to poll the status."""
    transport = _StubTransport(status_code=200, body={"messages": [{}]})
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=_stub_client(transport)
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_meta_whatsapp_send_handles_non_json_response() -> None:
    """A 2xx response that is not parseable as JSON is an
    upstream failure: silently accepting the message would
    leave the platform without a way to poll the status."""
    transport = _StubTransport(status_code=200, body="<html>oops</html>")
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=_stub_client(transport)
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_meta_whatsapp_send_handles_empty_messages_array() -> None:
    """A 2xx response with an empty ``messages`` array is an
    upstream failure: the platform needs the message id to
    poll the status later."""
    transport = _StubTransport(status_code=200, body={"messages": []})
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=_stub_client(transport)
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


class _FailingTransport(httpx.AsyncBaseTransport):
    """An :class:`httpx` transport that raises a
    :class:`httpx.ConnectError` on every request.

    Used to exercise the ``httpx.HTTPError`` → provider
    exception translation the adapters perform in their
    ``except httpx.HTTPError`` blocks.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise httpx.ConnectError("refused", request=request)


def test_meta_whatsapp_send_handles_transport_error() -> None:
    """A transport-level error (e.g. connection refused)
    becomes a :class:`ProviderUnavailableError` so the route
    layer can surface a 502 to the caller."""
    transport = _FailingTransport()
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=httpx.AsyncClient(transport=transport)
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))
    assert transport.calls == 1


def test_meta_whatsapp_get_status_returns_sent_on_success() -> None:
    """A successful status check returns ``sent`` so the
    service layer can map it to the platform's vocabulary."""
    transport = _StubTransport(status_code=200, body={"messages": [{"id": "wamid.1"}]})
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=_stub_client(transport)
    )

    status = asyncio.run(provider.get_status("wamid.1"))
    assert status == "sent"


def test_meta_whatsapp_get_status_maps_5xx_to_unavailable() -> None:
    transport = _StubTransport(status_code=500, body={"error": "boom"})
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=_stub_client(transport)
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.get_status("wamid.1"))


def test_meta_whatsapp_get_status_maps_429_to_rate_limited() -> None:
    transport = _StubTransport(status_code=429, body={"error": "rate"})
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=_stub_client(transport)
    )

    with pytest.raises(ProviderRateLimitError):
        asyncio.run(provider.get_status("wamid.1"))


def test_meta_whatsapp_get_status_maps_4xx_to_validation() -> None:
    transport = _StubTransport(status_code=404, body={"error": "not_found"})
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=_stub_client(transport)
    )

    with pytest.raises(ProviderValidationError):
        asyncio.run(provider.get_status("wamid.1"))


def test_meta_whatsapp_get_status_rejects_empty_id() -> None:
    """An empty provider message id is a validation error
    before the HTTP call is made."""
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=httpx.AsyncClient()
    )

    with pytest.raises(ProviderValidationError):
        asyncio.run(provider.get_status(""))


def test_meta_whatsapp_get_status_handles_transport_error() -> None:
    transport = _FailingTransport()
    provider = MetaWhatsAppProvider(
        access_token="t", phone_number_id="p", client=httpx.AsyncClient(transport=transport)
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.get_status("wamid.1"))


def test_meta_whatsapp_aclose_does_not_close_injected_client() -> None:
    """``aclose`` is a no-op when the client was injected
    (the platform owns the connection pool); the adapter
    closes the client only when it created it itself."""
    external = httpx.AsyncClient()
    provider = MetaWhatsAppProvider(access_token="t", phone_number_id="p", client=external)
    asyncio.run(provider.aclose())
    assert not external.is_closed


# ---------------------------------------------------------------------------
# SmsAggregatorProvider
# ---------------------------------------------------------------------------


def test_sms_aggregator_provider_is_a_base_provider() -> None:
    """The adapter must satisfy the :class:`BaseProvider`
    contract."""
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=httpx.AsyncClient(),
    )
    assert isinstance(provider, BaseProvider)
    assert provider.name == "sms_aggregator"
    assert provider.endpoint == "https://sms.test/v1/send"


def test_sms_aggregator_provider_requires_credentials() -> None:
    """A missing api_url / api_key / sender_id is a
    configuration error caught at construction time."""
    with pytest.raises(ValueError):
        SmsAggregatorProvider(api_url="", api_key="k", sender_id="s", client=httpx.AsyncClient())
    with pytest.raises(ValueError):
        SmsAggregatorProvider(api_url="u", api_key="", sender_id="s", client=httpx.AsyncClient())
    with pytest.raises(ValueError):
        SmsAggregatorProvider(api_url="u", api_key="k", sender_id="", client=httpx.AsyncClient())


def test_sms_aggregator_send_posts_expected_payload() -> None:
    """A successful send POSTs the expected JSON body, carries
    the bearer token, and returns the provider message id."""
    transport = _StubTransport(status_code=200, body={"message_id": "agg-1"})
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    result = asyncio.run(provider.send(to="+56912345678", body="hola"))
    assert result.provider_msg_id == "agg-1"

    sent = transport.requests[0]
    assert sent.method == "POST"
    assert sent.url.path.endswith("/v1/send")
    assert sent.headers["authorization"] == "Bearer k"
    payload = json.loads(sent.content)
    assert payload == {"to": "+56912345678", "from": "MSGGTWY", "body": "hola"}


def test_sms_aggregator_send_maps_5xx_to_unavailable() -> None:
    """A 500-class response becomes a
    :class:`ProviderUnavailableError`."""
    transport = _StubTransport(status_code=500, body={"error": "boom"})
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_sms_aggregator_send_rejects_error_envelope() -> None:
    """A 2xx response that carries an ``error`` envelope is
    treated as a validation error – some aggregators wrap
    business-level rejections in a 200 response and the
    platform must not silently accept them."""
    transport = _StubTransport(
        status_code=200, body={"error": {"code": "invalid_number", "message": "nope"}}
    )
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    with pytest.raises(ProviderValidationError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_sms_aggregator_send_accepts_alt_id_key() -> None:
    """Some aggregators return ``id`` instead of
    ``message_id``; the adapter must accept both shapes so a
    swap of providers does not break the integration."""
    transport = _StubTransport(status_code=200, body={"id": "agg-42"})
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    result = asyncio.run(provider.send(to="+56912345678", body="hola"))
    assert result.provider_msg_id == "agg-42"


def test_sms_aggregator_send_maps_4xx_to_validation() -> None:
    transport = _StubTransport(status_code=400, body={"error": "bad"})
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    with pytest.raises(ProviderValidationError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_sms_aggregator_send_maps_429_to_rate_limited() -> None:
    transport = _StubTransport(status_code=429, body={"error": "rate"})
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    with pytest.raises(ProviderRateLimitError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_sms_aggregator_send_handles_non_json_response() -> None:
    transport = _StubTransport(status_code=200, body="<html>oops</html>")
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_sms_aggregator_send_rejects_missing_message_id() -> None:
    transport = _StubTransport(status_code=200, body={})
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_sms_aggregator_send_rejects_empty_inputs() -> None:
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=httpx.AsyncClient(),
    )

    with pytest.raises(ProviderValidationError):
        asyncio.run(provider.send(to="", body="hola"))
    with pytest.raises(ProviderValidationError):
        asyncio.run(provider.send(to="+56912345678", body=""))


def test_sms_aggregator_send_handles_transport_error() -> None:
    transport = _FailingTransport()
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=httpx.AsyncClient(transport=transport),
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.send(to="+56912345678", body="hola"))


def test_sms_aggregator_get_status_returns_value() -> None:
    transport = _StubTransport(status_code=200, body={"status": "delivered"})
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    status = asyncio.run(provider.get_status("agg-1"))
    assert status == "delivered"


def test_sms_aggregator_get_status_defaults_to_sent() -> None:
    """An aggregator that omits ``status`` from the response
    is treated as ``sent`` (the message was accepted)."""
    transport = _StubTransport(status_code=200, body={})
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    status = asyncio.run(provider.get_status("agg-1"))
    assert status == "sent"


def test_sms_aggregator_get_status_maps_5xx_to_unavailable() -> None:
    transport = _StubTransport(status_code=500, body={"error": "boom"})
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.get_status("agg-1"))


def test_sms_aggregator_get_status_maps_429_to_rate_limited() -> None:
    transport = _StubTransport(status_code=429, body={"error": "rate"})
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    with pytest.raises(ProviderRateLimitError):
        asyncio.run(provider.get_status("agg-1"))


def test_sms_aggregator_get_status_maps_4xx_to_validation() -> None:
    transport = _StubTransport(status_code=404, body={"error": "not_found"})
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    with pytest.raises(ProviderValidationError):
        asyncio.run(provider.get_status("agg-1"))


def test_sms_aggregator_get_status_rejects_empty_id() -> None:
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=httpx.AsyncClient(),
    )

    with pytest.raises(ProviderValidationError):
        asyncio.run(provider.get_status(""))


def test_sms_aggregator_get_status_handles_transport_error() -> None:
    transport = _FailingTransport()
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=httpx.AsyncClient(transport=transport),
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.get_status("agg-1"))


def test_sms_aggregator_get_status_handles_non_json_response() -> None:
    transport = _StubTransport(status_code=200, body="not-json")
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=_stub_client(transport),
    )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.get_status("agg-1"))


def test_sms_aggregator_aclose_does_not_close_injected_client() -> None:
    """``aclose`` is a no-op when the client was injected;
    the adapter closes the client only when it created it
    itself."""
    external = httpx.AsyncClient()
    provider = SmsAggregatorProvider(
        api_url="https://sms.test",
        api_key="k",
        sender_id="MSGGTWY",
        client=external,
    )
    asyncio.run(provider.aclose())
    assert not external.is_closed


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_supported_channels_contains_known_channels() -> None:
    """The registry must surface every channel the platform
    supports so the OpenAPI schema and the routes can
    advertise the legal values without hard-coding the
    enum."""
    assert set(supported_channels()) == {Channel.SMS, Channel.WHATSAPP}


def test_get_provider_returns_correct_adapter() -> None:
    """``get_provider`` must return the adapter registered
    for the given channel; the assertion is on the
    :class:`BaseProvider` contract so a swap of the
    concrete class does not break the test."""
    settings = Settings()
    whatsapp = get_provider(Channel.WHATSAPP, settings=settings)
    sms = get_provider(Channel.SMS, settings=settings)

    assert isinstance(whatsapp, MetaWhatsAppProvider)
    assert isinstance(sms, SmsAggregatorProvider)
    assert whatsapp.name == "meta_whatsapp"
    assert sms.name == "sms_aggregator"


def test_get_provider_raises_for_unknown_channel() -> None:
    """An unknown channel is a :class:`UnsupportedChannelError`
    (a :class:`ValueError` subclass) so the route layer can
    surface a 422 without importing provider-specific
    exceptions."""
    settings = Settings()
    with pytest.raises(UnsupportedChannelError):
        # ``Channel`` is a :class:`enum.StrEnum` – we have to
        # bypass the constructor to simulate a value the
        # platform does not know about.
        get_provider("unknown", settings=settings)
