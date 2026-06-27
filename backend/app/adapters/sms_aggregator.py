"""Local SMS aggregator adapter.

The MVP integrates with a Chilean SMS aggregator via a REST API.
The aggregator's exact contract is out of scope for the unit
tests – we only need to know that:

- ``POST /v1/send`` accepts a JSON body with the destination,
  sender id and message body.
- The response is a JSON object that carries a ``message_id``
  field on success or an ``error`` object on failure.

Concrete aggregator URLs vary by operator; the adapter takes
the URL as a constructor argument so the deployment can point
at the contracted provider without a code change.

Wire format (illustrative – the real provider may differ)::

    POST https://api.aggregator.cl/v1/send
    Authorization: Bearer <api_key>
    Content-Type: application/json

    {
        "to": "+56912345678",
        "from": "MSGGTWY",
        "body": "Hola desde Message Gateway"
    }

Successful response::

    {
        "message_id": "agg-1234",
        "status": "queued"
    }

Error response::

    {
        "error": {
            "code": "invalid_number",
            "message": "destination is not a valid mobile number"
        }
    }
"""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import (
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderValidationError,
)

DEFAULT_TIMEOUT_SECONDS = 10.0


class SmsAggregatorProvider(BaseProvider):
    """Adapter for the local Chilean SMS aggregator.

    Stateless: every instance is cheap to create, and the
    underlying :class:`httpx.AsyncClient` can be shared across
    the process.
    """

    name = "sms_aggregator"

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        sender_id: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_url:
            raise ValueError("api_url is required for SmsAggregatorProvider")
        if not api_key:
            raise ValueError("api_key is required for SmsAggregatorProvider")
        if not sender_id:
            raise ValueError("sender_id is required for SmsAggregatorProvider")
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._sender_id = sender_id
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def endpoint(self) -> str:
        """Return the absolute URL the adapter POSTs to."""
        return f"{self._api_url}/v1/send"

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, building it on first use."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP client if the adapter created it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
        """Deliver a single SMS via the aggregator REST API.

        ``to``   – destination in the canonical ``+56…`` form.
        ``body`` – plain text payload.
        """
        if not to or not body:
            raise ProviderValidationError(
                "to and body are required",
                provider=self.name,
            )
        client = await self._get_client()
        payload: dict[str, Any] = {
            "to": to,
            "from": self._sender_id,
            "body": body,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = await client.post(self.endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"sms aggregator unreachable: {exc}",
                provider=self.name,
            ) from exc
        return _parse_send_response(response, provider_name=self.name)

    async def get_status(self, provider_msg_id: str) -> str:
        """Return a normalised status string for a previously sent SMS.

        The MVP keeps the contract narrow: the aggregator is
        responsible for honouring the delivery report webhook
        separately, and the GET endpoint is a thin polling
        helper. A successful response maps to ``sent``; the
        error handling mirrors :func:`send`.
        """
        if not provider_msg_id:
            raise ProviderValidationError(
                "provider_msg_id is required",
                provider=self.name,
            )
        client = await self._get_client()
        url = f"{self._api_url}/v1/messages/{provider_msg_id}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"sms aggregator unreachable: {exc}",
                provider=self.name,
            ) from exc
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"sms aggregator returned {response.status_code}",
                provider=self.name,
            )
        if response.status_code == 429:
            raise ProviderRateLimitError(
                "sms aggregator rate limited",
                provider=self.name,
            )
        if response.status_code >= 400:
            raise ProviderValidationError(
                f"sms aggregator rejected status check: {_truncate(response.text)}",
                provider=self.name,
            )
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError(
                f"sms aggregator returned a non-JSON response: {_truncate(response.text)}",
                provider=self.name,
            ) from exc
        return str(data.get("status") or "sent")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_send_response(response: httpx.Response, *, provider_name: str) -> SendResult:
    """Translate an :class:`httpx.Response` into a :class:`SendResult`."""
    if response.status_code >= 500:
        raise ProviderUnavailableError(
            f"{provider_name} returned {response.status_code}",
            provider=provider_name,
        )
    if response.status_code == 429:
        raise ProviderRateLimitError(
            f"{provider_name} rate limited",
            provider=provider_name,
        )
    if response.status_code >= 400:
        raise ProviderValidationError(
            f"{provider_name} rejected the message: {_truncate(response.text)}",
            provider=provider_name,
        )
    try:
        data: dict[str, Any] = response.json()
    except ValueError as exc:
        raise ProviderUnavailableError(
            f"{provider_name} returned a non-JSON response: {_truncate(response.text)}",
            provider=provider_name,
        ) from exc
    # Some aggregators wrap the error in an ``error`` key even on
    # a 2xx response when the message is rejected at the
    # business layer. The branch below is a defensive catch so
    # the platform never accepts a message the upstream marked
    # as failed.
    if isinstance(data.get("error"), dict):
        raise ProviderValidationError(
            f"{provider_name} rejected the message: {data['error']!r}",
            provider=provider_name,
        )
    provider_msg_id = str(data.get("message_id") or data.get("id") or "")
    if not provider_msg_id:
        raise ProviderUnavailableError(
            f"{provider_name} response missing message id: {data!r}",
            provider=provider_name,
        )
    return SendResult(provider_msg_id=provider_msg_id, raw=data)


def _truncate(value: str, *, limit: int = 200) -> str:
    """Trim a string to a single line and a bounded length."""
    single = value.splitlines()[0] if value else ""
    return single[:limit]


__all__ = ("DEFAULT_TIMEOUT_SECONDS", "SmsAggregatorProvider")
