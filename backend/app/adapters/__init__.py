"""Provider adapters (Adapter Pattern).

The platform integrates with multiple SMS / WhatsApp providers;
each implementation lives behind the :class:`BaseProvider`
interface declared in ``app.adapters.base``. The router service
selects the right adapter per client / channel / fallback policy.

Adding a new provider is a three-step change:

1. Implement :class:`BaseProvider` in a new module.
2. Register it in ``app.adapters.registry``.
3. Reference it from configuration (``Settings``).
"""

from app.adapters.base import BaseProvider, SendResult
from app.adapters.flow import (
    FlowClient,
    FlowError,
    FlowOrder,
    FlowPaymentStatus,
    FlowRejectionError,
    FlowUnavailableError,
)

__all__ = (
    "BaseProvider",
    "FlowClient",
    "FlowError",
    "FlowOrder",
    "FlowPaymentStatus",
    "FlowRejectionError",
    "FlowUnavailableError",
    "SendResult",
)
