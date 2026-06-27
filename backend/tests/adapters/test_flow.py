"""Unit tests for the Flow (payment gateway) adapter.

The tests inject a stub :class:`httpx.AsyncClient` so the
adapter can be exercised end-to-end without ever touching
the network. The stub mirrors the shape of the real Flow
``/payment/create`` and ``/payment/getStatus`` payloads.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.adapters.flow import (
    FlowClient,
    FlowError,
    FlowOrder,
    FlowPaymentStatus,
    FlowRejectionError,
    FlowUnavailableError,
)
from app.config import Settings

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_settings() -> Settings:
    """Settings with the Flow sandbox defaults."""
    return Settings(
        flow_api_key="test-key",
        flow_secret_key="test-secret",
        flow_base_url="https://sandbox.flow.cl/api",
        flow_environment="sandbox",
        flow_webhook_url="https://api.test.cl/v1/billing/webhook/flow",
        flow_confirmation_url="https://app.test.cl/billing/return",
        flow_return_url="https://app.test.cl/billing/cancel",
    )


class _StubTransport(httpx.AsyncBaseTransport):
    """httpx transport stub returning a canned response.

    The tests build one of these and pass it via
    :class:`httpx.AsyncClient(transport=...)`. The stub
    records the outgoing request so a test can assert the
    payload the adapter sent.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_body: dict[str, Any] | None = None,
        text_body: str = "",
    ) -> None:
        self.status_code = status_code
        self.json_body = json_body or {}
        self.text_body = text_body
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # Pass ``text`` *or* ``json`` – the ``Response``
        # constructor treats ``text`` as the literal body
        # and serialises ``json`` only when no ``text`` is
        # provided. Passing both leaves the body empty
        # because ``text`` wins, so we branch on which the
        # caller asked for.
        if self.text_body:
            body = self.text_body
        else:
            import json

            body = json.dumps(self.json_body)
        return httpx.Response(
            status_code=self.status_code,
            content=body.encode("utf-8"),
            request=request,
        )


def _patched_client(transport: _StubTransport) -> httpx.AsyncClient:
    """Return an :class:`httpx.AsyncClient` wired to ``transport``.

    The function uses :data:`_REAL_ASYNC_CLIENT` (the
    class object captured at import time, before any
    patch) to build the stubbed client. Going through
    ``httpx.AsyncClient`` directly would re-enter the
    patched function and recurse.
    """
    return _REAL_ASYNC_CLIENT(transport=transport, base_url="https://sandbox.flow.cl/api")


# Captured at import time so the test helpers can build
# the stubbed client without going through the patched
# symbol. The attribute lives on the module object –
# a future ``monkeypatch.setattr`` on the same name
# will not affect this reference because the value was
# already bound to a local name.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_httpx(monkeypatch, transport: _StubTransport) -> None:
    """Patch the adapter's ``httpx`` import to return a stubbed client.

    The adapter imports :mod:`httpx` at module level, so
    we patch the symbol on the adapter's own module
    (``app.adapters.flow.httpx``) rather than the
    top-level :mod:`httpx` module. Without this the
    patch would not take effect inside the adapter and
    the real HTTP client would attempt to talk to the
    configured Flow base URL.

    The replacement lambda captures the *real*
    :class:`httpx.AsyncClient` at patch time (through
    :data:`_REAL_ASYNC_CLIENT`) so the stub does not
    recurse when the adapter asks for a new client.
    """
    import app.adapters.flow as flow_module

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(transport=transport, base_url="https://sandbox.flow.cl/api")

    monkeypatch.setattr(flow_module.httpx, "AsyncClient", _factory)


# ---------------------------------------------------------------------------
# create_order
# ---------------------------------------------------------------------------


