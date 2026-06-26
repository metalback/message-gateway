"""Unit tests for the provider adapter contract."""

from __future__ import annotations

import inspect
from abc import ABC

import pytest

from app.adapters.base import BaseProvider, SendResult


def test_base_provider_is_abstract() -> None:
    """``BaseProvider`` must remain abstract: every concrete
    provider is expected to implement ``send`` and ``get_status``.
    A non-abstract base would let silent no-op adapters slip in."""
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

    import asyncio

    async def _exercise() -> tuple[str, str]:
        provider = Echo()
        result = await provider.send(to="+56912345678", body="hola")
        status = await provider.get_status(result.provider_msg_id)
        return result.provider_msg_id, status

    provider_msg_id, status = asyncio.run(_exercise())
    assert provider_msg_id == "echo-+56912345678"
    assert status == "delivered"
