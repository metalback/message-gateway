"""Flow (Chilean payment gateway) adapter.

Flow is the platform's payment processor: it fronts Webpay,
Onepay and the major card networks for the Chilean market.
The adapter hides the underlying HTTP details behind two
methods:

- :meth:`FlowClient.create_order` – mint a new payment
  order; the response carries the ``token`` the customer
  uses to complete the payment on Flow's hosted page.
- :meth:`FlowClient.get_status` – poll Flow for the
  current state of a previously created order. Used by the
  dashboard when the asynchronous ``payment/confirm``
  webhook has not arrived yet (or the customer closed the
  tab before the redirect could fire).

The HTTP calls are wrapped in :class:`FlowError` and its
subclasses so the route layer can map a provider outage
to a 503 without leaking Flow's internal status codes.
Every method takes a ``settings`` keyword so unit tests
can swap the sandbox base URL for an in-memory mock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.observability import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FlowError(Exception):
    """Base class for every Flow-domain exception."""

    http_status: int = 502

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FlowUnavailableError(FlowError):
    """Flow is unreachable or returned a transport-level error."""

    http_status = 503


class FlowRejectionError(FlowError):
    """Flow accepted the request but rejected it (validation, fraud, ...)."""

    http_status = 422


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowOrder:
    """Outcome of :meth:`FlowClient.create_order`.

    ``token`` is what the customer uses to complete the
    payment on Flow's hosted page; ``redirect_url`` is the
    convenience URL the platform can hand to the dashboard
    so the user is one click away from the Webpay
    checkout. ``raw_json`` is the verbatim Flow response –
    stored in the :class:`Payment.flow_response` column
    for audit purposes.
    """

    token: str
    redirect_url: str
    raw_json: str


@dataclass(frozen=True)
class FlowPaymentStatus:
    """Outcome of :meth:`FlowClient.get_status`.

    ``status`` is Flow's numeric state machine:

    - ``1`` – pending (the customer has not paid yet)
    - ``2`` – paid (the funds cleared)
    - ``3`` – rejected (the issuer declined the card)
    - ``4`` – cancelled (the customer closed the tab)
    - ``5`` – expired (the order timed out)
    """

    status: int
    payment_id: str | None
    raw_json: str


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class FlowClient:
    """Thin async wrapper around the Flow REST API.

    The class is intentionally stateless: every call opens
    a short-lived :class:`httpx.AsyncClient` so the adapter
    does not have to manage a connection pool. The MVP
    volume (one create + one status call per invoice) does
    not warrant the complexity.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._base_url = self._settings.flow_base_url.rstrip("/")

    async def create_order(
        self,
        *,
        commerce_order: str,
        subject: str,
        amount_clp: int,
        email: str,
    ) -> FlowOrder:
        """Create a new Flow order and return the token + redirect URL.

        The function builds the same payload Flow's
        ``/payment/create`` endpoint expects: a free-form
        ``commerce_order`` (the platform's own invoice
        id), a human-readable subject, the amount in CLP
        and the customer's e-mail address. The
        ``confirmation`` / ``return`` URLs come from
        :class:`Settings` so a deployment can swap them
        per environment.
        """
        if not isinstance(commerce_order, str) or not commerce_order:
            raise FlowRejectionError("invalid_commerce_order", "commerce_order is required")
        if not isinstance(subject, str) or not subject:
            raise FlowRejectionError("invalid_subject", "subject is required")
        if not isinstance(amount_clp, int) or amount_clp <= 0:
            raise FlowRejectionError("invalid_amount", "amount_clp must be a positive integer")
        if not isinstance(email, str) or not email:
            raise FlowRejectionError("invalid_email", "email is required")

        payload = {
            "apiKey": self._settings.flow_api_key,
            "commerceOrder": commerce_order,
            "subject": subject,
            "amount": amount_clp,
            "email": email,
            "urlConfirmation": self._settings.flow_webhook_url,
            "urlReturn": self._settings.flow_confirmation_url,
            "optional": {"environment": self._settings.flow_environment},
        }
        response = await self._post("/payment/create", payload)
        token = self._extract_string(response, "token")
        redirect_url = self._extract_string(response, "redirectUrl")
        # The raw payload is the response Flow returns; we
        # serialise it once here so :class:`Payment` can
        # store it in the :attr:`flow_response` column.
        import json

        raw_json = json.dumps(response, ensure_ascii=False)
        logger.info(
            "flow.order.created",
            extra={
                "commerce_order": commerce_order,
                "flow_token": token,
                "amount_clp": amount_clp,
            },
        )
        return FlowOrder(token=token, redirect_url=redirect_url, raw_json=raw_json)

    async def get_status(self, *, token: str) -> FlowPaymentStatus:
        """Poll Flow for the current status of ``token``.

        The endpoint is idempotent: calling it on an
        already-paid order returns ``status=2`` and does
        not charge the customer again.
        """
        if not isinstance(token, str) or not token:
            raise FlowRejectionError("invalid_token", "token is required")
        payload = {
            "apiKey": self._settings.flow_api_key,
            "token": token,
        }
        response = await self._post("/payment/getStatus", payload)
        try:
            status = int(response.get("status", 0))
        except (TypeError, ValueError) as exc:
            raise FlowUnavailableError(
                "invalid_status",
                f"Flow returned a non-numeric status: {response.get('status')!r}",
            ) from exc
        payment_id_raw = response.get("paymentId")
        payment_id = str(payment_id_raw) if payment_id_raw is not None else None
        import json

        raw_json = json.dumps(response, ensure_ascii=False)
        return FlowPaymentStatus(status=status, payment_id=payment_id, raw_json=raw_json)

    # --- internals -------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``payload`` to Flow and return the decoded JSON.

        A non-2xx response, a transport-level error or a
        non-JSON body all surface as
        :class:`FlowUnavailableError` so the caller can
        branch on a single exception type.
        """
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise FlowUnavailableError(
                "flow_unreachable",
                f"could not reach Flow at {url}: {exc!s}",
            ) from exc
        if response.status_code >= 500:
            raise FlowUnavailableError(
                "flow_server_error",
                f"Flow returned HTTP {response.status_code}",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise FlowUnavailableError(
                "flow_invalid_response",
                f"Flow returned a non-JSON body: {response.text!r}",
            ) from exc
        if response.status_code >= 400:
            raise FlowRejectionError(
                str(data.get("code", "flow_rejected")),
                str(data.get("message", "Flow rejected the request")),
            )
        if not isinstance(data, dict):
            raise FlowUnavailableError(
                "flow_invalid_response",
                "Flow response is not a JSON object",
            )
        return data

    @staticmethod
    def _extract_string(payload: dict[str, Any], key: str) -> str:
        """Return the string value of ``key`` or raise :class:`FlowRejectionError`."""
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise FlowRejectionError(
                "flow_missing_field",
                f"Flow response is missing a {key!r} field",
            )
        return value


__all__ = (
    "FlowClient",
    "FlowError",
    "FlowOrder",
    "FlowPaymentStatus",
    "FlowRejectionError",
    "FlowUnavailableError",
)
