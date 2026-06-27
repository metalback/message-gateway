"""Provider-specific error types.

Concrete adapters raise :class:`ProviderError` (or one of its
subclasses) when the upstream behaves unexpectedly. The route
layer maps the subclass to the matching HTTP status so a
caller never has to know which provider surfaced the failure.

The split between :class:`ProviderUnavailableError`,
:class:`ProviderValidationError` and
:class:`ProviderRateLimitError` matches the three failure modes
the PRD calls out:

- *unavailable*  – the upstream is down, times out or returns
                   a 5xx. Retryable; the worker would reschedule.
- *validation*   – the upstream rejected the message shape
                   (e.g. invalid phone number, template not
                   approved). Permanent; retrying would fail the
                   same way.
- *rate_limit*   – the upstream throttled the platform. Retryable
                   after a back-off; the worker would honour the
                   retry-after header.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for every provider-domain exception.

    The route layer converts subclasses of this exception into a
    uniform HTTP response so the rest of the platform does not
    have to know which provider surfaced the failure.
    """

    http_status: int = 502
    code: str = "provider_error"

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider


class ProviderUnavailableError(ProviderError):
    """The upstream is unreachable or returned a 5xx response.

    The HTTP mapping is ``502 Bad Gateway`` because the platform
    itself is healthy but cannot fulfil the request through the
    configured provider.
    """

    http_status = 502
    code = "provider_unavailable"


class ProviderValidationError(ProviderError):
    """The upstream rejected the request because of an invalid
    argument (``400``-class response).

    The HTTP mapping is ``422 Unprocessable Entity`` because the
    request was well-formed at the platform edge but the
    provider's own validator rejected it. Retrying without
    fixing the input would fail the same way.
    """

    http_status = 422
    code = "provider_validation"


class ProviderRateLimitError(ProviderError):
    """The upstream throttled the platform (``429`` response).

    The HTTP mapping is ``429 Too Many Requests``; the worker
    uses the ``retry_after`` hint to back off.
    """

    http_status = 429
    code = "provider_rate_limited"

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after = retry_after


__all__ = (
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderUnavailableError",
    "ProviderValidationError",
)
