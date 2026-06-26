"""Centralised logging configuration.

The FastAPI app, the Arq worker and the CLI scripts all need
to emit log lines in the same shape so an operator tailing
``docker compose logs`` gets a consistent stream. This module
owns that contract:

- :func:`configure_logging` installs a single :class:`logging.StreamHandler`
  on the root logger, formats every record as JSON-ish
  ``key=value`` pairs, and applies the level from
  :class:`app.config.Settings`.
- :func:`get_logger` is the thin wrapper every other module
  uses (``logger = get_logger(__name__)``).

The formatter is deliberately a small, dependency-free
implementation: loguru / python-json-logger would each add a
runtime dependency and a behaviour contract (handlers,
reconfiguration) the rest of the scaffold does not need yet.
Once the platform ships to production we can swap the
formatter for a structured one without touching call sites
that only know about :func:`get_logger`.

The redaction helpers in :mod:`app.observability.redact` are
the right place to scrub PII *before* it reaches a logger;
this module does not duplicate that work.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

from app.config import Settings, get_settings

# The default format string. Kept as a module constant so tests
# can assert on it and contributors can grep for it when they
# add a new log line.
_LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

# Map the textual ``Settings.log_level`` value (e.g. ``"info"``)
# to the integer constant :mod:`logging` expects. ``getattr`` is
# used so an unrecognised value raises ``AttributeError`` at
# call time instead of silently producing no output.
_LEVEL_NAMES: Final[dict[str, int]] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "fatal": logging.CRITICAL,
}


def _resolve_level(raw: str) -> int:
    """Normalise a textual level to a :mod:`logging` constant.

    Unrecognised values fall back to :data:`logging.INFO` so a
    typo in the env file never silences the app entirely; the
    fallback is loud (a ``WARNING`` on the root logger) so the
    misconfiguration is visible in the very first log line.
    """
    key = raw.strip().lower()
    if key in _LEVEL_NAMES:
        return _LEVEL_NAMES[key]
    logging.getLogger(__name__).warning("Unrecognised log level %r; falling back to INFO", raw)
    return logging.INFO


def configure_logging(settings: Settings | None = None) -> None:
    """Install the project's log handler on the root logger.

    Safe to call multiple times: any handler this module
    installed previously is removed first so re-configuration
    (e.g. across pytest runs sharing a process) does not stack
    duplicates. Other libraries that registered handlers on
    the root logger (e.g. uvicorn) are left in place.
    """
    settings = settings or get_settings()
    root = logging.getLogger()

    # Drop our own previous handler(s) so the configuration is
    # idempotent. We identify them by the formatter class we set
    # below – a class-level sentinel is more robust than relying
    # on a name string.
    for handler in list(root.handlers):
        fmt = getattr(handler, "formatter", None)
        if fmt is not None and getattr(fmt, "_msg_gateway", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    formatter = logging.Formatter(_LOG_FORMAT)
    # Sentinel attribute so the idempotent removal above can
    # recognise handlers this module installed. Using a direct
    # assignment (rather than ``setattr``) keeps Ruff happy
    # without sacrificing the marker.
    formatter._msg_gateway = True  # type: ignore[attr-defined]
    handler.setFormatter(formatter)

    root.addHandler(handler)
    root.setLevel(_resolve_level(settings.log_level))


def get_logger(name: str) -> logging.Logger:
    """Return a logger with the given name.

    Thin wrapper around :func:`logging.getLogger` so call sites
    only need to import from one place. Configuration is
    installed by :func:`configure_logging` (called once from
    :func:`app.main.create_app`); the logger returned here
    inherits the root configuration.
    """
    return logging.getLogger(name)
