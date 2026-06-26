"""Unit tests for the PII redaction helpers.

The helpers live in :mod:`app.observability.redact`; the tests
assert the *observable* contract (token shape, determinism,
non-recognition behaviour) without exposing the salt or the hash
internals.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.observability import (
    RedactionResult,
    hash_phone,
    hash_rut,
    mask_phone,
    mask_rut,
    normalise_phone,
    normalise_rut,
)

# ---------------------------------------------------------------------------
# normalise_phone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+56912345678", "+56912345678"),
        ("+56 9 1234 5678", "+56912345678"),
        ("+56-9-1234-5678", "+56912345678"),
        ("56912345678", "+56912345678"),
        ("912345678", "+56912345678"),
    ],
)
def test_normalise_phone_accepts_canonical_shapes(raw: str, expected: str) -> None:
    """The same phone number, written in any of the formats the
    API edge accepts, must collapse to the same canonical form so
    downstream hashes / masks line up."""
    assert normalise_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not a phone",
        "+1234567890",  # wrong country code
        "12345",  # too short
        "+56 2 2345 6789",  # landline, not mobile
    ],
)
def test_normalise_phone_returns_none_for_unrecognised_inputs(
    raw: str,
) -> None:
    """Inputs that do not match a Chilean mobile number must
    return ``None`` so the caller can branch on the result instead
    of relying on a best-effort guess."""
    assert normalise_phone(raw) is None


def test_normalise_phone_handles_non_string_input() -> None:
    """Defensive: the helper is called from logging paths that
    may pass arbitrary objects. A non-string must not raise – it
    just returns ``None`` so the caller can fall through to a
    generic log line."""
    assert normalise_phone(12345) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# hash_phone
# ---------------------------------------------------------------------------


def test_hash_phone_is_deterministic() -> None:
    """The same input under the same salt must produce the same
    token; that's what makes the hash usable as a log correlation
    id across two requests."""
    settings = Settings(secret_key="fixed-salt")
    a = hash_phone("+56912345678", settings=settings)
    b = hash_phone("+56912345678", settings=settings)
    assert a == b
    assert a.redacted is True
    assert a.token.startswith("phn_")
    assert len(a.token) == len("phn_") + 8


def test_hash_phone_collapses_input_shapes() -> None:
    """Two surface forms of the same number must hash to the same
    token. This is the property that lets a support engineer
    correlate ``+56 9 1234 5678`` in one log line with
    ``+56912345678`` in another without ever seeing the digits."""
    settings = Settings(secret_key="fixed-salt")
    a = hash_phone("+56 9 1234 5678", settings=settings)
    b = hash_phone("+56912345678", settings=settings)
    assert a == b


def test_hash_phone_changes_with_salt() -> None:
    """A salt rotation must invalidate the previously emitted
    tokens. This is what the security model relies on: rotating
    ``SECRET_KEY`` in production is enough to make the next batch
    of logs uncorrelatable with the old ones (and vice-versa)."""
    a = hash_phone("+56912345678", settings=Settings(secret_key="salt-a"))
    b = hash_phone("+56912345678", settings=Settings(secret_key="salt-b"))
    assert a != b


def test_hash_phone_passthrough_for_unrecognised_input() -> None:
    """When the input is not a phone number, the helper returns it
    unchanged with ``redacted=False`` so the caller can decide how
    to log the value (or emit a warning)."""
    result = hash_phone("not a phone")
    assert result.redacted is False
    assert result.token == "not a phone"


# ---------------------------------------------------------------------------
# mask_phone
# ---------------------------------------------------------------------------


def test_mask_phone_keeps_country_code_and_tail() -> None:
    """Operators still need to recognise the country code and the
    last few digits, so the mask keeps both visible."""
    result = mask_phone("+56912345678", visible_tail=4)
    assert result.redacted is True
    # Canonical form is ``+56912345678`` (11 digits). With a tail
    # of 4 the helper keeps the last four digits visible and
    # masks the rest.
    assert result.token == "+*******5678"


def test_mask_phone_zero_visible_tail_returns_full_mask() -> None:
    """A tail of zero is the most conservative option – the
    operator sees only the country code and the asterisk count."""
    result = mask_phone("+56912345678", visible_tail=0)
    assert result.token == "+" + "*" * 11


def test_mask_phone_clamps_visible_tail_to_input_length() -> None:
    """Asking for more visible digits than the input carries would
    leak the input verbatim. The helper clamps the tail so the
    returned token is never longer than the canonical form."""
    result = mask_phone("+56912345678", visible_tail=20)
    # 11 digits of payload (after ``+``); 11 visible, 0 masked.
    assert result.token == "+56912345678"


def test_mask_phone_passthrough_for_unrecognised_input() -> None:
    result = mask_phone("not a phone")
    assert result.redacted is False
    assert result.token == "not a phone"


# ---------------------------------------------------------------------------
# normalise_rut
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("12345678-5", "12345678-5"),
        ("12.345.678-5", "12345678-5"),
        ("123456785", "12345678-5"),  # body+dv concatenated
        ("1234567-5", "01234567-5"),  # body is zero-padded to 8 digits
    ],
)
def test_normalise_rut_accepts_canonical_shapes(raw: str, expected: str) -> None:
    """RUTs come in many shapes from upstream systems; the helper
    must collapse every shape of the *same* RUT to the same
    canonical form so the hash is stable."""
    assert normalise_rut(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not a rut",
        "12345678",  # missing check digit
        "12345678-X",  # invalid check digit
        "12345678901234-5",  # body too long
    ],
)
def test_normalise_rut_returns_none_for_unrecognised_inputs(
    raw: str,
) -> None:
    assert normalise_rut(raw) is None


def test_normalise_rut_handles_non_string_input() -> None:
    assert normalise_rut(12345) is None  # type: ignore[arg-type]


def test_normalise_rut_is_case_insensitive_on_dv() -> None:
    """The check digit is conventionally uppercase but data
    quality is not guaranteed; ``k`` must normalise to ``K`` so
    the hash is stable across casings."""
    assert normalise_rut("12345678-k") == normalise_rut("12345678-K")


# ---------------------------------------------------------------------------
# hash_rut
# ---------------------------------------------------------------------------


def test_hash_rut_is_deterministic() -> None:
    settings = Settings(secret_key="fixed-salt")
    a = hash_rut("12345678-5", settings=settings)
    b = hash_rut("12.345.678-5", settings=settings)
    assert a == b
    assert a.redacted is True
    assert a.token.startswith("rut_")
    assert len(a.token) == len("rut_") + 8


def test_hash_rut_changes_with_salt() -> None:
    a = hash_rut("12345678-5", settings=Settings(secret_key="salt-a"))
    b = hash_rut("12345678-5", settings=Settings(secret_key="salt-b"))
    assert a != b


def test_hash_rut_passthrough_for_unrecognised_input() -> None:
    result = hash_rut("not a rut")
    assert result.redacted is False
    assert result.token == "not a rut"


# ---------------------------------------------------------------------------
# mask_rut
# ---------------------------------------------------------------------------


def test_mask_rut_keeps_check_digit() -> None:
    """The check digit is the only piece of information that
    disambiguates a RUT body among the issued range, so the mask
    must always keep it visible."""
    result = mask_rut("12.345.678-5")
    assert result.redacted is True
    assert result.token == "123456**-5"


def test_mask_rut_passthrough_for_unrecognised_input() -> None:
    result = mask_rut("not a rut")
    assert result.redacted is False
    assert result.token == "not a rut"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


def test_redaction_result_is_immutable() -> None:
    """The value object must be frozen so it can be safely passed
    across the logging boundary without risk of mutation."""
    result = RedactionResult(token="phn_abcdef01", redacted=True)
    with pytest.raises((AttributeError, Exception)):
        result.token = "tampered"  # type: ignore[misc]
