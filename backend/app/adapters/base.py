"""``BaseProvider`` interface for SMS / WhatsApp adapters.

Every concrete provider (Meta Cloud API, local SMS aggregator,
Twilio fallback) implements this abstract class. The router
service selects which adapter to invoke based on channel,
client configuration and runtime health.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SendResult:
    """Result of a successful ``send`` call.

    ``provider_msg_id`` is the identifier the upstream assigned
    to the message; we persist it so subsequent status checks
    can be correlated.

    ``provider_name`` is the name of the *underlying* adapter
    that actually delivered the message. It is optional so the
    existing single-provider adapters do not have to be
    touched; the field is set by :class:`BaseProvider.send`
    implementations (and by the failover router, which may
    switch providers mid-call) so the messaging service can
    record which provider handled the request. A ``None``
    value is treated as "use the caller-provided provider's
    name" by the service layer.
    """

    provider_msg_id: str
    raw: dict[str, Any]
    provider_name: str | None = None


class BaseProvider(ABC):
    """Provider contract every adapter must satisfy."""

    name: str

    @abstractmethod
    async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
        """Deliver a single message; raise on transport / validation errors."""

    @abstractmethod
    async def get_status(self, provider_msg_id: str) -> str:
        """Return a normalised status string for a previously sent message."""
