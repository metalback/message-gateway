"""Unit tests for :mod:`app.observability.logging`.

The logging module owns two contracts:

- :func:`configure_logging` installs a single, identifiable
  handler on the root logger and applies the level declared in
  :class:`app.config.Settings`.
- :func:`get_logger` returns a :mod:`logging` logger so call
  sites can use a single import.

Both are exercised here without spinning up the full FastAPI
app: the tests install / remove the configuration in isolation
so a failure in one case cannot leak into the next.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.config import Settings
from app.observability import configure_logging, get_logger
from app.observability import logging as logging_module

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_handlers(logger: logging.Logger) -> list[logging.Handler]:
    """Return the handlers this module installed on ``logger``.

    The configuration is idempotent, so a re-installation leaves
    *one* project handler behind. We identify it via the
    ``_msg_gateway`` sentinel we stamp on the formatter – using
    a class attribute is more robust than matching on the stream
    type, which other libraries (e.g. uvicorn) might also use.
    """
    out: list[logging.Handler] = []
    for handler in logger.handlers:
        fmt = getattr(handler, "formatter", None)
        if fmt is not None and getattr(fmt, "_msg_gateway", False):
            out.append(handler)
    return out


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Any:
    """Snapshot the root logger around each test.

    The tests below mutate ``logging.getLogger()``'s handlers
    and level; restoring them keeps the rest of the suite
    isolated from a misbehaving case.

    We detach every handler *before* the test runs (not just
    save / restore), so an assertion like "no project handlers
    exist" can succeed regardless of what the previous test
    left behind. Other libraries' handlers (e.g. uvicorn's)
    are preserved verbatim and re-attached in the teardown.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    # Detach everything so the test starts from a known state.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    try:
        yield
    finally:
        # Re-attach the original handler set verbatim, dropping
        # any handler the test may have left behind.
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------


def test_configure_logging_installs_handler() -> None:
    """A fresh configuration adds exactly one project handler to
    the root logger so the very first log line after startup is
    routed through the project's formatter."""
    root = logging.getLogger()
    assert _project_handlers(root) == []

    configure_logging(Settings(log_level="info"))

    installed = _project_handlers(root)
    assert len(installed) == 1


def test_configure_logging_is_idempotent() -> None:
    """Calling :func:`configure_logging` twice must not stack
    duplicate handlers – every module that runs in ``create_app``
    (uvicorn, FastAPI, the app itself) calls into logging at
    import time, and we don't want a single startup to install
    N copies of the same handler."""
    configure_logging(Settings(log_level="info"))
    configure_logging(Settings(log_level="info"))

    assert len(_project_handlers(logging.getLogger())) == 1


def test_configure_logging_applies_settings_level() -> None:
    """The configured level must match ``Settings.log_level`` so
    setting ``LOG_LEVEL=debug`` in the environment immediately
    surfaces debug messages without a redeploy."""
    configure_logging(Settings(log_level="debug"))
    assert logging.getLogger().level == logging.DEBUG

    configure_logging(Settings(log_level="warning"))
    assert logging.getLogger().level == logging.WARNING


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("debug", logging.DEBUG),
        ("INFO", logging.INFO),
        ("WaRnInG", logging.WARNING),
        ("error", logging.ERROR),
        ("critical", logging.CRITICAL),
    ],
)
def test_configure_logging_normalises_level_strings(raw: str, expected: int) -> None:
    """The textual level in ``Settings.log_level`` is
    case-insensitive and accepts the canonical ``logging``
    names. Contributors that set ``LOG_LEVEL=Info`` in the env
    file (capitalised for readability) must not silently get a
    missing-logger behaviour."""
    configure_logging(Settings(log_level=raw))
    assert logging.getLogger().level == expected


def test_configure_logging_falls_back_on_unknown_level() -> None:
    """An unrecognised level (typo, removed name) must not crash
    the app; instead the root logger falls back to ``INFO`` and
    emits a warning so the misconfiguration is visible in the
    very first log line."""
    configure_logging(Settings(log_level="not-a-level"))
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_preserves_existing_handlers() -> None:
    """We only own the handler this module installs. Other
    libraries (e.g. uvicorn) attach their own handlers to the
    root logger and we must not detach them – uvicorn's access
    log is a useful operational signal and we don't want to
    surprise an operator with a silent access log on the
    first :func:`configure_logging` call after a refactor."""
    root = logging.getLogger()
    foreign = logging.NullHandler()  # stand-in for "uvicorn's handler"
    root.addHandler(foreign)
    try:
        configure_logging(Settings(log_level="info"))
        # Foreign handler is still attached, ours is also there.
        assert foreign in root.handlers
        assert len(_project_handlers(root)) == 1
    finally:
        root.removeHandler(foreign)


def test_configure_logging_uses_settings_when_none_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling :func:`configure_logging` with no argument must
    defer to :func:`app.config.get_settings` – the same code
    path ``create_app`` uses, so the level matches the one a
    process inherits from its environment."""
    settings = Settings(log_level="error")

    def _fake_get_settings() -> Settings:
        return settings

    monkeypatch.setattr(logging_module, "get_settings", _fake_get_settings)

    configure_logging()
    assert logging.getLogger().level == logging.ERROR


# ---------------------------------------------------------------------------
# get_logger
# ---------------------------------------------------------------------------


def test_get_logger_returns_named_logger() -> None:
    """The returned object must be a real :mod:`logging` logger
    so ``logger.info(...)`` works without any extra wiring."""
    logger = get_logger("app.observability.logging.test")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "app.observability.logging.test"


def test_get_logger_returns_same_instance_for_same_name() -> None:
    """:mod:`logging` caches loggers by name; the helper must
    not break that contract (a fresh object per call would
    defeat handler inheritance)."""
    a = get_logger("app.observability.logging.shared")
    b = get_logger("app.observability.logging.shared")
    assert a is b


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def test_logging_helpers_exported_from_package() -> None:
    """The two public helpers are part of the package's public
    surface (re-exported from :mod:`app.observability`) so
    callers can ``from app.observability import configure_logging``."""
    from app import observability

    assert observability.configure_logging is configure_logging
    assert observability.get_logger is get_logger
