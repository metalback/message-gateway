"""HTTP client for outbound delivery-receipt POSTs.

This module owns the network side of the webhook feature
(issue #5):

- :class:`WebhookDeliveryClient` – the seam the service
  layer uses to fire an outbound POST. The class is a
  thin wrapper over :func:`httpx.AsyncClient.post` with
  bounded exponential back-off so a flaky customer
  endpoint does not consume the worker's quota.

- :class:`WebhookDeliveryResult` – the per-attempt
  outcome the service layer hands back to the caller for
  audit / persistence.

The module is intentionally tiny: every helper that does
I/O lives here so the rest of the service layer can be
tested with an in-memory fake.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from app.observability import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookDeliveryResult:
    """Outcome of a single :meth:`WebhookDeliveryClient.deliver` call.

    ``succeeded``      – whether the receiver returned a
                         2xx (or 3xx that we followed
                         successfully).
    ``attempts``       – number of HTTP requests the
                         client fired (1..max_attempts).
    ``status_code``    – the last HTTP status code the
                         receiver returned (``None`` if
                         every attempt failed at the
                         transport layer).
    ``response_body``  – the last response body, truncated
                         to 500 characters so a misbehaving
                         receiver cannot blow up the
                         database row.
    ``error``          – the last error message
                         (``None`` on success).
    """

    succeeded: bool
    attempts: int
    status_code: int | None
    response_body: str | None
    error: str | None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class WebhookDeliveryClient:
    """Async client that POSTs a delivery receipt to a
    customer-configured URL with bounded exponential back-off.

    The class is constructed once per worker process; the
    underlying :class:`httpx.AsyncClient` is created lazily
    on the first call so the service layer does not have
    to manage a "client is open" lifecycle.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        max_attempts: int = 5,
        backoff_base_seconds: float = 1.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be > 0")
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return the cached :class:`httpx.AsyncClient`,
        creating it on first use.

        Lazy construction mirrors the pattern
        :func:`app.redis_client.get_redis_client` uses:
        tests can instantiate the class without a network
        handle, and the first ``deliver()`` call pays the
        cost of the connect pool.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying :class:`httpx.AsyncClient`.

        Exposed so the worker (and the unit tests) can
        shut the pool down deterministically. A
        ``finally`` block in the worker's main loop is
        the right place to call it.
        """
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def deliver(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> WebhookDeliveryResult:
        """POST ``body`` to ``url`` with bounded retry.

        The function never raises: a transport error is
        captured on the returned
        :class:`WebhookDeliveryResult` so the service
        layer can persist the outcome without having to
        wrap every call in a try / except.
        """
        client = await self._ensure_client()
        attempts = 0
        last_status: int | None = None
        last_body: str | None = None
        last_error: str | None = None
        for attempt in range(1, self._max_attempts + 1):
            attempts = attempt
            try:
                response = await client.post(url, content=body, headers=headers)
                last_status = response.status_code
                last_body = response.text[:500] if response.text else None
                if 200 <= response.status_code < 400:
                    return WebhookDeliveryResult(
                        succeeded=True,
                        attempts=attempts,
                        status_code=response.status_code,
                        response_body=last_body,
                        error=None,
                    )
                last_error = f"http_{response.status_code}"
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:200]
                logger.warning(
                    "webhook_delivery_attempt_failed",
                    extra={
                        "url": url,
                        "attempt": attempt,
                        "max_attempts": self._max_attempts,
                        "error": last_error,
                    },
                )
            if attempt < self._max_attempts:
                # Exponential back-off: 1s, 2s, 4s, 8s, 16s …
                # Capped implicitly by ``max_attempts`` so a
                # long-running worker does not stall forever.
                wait = self._backoff_base_seconds * (2 ** (attempt - 1))
                await asyncio.sleep(wait)
        return WebhookDeliveryResult(
            succeeded=False,
            attempts=attempts,
            status_code=last_status,
            response_body=last_body,
            error=last_error,
        )


__all__ = ("WebhookDeliveryClient", "WebhookDeliveryResult")