async def test_create_order_posts_to_payment_create(
    fast_settings: Settings, monkeypatch
) -> None:
    """A successful create posts to ``/payment/create`` and returns a token."""
    transport = _StubTransport(
        json_body={
            "token": "01ab23cd-ef45-6789-0abc-def012345678",
            "redirectUrl": "https://sandbox.flow.cl/pay/01ab",
        }
    )
    _patch_httpx(monkeypatch, transport)

    client = FlowClient(settings=fast_settings)
    order = await client.create_order(
        commerce_order="INV-1",
        subject="Factura F-2026-1",
        amount_clp=23788,
        email="ops@acme.cl",
    )

    assert isinstance(order, FlowOrder)
    assert order.token == "01ab23cd-ef45-6789-0abc-def012345678"
    assert order.redirect_url == "https://sandbox.flow.cl/pay/01ab"
    assert order.raw_json  # the raw payload is persisted for audit
    # The request hits the right endpoint with the right
    # base URL.
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert str(request.url) == "https://sandbox.flow.cl/api/payment/create"
    body = request.read().decode("utf-8")
    assert "INV-1" in body
    assert "23788" in body
    assert "ops@acme.cl" in body


@pytest.mark.parametrize(
    "kwargs",
    [
        {"commerce_order": "", "subject": "x", "amount_clp": 1, "email": "x@x.cl"},
        {"commerce_order": "x", "subject": "", "amount_clp": 1, "email": "x@x.cl"},
        {"commerce_order": "x", "subject": "x", "amount_clp": 0, "email": "x@x.cl"},
        {"commerce_order": "x", "subject": "x", "amount_clp": -5, "email": "x@x.cl"},
        {"commerce_order": "x", "subject": "x", "amount_clp": 1, "email": ""},
    ],
)
async def test_create_order_rejects_invalid_inputs(
    fast_settings: Settings, kwargs: dict[str, object]
) -> None:
    """The validation layer rejects every documented bad payload."""
    client = FlowClient(settings=fast_settings)
    with pytest.raises(FlowRejectionError):
        await client.create_order(
            commerce_order=str(kwargs["commerce_order"]),
            subject=str(kwargs["subject"]),
            amount_clp=int(kwargs["amount_clp"]),  # type: ignore[call-overload]
            email=str(kwargs["email"]),
        )


async def test_create_order_raises_on_5xx(
    fast_settings: Settings, monkeypatch
) -> None:
    """A 5xx response surfaces as :class:`FlowUnavailableError`."""
    transport = _StubTransport(status_code=503, json_body={"code": "down"})
    _patch_httpx(monkeypatch, transport)

    client = FlowClient(settings=fast_settings)
    with pytest.raises(FlowUnavailableError) as exc:
        await client.create_order(
            commerce_order="INV-1",
            subject="x",
            amount_clp=1,
            email="x@x.cl",
        )
    assert exc.value.code == "flow_server_error"


async def test_create_order_raises_on_non_json_response(
    fast_settings: Settings, monkeypatch
) -> None:
    """A non-JSON body is treated as a transport-level failure."""
    transport = _StubTransport(status_code=200, text_body="not json")
    _patch_httpx(monkeypatch, transport)

    client = FlowClient(settings=fast_settings)
    with pytest.raises(FlowUnavailableError) as exc:
        await client.create_order(
            commerce_order="INV-1",
            subject="x",
            amount_clp=1,
            email="x@x.cl",
        )
    assert exc.value.code == "flow_invalid_response"


async def test_create_order_raises_on_4xx(
    fast_settings: Settings, monkeypatch
) -> None:
    """A 4xx response surfaces as :class:`FlowRejectionError` with Flow's code."""
    transport = _StubTransport(
        status_code=400, json_body={"code": "invalid_amount", "message": "negative"}
    )
    _patch_httpx(monkeypatch, transport)

    client = FlowClient(settings=fast_settings)
    with pytest.raises(FlowRejectionError) as exc:
        await client.create_order(
            commerce_order="INV-1",
            subject="x",
            amount_clp=1,
            email="x@x.cl",
        )
    assert exc.value.code == "invalid_amount"
    assert exc.value.message == "negative"


async def test_create_order_raises_when_token_missing(
    fast_settings: Settings, monkeypatch
) -> None:
    """A 200 response without a ``token`` is a :class:`FlowRejectionError`."""
    transport = _StubTransport(json_body={"redirectUrl": "x"})
    _patch_httpx(monkeypatch, transport)

    client = FlowClient(settings=fast_settings)
    with pytest.raises(FlowRejectionError) as exc:
        await client.create_order(
            commerce_order="INV-1",
            subject="x",
            amount_clp=1,
            email="x@x.cl",
        )
    assert exc.value.code == "flow_missing_field"


