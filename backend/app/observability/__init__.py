"""Observability helpers: structured logging, metrics, PII redaction.

Modules in this package:

- ``redact.py``  – phone-number & RUT hash / mask helpers (see
  :mod:`app.observability.redact`).
- ``logging.py`` – root-logger configuration and a thin
  :func:`get_logger` wrapper (see :mod:`app.observability.logging`).

Modules planned for this package:

- ``metrics.py`` – Prometheus counters / histograms.

These helpers are intentionally kept out of the request-handling
path so they can be unit-tested in isolation and reused by Arq
workers and CLI scripts.
"""

from app.observability.logging import configure_logging, get_logger
from app.observability.redact import (
    RedactionResult,
    hash_phone,
    hash_rut,
    mask_phone,
    mask_rut,
    normalise_phone,
    normalise_rut,
)

__all__ = (
    "RedactionResult",
    "configure_logging",
    "get_logger",
    "hash_phone",
    "hash_rut",
    "mask_phone",
    "mask_rut",
    "normalise_phone",
    "normalise_rut",
)
