"""Provider adapters (Adapter Pattern).

The platform integrates with multiple SMS / WhatsApp providers;
each implementation lives behind the :class:`BaseProvider`
interface declared in ``app.adapters.base``. The router service
selects the right adapter per client / channel / fallback policy.

Adding a new provider is a three-step change:

1. Implement :class:`BaseProvider` in a new module.
2. Register it in ``app.adapters.registry``.
3. Reference it from configuration (``Settings``).

Modules in this package:

- ``base.py``             – the abstract :class:`BaseProvider` and
                            the :class:`SendResult` dataclass.
- ``errors.py``           – provider-specific exception types
                            (unavailable, validation, rate limit).
- ``meta_whatsapp.py``    – Meta Cloud API adapter (WhatsApp).
- ``sms_aggregator.py``   – local Chilean SMS aggregator.
- ``registry.py``         – channel → adapter factory mapping.
"""

from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.adapters.flow import (
    FlowClient,
    FlowError,
    FlowOrder,
    FlowPaymentStatus,
    FlowRejectionError,
    FlowUnavailableError,
)
from app.adapters.registry import (
    UnsupportedChannelError,
    get_provider,
    supported_channels,
)

__all__ = (
    "BaseProvider",
    "FlowClient",
    "FlowError",
    "FlowOrder",
    "FlowPaymentStatus",
    "FlowRejectionError",
    "FlowUnavailableError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderUnavailableError",
    "ProviderValidationError",
    "SendResult",
    "UnsupportedChannelError",
    "get_provider",
    "supported_channels",
)