async def test_create_order_raises_on_transport_error(
    fast_settings: Settings, monkeypatch
) -> None:
    """An ``httpx`` transport error is wrapped as :class:`FlowUnavailableError`."""
    class _FailingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated outage")

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return _REAL_ASYNC_CLIENT(transport=_FailingTransport())

    import app.adapters.flow as flow_module

    monkeypatch.setattr(flow_module.httpx, "AsyncClient", _factory)

    client = FlowClient(settings=fast_settings)
    with pytest.raises(FlowUnavailableError) as exc:
        await client.create_order(
            commerce_order="INV-1",
            subject="x",
            amount_clp=1,
            email="x@x.cl",
        )
    assert exc.value.code == "flow_unreachable"


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


async def test_get_status_returns_numeric_status(
    fast_settings: Settings, monkeypatch
) -> None:
    """A status response is decoded into a :class:`FlowPaymentStatus`."""
    transport = _StubTransport(
        json_body={"status": 2, "paymentId": "flow-1", "amount": 23788}
    )
    _patch_httpx(monkeypatch, transport)

    client = FlowClient(settings=fast_settings)
    status = await client.get_status(token="abc")
    assert isinstance(status, FlowPaymentStatus)
    assert status.status == 2
    assert status.payment_id == "flow-1"


async def test_get_status_handles_missing_payment_id(
    fast_settings: Settings, monkeypatch
) -> None:
    """A response without ``paymentId`` still decodes (it's optional)."""
    transport = _StubTransport(json_body={"status": 1})
    _patch_httpx(monkeypatch, transport)

    client = FlowClient(settings=fast_settings)
    status = await client.get_status(token="abc")
    assert status.status == 1
    assert status.payment_id is None


async def test_get_status_rejects_invalid_token(
    fast_settings: Settings,
) -> None:
    """An empty / missing token is rejected at the boundary."""
    client = FlowClient(settings=fast_settings)
    with pytest.raises(FlowRejectionError):
        await client.get_status(token="")
    with pytest.raises(FlowRejectionError):
        await client.get_status(token=None)  # type: ignore[arg-type]


async def test_get_status_raises_on_non_numeric_status(
    fast_settings: Settings, monkeypatch
) -> None:
    """A non-numeric status field is a transport error – not silently ``0``."""
    transport = _StubTransport(json_body={"status": "paid-ish"})
    _patch_httpx(monkeypatch, transport)

    client = FlowClient(settings=fast_settings)
    with pytest.raises(FlowUnavailableError) as exc:
        await client.get_status(token="abc")
    assert exc.value.code == "invalid_status"


async def test_get_status_raises_on_5xx(
    fast_settings: Settings, monkeypatch
) -> None:
    """A 5xx response surfaces as :class:`FlowUnavailableError`."""
    transport = _StubTransport(status_code=502, json_body={"code": "down"})
    _patch_httpx(monkeypatch, transport)

    client = FlowClient(settings=fast_settings)
    with pytest.raises(FlowUnavailableError) as exc:
        await client.get_status(token="abc")
    assert exc.value.code == "flow_server_error"


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


def test_flow_client_uses_settings_base_url() -> None:
    """The base URL is taken from the settings (no hard-coded URLs)."""
    cfg = Settings(flow_base_url="https://www.flow.cl/api")
    client = FlowClient(settings=cfg)
    assert client._base_url == "https://www.flow.cl/api"


def test_flow_client_strips_trailing_slash() -> None:
    """A ``/`` suffix on the base URL does not duplicate in the request path."""
    cfg = Settings(flow_base_url="https://www.flow.cl/api/")
    client = FlowClient(settings=cfg)
    assert client._base_url == "https://www.flow.cl/api"


def test_flow_error_is_subclass_of_exception() -> None:
    """The error hierarchy is rooted in :class:`FlowError`."""
    assert issubclass(FlowUnavailableError, FlowError)
    assert issubclass(FlowRejectionError, FlowError)
