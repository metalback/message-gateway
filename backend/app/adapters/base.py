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
    """

    provider_msg_id: str
    raw: dict[str, Any]


class BaseProvider(ABC):
    """Provider contract every adapter must satisfy."""

    name: str

    @abstractmethod
    async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
        """Deliver a single message; raise on transport / validation errors."""

    @abstractmethod
    async def get_status(self, provider_msg_id: str) -> str:
        """Return a normalised status string for a previously sent message."""
