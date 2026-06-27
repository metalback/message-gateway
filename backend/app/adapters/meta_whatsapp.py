"""Meta Cloud API adapter (WhatsApp).

The MVP sends WhatsApp traffic through Meta's Cloud API
(``graph.facebook.com``) using the WABA owned by the platform.
Every client shares the same sender number; per-customer
templating is handled at the API edge, not by giving each
customer their own WABA.

Wire format (abbreviated)::

    POST https://graph.facebook.com/v22.0/<phone_number_id>/messages
    Authorization: Bearer <access_token>
    Content-Type: application/json

    {
        "messaging_product": "whatsapp",
        "to": "+56912345678",
        "type": "text",
        "text": {"body": "Hola desde Message Gateway"}
    }

The successful response carries a ``messages[0].id`` field that
we persist as ``provider_msg_id`` so subsequent status checks
can be correlated.

The adapter accepts an optional ``httpx.AsyncClient`` (the
default is built lazily from the configured ``meta_api_base``)
so tests can swap it for a stub without monkeypatching the
global HTTP client.
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

# The default Meta Graph API version. Pinned here so a future
# upgrade is a one-line change; the value follows the "v22.0"
# baseline documented in the PRD's "Stack técnico" section.
DEFAULT_GRAPH_API_VERSION = "v22.0"

# Maximum number of seconds a single HTTP call may take before
# we treat the upstream as "unavailable" and surface a 502. The
# bound matches Meta's own recommended ceiling for the
# ``/messages`` endpoint.
DEFAULT_TIMEOUT_SECONDS = 10.0


class MetaWhatsAppProvider(BaseProvider):
    """Adapter for the Meta Cloud API (WhatsApp).

    The provider is stateless: every instance is cheap to
    create, and the underlying ``httpx.AsyncClient`` can be
    shared across the process (which is what the application
    factory does).
    """

    name = "meta_whatsapp"

    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        api_base: str = "https://graph.facebook.com",
        api_version: str = DEFAULT_GRAPH_API_VERSION,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("access_token is required for MetaWhatsAppProvider")
        if not phone_number_id:
            raise ValueError("phone_number_id is required for MetaWhatsAppProvider")
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._api_base = api_base.rstrip("/")
        self._api_version = api_version
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def endpoint(self) -> str:
        """Return the absolute URL Meta receives ``POST /messages`` on."""
        return f"{self._api_base}/{self._api_version}/{self._phone_number_id}/messages"

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, building it on first use.

        Kept as a property so the constructor stays synchronous
        (the rest of the platform does not need to ``await``
        before it can hand the adapter around).
        """
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP client if the adapter created it.

        Safe to call on adapters that received an external
        client: the ``_owns_client`` flag tracks ownership so
        we do not tear down a connection pool the rest of the
        app relies on.
        """
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
        """Deliver a single text message via the Meta Cloud API.

        ``to``  – destination in the canonical ``+56…`` form.
        ``body`` – plain text payload (templates land in a
                  follow-up task).
        """
        if not to or not body:
            raise ProviderValidationError(
                "to and body are required",
                provider=self.name,
            )
        client = await self._get_client()
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        # ``preview_url`` is off by default – Meta only enables
        # URL previews for the WhatsApp Business API when the
        # client asks for them. The platform does not promise
        # URL previews in the MVP, so we leave it off.
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        try:
            response = await client.post(self.endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"meta cloud api unreachable: {exc}",
                provider=self.name,
            ) from exc
        return _parse_send_response(response, provider_name=self.name)

    async def get_status(self, provider_msg_id: str) -> str:
        """Return a normalised status string for a previously sent message.

        Meta exposes per-message status through the
        ``/{phone_number_id}/messages`` endpoint; the MVP keeps
        things simple and treats any successful response as
        ``sent``. A future iteration will consume the
        ``conversation.status`` field.
        """
        if not provider_msg_id:
            raise ProviderValidationError(
                "provider_msg_id is required",
                provider=self.name,
            )
        client = await self._get_client()
        url = f"{self._api_base}/{self._api_version}/{self._phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        params = {"ids": provider_msg_id}
        try:
            response = await client.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"meta cloud api unreachable: {exc}",
                provider=self.name,
            ) from exc
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"meta cloud api returned {response.status_code}",
                provider=self.name,
            )
        if response.status_code == 429:
            raise ProviderRateLimitError(
                "meta cloud api rate limited",
                provider=self.name,
            )
        if response.status_code >= 400:
            raise ProviderValidationError(
                f"meta cloud api rejected status check: {_truncate(response.text)}",
                provider=self.name,
            )
        return "sent"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_send_response(response: httpx.Response, *, provider_name: str) -> SendResult:
    """Translate an :class:`httpx.Response` into a :class:`SendResult`.

    Centralised so the ``send`` method stays short and the
    status-code-to-error mapping is the same for the
    ``/messages`` and any future endpoints.
    """
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
    messages = data.get("messages") or []
    if not messages:
        raise ProviderUnavailableError(
            f"{provider_name} returned an empty messages array: {data!r}",
            provider=provider_name,
        )
    provider_msg_id = str(messages[0].get("id") or "")
    if not provider_msg_id:
        raise ProviderUnavailableError(
            f"{provider_name} response missing message id: {data!r}",
            provider=provider_name,
        )
    return SendResult(
        provider_msg_id=provider_msg_id, raw=data, provider_name=provider_name
    )


def _truncate(value: str, *, limit: int = 200) -> str:
    """Trim a string to a single line and a bounded length.

    Keeps error messages short so the response body never
    carries a multi-page stack trace from the upstream.
    """
    single = value.splitlines()[0] if value else ""
    return single[:limit]


__all__ = (
    "DEFAULT_GRAPH_API_VERSION",
    "DEFAULT_TIMEOUT_SECONDS",
    "MetaWhatsAppProvider",
)
