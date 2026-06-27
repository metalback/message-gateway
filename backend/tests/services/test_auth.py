"""Unit tests for the auth domain service.

These tests exercise the public surface of
:mod:`app.services.auth` against an in-memory SQLite database
– the same backend the route tests use, so a regression in
either layer is caught at the same fixture level.

The tests assert the *observable* contract: the rows that
land in the database, the API keys that are returned, and
the exceptions that bubble up. The bcrypt internals are not
re-derived; we only check that a freshly minted key verifies
and a corrupted one does not.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models.client import Client, ClientPlan, ClientStatus
from app.services.auth import (
    DuplicateIdentityError,
    InactiveClientError,
    InvalidApiKeyError,
    InvalidCredentialsError,
    InvalidInputError,
    authenticate_api_key,
    authenticate_login,
    decode_session_token,
    register_client,
    rotate_api_key,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_settings() -> Settings:
    """Settings with the bcrypt cost factor dropped to the floor.

    The auth tests are about behaviour, not about timing: a
    ``rounds=12`` hash costs ~250 ms and adds up over the
    hundreds of invocations in a typical pytest run. The
    minimum allowed by :class:`Settings` is ``4``; that
    keeps the suite fast without bypassing the hashing
    pipeline entirely.
    """
    return Settings(
        secret_key="test-secret",
        jwt_secret="test-jwt-secret",
        bcrypt_rounds=4,
        jwt_ttl_minutes=15,
        api_key_prefix="mgw_live_",
    )


@pytest.fixture
def good_payload() -> dict[str, str]:
    """A baseline registration payload reused across the suite."""
    return {
        "name": "Acme SpA",
        "email": "ops@acme.cl",
        "rut": "12.345.678-5",
        "password": "sup3r-secret",
    }


# ---------------------------------------------------------------------------
# register_client
# ---------------------------------------------------------------------------


async def test_register_client_persists_row_and_mints_key(
    async_session, fast_settings, good_payload
) -> None:
    """A successful registration returns a fresh row and a
    plain API key with the configured prefix; the row carries
    the bcrypt digest but **not** the plain key."""
    result = await register_client(async_session, settings=fast_settings, **good_payload)

    # The result dataclass carries the new key and the row.
    assert result.api_key.startswith("mgw_live_")
    assert len(result.api_key) > len("mgw_live_")
    assert result.client.id
    assert result.client.email == "ops@acme.cl"
    assert result.client.rut == "12345678-5"
    assert result.client.status == ClientStatus.ACTIVE
    assert result.client.plan == ClientPlan.STARTER
    assert result.client.api_key_last4 == result.api_key[-4:]

    # The plain key never makes it into the database.
    assert result.api_key not in result.client.api_key_hash

    # Re-read the row to confirm ``commit`` actually fired.
    stmt = select(Client).where(Client.id == result.client.id)
    row = (await async_session.execute(stmt)).scalar_one()
    assert row.email == good_payload["email"]


async def test_register_normalises_email_case(async_session, fast_settings, good_payload) -> None:
    """E-mail is stored lower-cased so subsequent logins are
    case-insensitive."""
    good_payload["email"] = "OPS@Acme.CL"
    result = await register_client(async_session, settings=fast_settings, **good_payload)
    assert result.client.email == "ops@acme.cl"


@pytest.mark.parametrize(
    "rut, expected",
    [
        ("12.345.678-5", "12345678-5"),
        ("12345678-5", "12345678-5"),
        ("123456785", "12345678-5"),
    ],
)
async def test_register_normalises_rut(
    async_session, fast_settings, good_payload, rut, expected
) -> None:
    """RUT is persisted in the canonical ``body-dv`` shape
    regardless of whether the request used dot separators or
    the legacy concatenated form."""
    good_payload["rut"] = rut
    result = await register_client(async_session, settings=fast_settings, **good_payload)
    assert result.client.rut == expected


async def test_register_rejects_duplicate_email(async_session, fast_settings, good_payload) -> None:
    """A second registration with the same email raises
    :class:`DuplicateIdentityError`."""
    await register_client(async_session, settings=fast_settings, **good_payload)
    with pytest.raises(DuplicateIdentityError):
        await register_client(async_session, settings=fast_settings, **good_payload)


async def test_register_rejects_duplicate_rut(async_session, fast_settings, good_payload) -> None:
    """A different email but the same RUT is still a collision."""
    await register_client(async_session, settings=fast_settings, **good_payload)
    good_payload["email"] = "another@acme.cl"
    with pytest.raises(DuplicateIdentityError):
        await register_client(async_session, settings=fast_settings, **good_payload)


@pytest.mark.parametrize(
    "field, value, expected_code",
    [
        ("email", "not-an-email", "invalid_email"),
        ("rut", "12.345.678-0", "invalid_rut"),
        ("rut", "not-a-rut", "invalid_rut"),
        ("password", "short", "weak_password"),
        ("password", "        ", "weak_password"),
        ("name", "   ", "invalid_name"),
    ],
)
async def test_register_rejects_invalid_input(
    async_session, fast_settings, good_payload, field, value, expected_code
) -> None:
    """Every documented validation rule raises
    :class:`InvalidInputError` with a stable ``code`` so the
    front-end can map errors to UI hints."""
    good_payload[field] = value
    with pytest.raises(InvalidInputError) as exc:
        await register_client(async_session, settings=fast_settings, **good_payload)
    assert exc.value.code == expected_code


# ---------------------------------------------------------------------------
# authenticate_login
# ---------------------------------------------------------------------------


async def test_login_returns_jwt_for_valid_credentials(
    async_session, fast_settings, good_payload
) -> None:
    """A registered client can log in; the response carries a
    signed JWT and the client summary."""
    await register_client(async_session, settings=fast_settings, **good_payload)
    result = await authenticate_login(
        async_session,
        settings=fast_settings,
        email=good_payload["email"],
        password=good_payload["password"],
    )

    # The JWT must round-trip through the public decoder with
    # the right ``sub`` claim and ``type``.
    payload = decode_session_token(result.token, settings=fast_settings)
    assert payload["sub"] == result.client.id
    assert payload["email"] == "ops@acme.cl"
    assert payload["type"] == "login"
    # ``expires_at`` is timezone-aware; ``created_at`` is naive
    # (SQLite's default behaviour under the test engine). The
    # contract being verified is that the token's expiry is
    # *after* the row's creation timestamp – cast both to
    # naive UTC for the comparison.
    created_naive = result.client.created_at.replace(tzinfo=None)
    expires_naive = result.expires_at.replace(tzinfo=None)
    assert expires_naive > created_naive


async def test_login_is_case_insensitive_on_email(
    async_session, fast_settings, good_payload
) -> None:
    """A request with ``OPS@ACME.CL`` still matches the
    lower-cased row."""
    await register_client(async_session, settings=fast_settings, **good_payload)
    result = await authenticate_login(
        async_session,
        settings=fast_settings,
        email="OPS@ACME.CL",
        password=good_payload["password"],
    )
    assert result.client.email == "ops@acme.cl"


@pytest.mark.parametrize(
    "email, password",
    [
        ("ops@acme.cl", "wrong-password"),
        ("ghost@acme.cl", "sup3r-secret"),
    ],
)
async def test_login_rejects_invalid_credentials(
    async_session, fast_settings, good_payload, email, password
) -> None:
    """Both a wrong password and an unknown email raise the
    same generic error so an attacker cannot enumerate
    accounts."""
    await register_client(async_session, settings=fast_settings, **good_payload)
    with pytest.raises(InvalidCredentialsError):
        await authenticate_login(
            async_session,
            settings=fast_settings,
            email=email,
            password=password,
        )


async def test_login_rejects_suspended_client(async_session, fast_settings, good_payload) -> None:
    """A suspended client cannot log in – the auth path
    refuses the request even if the password is correct."""
    result = await register_client(async_session, settings=fast_settings, **good_payload)
    result.client.status = ClientStatus.SUSPENDED
    await async_session.commit()

    with pytest.raises(InactiveClientError):
        await authenticate_login(
            async_session,
            settings=fast_settings,
            email=good_payload["email"],
            password=good_payload["password"],
        )


# ---------------------------------------------------------------------------
# authenticate_api_key
# ---------------------------------------------------------------------------


async def test_authenticate_api_key_returns_active_client(
    async_session, fast_settings, good_payload
) -> None:
    """A round-trip: register, then resolve the plain key back
    to the same row."""
    result = await register_client(async_session, settings=fast_settings, **good_payload)
    resolved = await authenticate_api_key(async_session, api_key=result.api_key)
    assert resolved.id == result.client.id


async def test_authenticate_api_key_rejects_unknown_key(async_session, fast_settings) -> None:
    """An obviously-fake key is rejected without leaking which
    half of the credential was wrong."""
    with pytest.raises(InvalidApiKeyError):
        await authenticate_api_key(async_session, api_key="mgw_live_does-not-exist")


async def test_authenticate_api_key_rejects_suspended_client(
    async_session, fast_settings, good_payload
) -> None:
    """A suspended client's API key is treated as invalid."""
    result = await register_client(async_session, settings=fast_settings, **good_payload)
    result.client.status = ClientStatus.SUSPENDED
    await async_session.commit()

    with pytest.raises(InvalidApiKeyError):
        await authenticate_api_key(async_session, api_key=result.api_key)


