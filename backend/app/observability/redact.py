"""PII redaction helpers for logs, error messages and audit trails.

The platform logs every outbound message for billing and debugging
purposes. Phone numbers and Chilean RUTs are personally identifiable
information and **must never appear in clear text in any log
output**: the PRD (§"Seguridad") and CODING_STANDARDS.md §9 both
require that logs carry the *minimum* information needed to triage
an incident, and that PII is replaced with a stable, irreversible
token.

The helpers below provide two operations:

- :func:`hash_phone` / :func:`hash_rut` – deterministic, salted
  SHA-256 digests suitable for correlating two log lines that refer
  to the same customer without exposing the underlying identifier.
- :func:`mask_phone` / :func:`mask_rut` – human-friendly masks that
  keep the last few digits visible so an operator can still
  eyeball "is this the right number?" without the full value ever
  hitting the log stream.

The default salt is read from :class:`app.config.Settings` so a
deployment can rotate it (and therefore invalidate every prior
hash) without a code change. The salt is **not** a secret in the
cryptographic sense – it exists only to prevent trivial rainbow
tables from mapping hashes back to phone numbers. Treat the
mapping as pseudonymous, not anonymous.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# A Chilean phone number, in any of the shapes we accept at the API
# edge: ``+56 9 1234 5678``, ``+56912345678``, ``+56-9-1234-5678``,
# ``56912345678`` or the bare 9-digit mobile ``912345678``. We
# normalise to ``+56...`` before hashing / masking so every
# representation of the same number produces the same token.
_PHONE_RE = re.compile(r"^\+?56[\s\-]?0?(9[\s\-]?\d{4}[\s\-]?\d{4})$|^(9\d{8})$")

# A Chilean RUT: a body of 1–8 digits, an optional ``.000``
# thousands separator, the check digit (0–9 or ``K`` / ``k``). We
# require an explicit dash between the body and the check digit
# so the ambiguous ``12345678`` (which could be parsed as body
# ``1234567`` + dv ``8``) is rejected. The body itself may use
# dot separators (``12.345.678``) or none (``12345678``).
_RUT_WITH_DV_RE = re.compile(r"^(\d{1,8}|\d{1,2}\.\d{3}\.\d{3})\-([0-9Kk])$")

# A redacted token: 8 hex characters is enough for a log correlation
# id (16M values) while staying short enough to scan visually.
_HASH_PREFIX = "phn_"
_RUT_HASH_PREFIX = "rut_"


@dataclass(frozen=True)
class RedactionResult:
    """The outcome of a redaction call.

    ``token`` is what the caller should write to the log. ``redacted``
    flags whether the input was recognised and a transformation was
    applied; the caller can branch on it (e.g. emit a warning when
    something that *should* have been a phone number was not
    recognised by the pattern).
    """

    token: str
    redacted: bool


# ---------------------------------------------------------------------------
# Settings access (kept lazy so importing this module is cheap)
# ---------------------------------------------------------------------------


def _salt(settings: Settings | None) -> str:
    """Return the salt used by the hash helpers.

    Defaults to the runtime ``secret_key``; that key is rotated per
    environment so a dev log never collides with a production log
    even when both hash the same number.
    """
    return (settings or get_settings()).secret_key


# ---------------------------------------------------------------------------
# Phone numbers
# ---------------------------------------------------------------------------


def normalise_phone(value: str) -> str | None:
    """Return the canonical ``+56...`` representation of ``value``.

    Returns ``None`` when the value does not match any of the
    accepted formats. The function never raises – the caller is
    expected to branch on the return value.
    """
    if not isinstance(value, str):  # defensive: callers may pass bytes
        return None
    match = _PHONE_RE.match(value.strip())
    if match is None:
        return None
    # The first capture group holds ``9XXXXXXX`` (with the
    # leading 9 already included) for the ``+56...`` shape; the
    # second capture group covers the bare 9-digit mobile shape.
    # One of the two is always populated because the regex
    # alternatives are mutually exclusive.
    digits = match.group(1) or match.group(2)
    digits = digits.replace(" ", "").replace("-", "")
    return f"+56{digits}"


def hash_phone(value: str, *, settings: Settings | None = None) -> RedactionResult:
    """Return a stable SHA-256 digest for ``value``.

    Inputs that do not look like a Chilean phone number pass
    through unchanged (``RedactionResult.redacted is False``) so the
    caller can decide how to handle them.
    """
    canonical = normalise_phone(value)
    if canonical is None:
        return RedactionResult(token=value, redacted=False)
    digest = hashlib.sha256((_salt(settings) + canonical).encode("utf-8")).hexdigest()[:8]
    return RedactionResult(token=f"{_HASH_PREFIX}{digest}", redacted=True)


def mask_phone(value: str, *, visible_tail: int = 4) -> RedactionResult:
    """Replace all but the last ``visible_tail`` digits with ``*``.

    Useful for operator-facing dashboards where partial visibility
    is acceptable. ``visible_tail`` is clamped to ``[0, len(digits)]``
    to avoid leaking more digits than the input carries.
    """
    canonical = normalise_phone(value)
    if canonical is None:
        return RedactionResult(token=value, redacted=False)
    digits = canonical.lstrip("+")
    tail = max(0, min(visible_tail, len(digits)))
    if tail == 0:
        visible = ""
    else:
        visible = digits[-tail:]
    masked = "*" * (len(digits) - tail) + visible
    # Keep the ``+56`` prefix so the masked token is still recognisable
    # as a Chilean number on the screen.
    return RedactionResult(
        token=f"+{masked}",
        redacted=True,
    )


# ---------------------------------------------------------------------------
# Chilean RUT
# ---------------------------------------------------------------------------


def normalise_rut(value: str) -> str | None:
    """Return the canonical ``<body>-<dv>`` representation of ``value``.

    The body is zero-padded to 8 digits so the same number always
    hashes to the same digest regardless of the input shape
    (``1234567-5`` and ``12.345.678-5`` are the same RUT).

    The legacy concatenated form ``123456785`` (body + check digit
    with no separator) is also accepted, but only when the body
    has exactly 8 digits – that is the unambiguous modern RUT
    shape. Shorter concatenated forms are rejected because they
    collide with bare bodies.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip().upper()
    match = _RUT_WITH_DV_RE.match(cleaned)
    if match is not None:
        body = match.group(1).replace(".", "")
        dv = match.group(2).upper()
        return f"{int(body):08d}-{dv}"
    # Fallback: the legacy concatenated form. We require the
    # body to be exactly 8 digits so ``123456785`` (body 12345678,
    # dv 5) is accepted but ``12345678`` (no dv) is rejected.
    if cleaned.isdigit() and len(cleaned) == 9:
        body = cleaned[:-1]
        dv = cleaned[-1]
        return f"{int(body):08d}-{dv}"
    return None


def hash_rut(value: str, *, settings: Settings | None = None) -> RedactionResult:
    """Stable SHA-256 digest for a Chilean RUT.

    Mirrors :func:`hash_phone` so the two helpers share a contract:
    a value that does not look like a RUT passes through unchanged
    with ``redacted=False``.
    """
    canonical = normalise_rut(value)
    if canonical is None:
        return RedactionResult(token=value, redacted=False)
    digest = hashlib.sha256((_salt(settings) + canonical).encode("utf-8")).hexdigest()[:8]
    return RedactionResult(token=f"{_RUT_HASH_PREFIX}{digest}", redacted=True)


def mask_rut(value: str) -> RedactionResult:
    """Mask the body of a RUT, keeping the check digit visible.

    Operators still need the check digit to disambiguate RUTs they
    have on file (the digit is the only piece of information that
    makes ``12.345.678`` unique among the issued range), so we
    never mask it.
    """
    canonical = normalise_rut(value)
    if canonical is None:
        return RedactionResult(token=value, redacted=False)
    body, _, dv = canonical.partition("-")
    masked_body = body[:-2] + "**" if len(body) > 2 else "**"
    return RedactionResult(
        token=f"{masked_body}-{dv}",
        redacted=True,
    )