# ---------------------------------------------------------------------------
# rotate_api_key
# ---------------------------------------------------------------------------


async def test_rotate_api_key_invalidates_previous_key(
    async_session, fast_settings, good_payload
) -> None:
    """Rotating a key must produce a value the new key
    accepts *and* the old one rejects."""
    original = await register_client(async_session, settings=fast_settings, **good_payload)
    original_key = original.api_key

    new_key = await rotate_api_key(async_session, settings=fast_settings, client=original.client)

    assert new_key != original_key
    assert new_key.startswith("mgw_live_")

    # The new key resolves to the same client.
    resolved = await authenticate_api_key(async_session, api_key=new_key)
    assert resolved.id == original.client.id

    # The old key is no longer valid.
    with pytest.raises(InvalidApiKeyError):
        await authenticate_api_key(async_session, api_key=original_key)


async def test_rotate_api_key_updates_last4(async_session, fast_settings, good_payload) -> None:
    """The dashboard's "key ending in …" hint must follow the
    rotation."""
    original = await register_client(async_session, settings=fast_settings, **good_payload)
    assert original.client.api_key_last4 == original.api_key[-4:]

    new_key = await rotate_api_key(async_session, settings=fast_settings, client=original.client)
    assert original.client.api_key_last4 == new_key[-4:]


# ---------------------------------------------------------------------------
# decode_session_token
# ---------------------------------------------------------------------------


async def test_decode_session_token_rejects_garbage(
    fast_settings,
) -> None:
    """A non-JWT payload is reported as invalid credentials –
    the route layer turns this into a 401 without leaking
    which part of the token was malformed."""
    with pytest.raises(InvalidCredentialsError):
        decode_session_token("not.a.real.jwt", settings=fast_settings)


async def test_decode_session_token_rejects_wrong_type(
    async_session, fast_settings, good_payload
) -> None:
    """A token that decodes successfully but carries the wrong
    ``type`` claim is still rejected. The guard is a defence
    against a future endpoint that mints tokens for a
    different purpose being accidentally accepted here."""
    import jwt as pyjwt

    payload = {"sub": "x", "email": "x@x.cl", "iat": 0, "exp": 9_999_999_999, "type": "magic-link"}
    token = pyjwt.encode(payload, fast_settings.jwt_secret, algorithm=fast_settings.jwt_algorithm)
    with pytest.raises(InvalidCredentialsError):
        decode_session_token(token, settings=fast_settings)
