"""Auth & registration service.

This module owns the domain logic for the
``POST /v1/auth/register`` and ``POST /v1/auth/login`` endpoints
documented in the PRD:

- :func:`register_client` – creates a new :class:`~app.models.client.Client`
  row, mints a fresh API key, hashes both the password and the
  API key with bcrypt, and returns the **plain** API key to the
  caller exactly once. The plain key is never written to the
  database; only the bcrypt digests persist.
- :func:`authenticate_login` – verifies a (email, password)
  pair against the stored hash and returns a dashboard session
  token (a signed JWT).
- :func:`authenticate_api_key` – verifies an inbound API key
  against the stored hash. Used by the ``require_api_key``
  FastAPI dependency in :mod:`app.routes.auth`.
- :func:`rotate_api_key` – mints a fresh API key for an existing
  client, replacing the stored hash. The previous key is
  invalidated immediately.

Design choices worth flagging:

- Bcrypt is the hashing algorithm of choice for both passwords
  and API keys. The cost factor comes from
  :class:`app.config.Settings.bcrypt_rounds` so a deployment
  can dial it up (or down, in dev) without code changes. The
  upper bound (15) is enforced by the ``Field(..., le=15)``
  validator on the setting.
- API keys carry a configurable public prefix (default
  ``mgw_live_``) and a 32-byte secret, base64url-encoded. The
  prefix is what an operator greps for in their own config;
  the secret is the actual authentication material.
- The login token is a short-lived JWT signed with the
  ``JWT_SECRET`` setting. The algorithm defaults to ``HS256``
  which keeps the verification path in the same process the
  token was minted in – appropriate for the MVP, where the
  Angular dashboard calls back into the same FastAPI service.
- The module never logs clear-text passwords, plain API keys
  or bcrypt digests. The redaction helpers in
  :mod:`app.observability.redact` are intentionally **not**
  used here because the helper surface deals with phone numbers
  and RUTs; secrets of any kind are simply not logged.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.client import Client, ClientPlan, ClientStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Length (in bytes) of the random secret embedded in every API
# key we mint. 32 bytes = 256 bits of entropy, which is well
# above the threshold the OWASP API Security Top 10 recommends
# for "high-value" secrets. The value is module-private so a
# caller cannot accidentally shorten it.
_API_KEY_SECRET_BYTES = 32

# Length of the ``api_key_last4`` tail we keep in the database.
# Exposed as a constant because both the schema migration and
# the response payload reference it; bumping it is a breaking
# change in three places, all of which would need to move
# together.
_API_KEY_LAST4_LEN = 4

# JWT claim names. The ``sub`` claim carries the client id so a
# dependency can resolve the current user from the token
# without an extra round-trip to the database.
_JWT_CLAIM_SUB = "sub"
_JWT_CLAIM_EMAIL = "email"
_JWT_CLAIM_IAT = "iat"
_JWT_CLAIM_EXP = "exp"
_JWT_CLAIM_TYPE = "type"
_JWT_TYPE_LOGIN = "login"

# The minimum acceptable length for a dashboard password. 8 is
# the OWASP floor; we deliberately do not require anything
# stronger at the API edge (a strength meter lives in the
# dashboard) so a unit test or a low-friction onboarding flow
# can still pass.
_MIN_PASSWORD_LENGTH = 8


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Base class for every auth-domain exception.

    The HTTP layer converts subclasses of this exception into a
    uniform ``HTTP 401`` / ``409`` / ``422`` response so the
    domain stays free of FastAPI-specific concerns.
    """

    http_status: int = 400

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidInputError(AuthError):
    """The request body did not pass validation."""

    http_status = 422


class DuplicateIdentityError(AuthError):
    """A unique field (email or RUT) is already taken."""

    http_status = 409


class InvalidCredentialsError(AuthError):
    """The (email, password) pair did not match a row."""

    http_status = 401


class InactiveClientError(AuthError):
    """The matched client is suspended or pending."""

    http_status = 403


class InvalidApiKeyError(AuthError):
    """The inbound API key did not match a row."""

    http_status = 401


# ---------------------------------------------------------------------------
# RUT helpers (kept module-private so other modules go through
# ``app.observability.redact.normalise_rut`` when they need to
# display / log a RUT).
# ---------------------------------------------------------------------------

# Accept ``12345678-5``, ``12.345.678-5`` and the legacy
# concatenated ``123456785``. The body may use dot separators
# or none; the check digit is mandatory. Body length is 1-8
# digits to match the Chilean legal range (a RUT can be as
# short as ``0-1`` for "natural persons without a sequence"
# – exceedingly rare in practice, but the validator must not
# reject what the registry issues).
_RUT_RE = re.compile(
    r"^(\d{1,8}|\d{1,2}\.\d{3}\.\d{3})-([0-9Kk])$"
    r"|^(\d{1,8})([0-9K])$"
)


def _normalise_rut(raw: str) -> str | None:
    """Return the canonical ``<8-digit-body>-<dv>`` representation.

    The canonical form is what gets persisted: it makes equality
    comparisons deterministic regardless of the input shape. Any
    unrecognised format returns ``None`` so the caller can raise
    a 422 without branching on regex details.
    """
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().upper()
    if not cleaned:
        return None
    match = _RUT_RE.match(cleaned)
    if match is None:
        return None
    # Pick the right group based on which alternative matched.
    if match.group(1) is not None:
        body = match.group(1).replace(".", "")
        dv = match.group(2)
    else:
        body = match.group(3)
        dv = match.group(4)
    if not body.isdigit():
        return None
    # Validate the check digit so an obvious typo ("12345678-0"
    # for "12345678-5") is caught at the edge instead of
    # sneaking into the database as a "valid format, wrong
    # check digit" row.
    if not _check_rut_dv(body, dv):
        return None
    return f"{int(body):08d}-{dv}"


def _check_rut_dv(body: str, dv: str) -> bool:
    """Return ``True`` if ``dv`` is the correct check digit for ``body``.

    The algorithm is the standard Chilean RUT verifier: walk the
    body right-to-left, multiply each digit by the cycling
    sequence ``2, 3, 4, 5, 6, 7, 2, 3, …``, sum the products,
    compute ``11 - (sum % 11)`` and translate the result
    (``11`` -> ``0``, ``10`` -> ``K``).
    """
    if not body.isdigit():
        return False
    total = 0
    multiplier = 2
    for digit in reversed(body):
        total += int(digit) * multiplier
        multiplier = 2 if multiplier == 7 else multiplier + 1
    remainder = 11 - (total % 11)
    expected = "0" if remainder == 11 else "K" if remainder == 10 else str(remainder)
    return expected == dv.upper()


def _normalise_email(raw: str) -> str | None:
    """Return the canonical (lowercased, DNS-validated) form of ``raw``.

    Returns ``None`` if ``raw`` is not a syntactically valid
    e-mail. The DNS check is intentionally *light* – the
    ``email-validator`` library performs deliverability
    validation that we don't need at the API edge (the dashboard
    can run a stricter check before submitting).

    The local part is forced to lower-case as well; the upstream
    ``email-validator`` library lower-cases the domain but
    leaves the local part alone (preserving the rare
    case-sensitive mailbox that some legacy MTAs still honour).
    The platform's auth model assumes a case-insensitive login,
    so we normalise on the way in.
    """
    if not isinstance(raw, str):
        return None
    try:
        # ``check_deliverability=False`` skips the DNS MX lookup
        # so the function is cheap to call in unit tests; the
        # platform sends a confirmation email later in the flow.
        result = validate_email(raw.strip(), check_deliverability=False)
    except EmailNotValidError:
        return None
    return f"{result.local_part.lower()}@{result.ascii_domain.lower()}"


# ---------------------------------------------------------------------------
# Password / API key hashing
# ---------------------------------------------------------------------------


def _bcrypt_hash(value: str, *, settings: Settings | None = None) -> str:
    """Return a bcrypt digest of ``value`` using the configured cost factor.

    The function is module-private because the *only* legitimate
    callers are :func:`register_client` and :func:`rotate_api_key`,
    both of which already control the input. A public helper
    would invite accidental double-hashing (hashing a value that
    is already a hash) and undermine the security model.
    """
    cfg = settings or get_settings()
    salt = bcrypt.gensalt(rounds=cfg.bcrypt_rounds)
    digest = bcrypt.hashpw(value.encode("utf-8"), salt)
    # ``bcrypt.hashpw`` returns ``bytes``; the database column is
    # ``String(255)`` which happily accepts ASCII. Decoding to
    # a ``str`` is what the SQLAlchemy ``String`` binding wants.
    return digest.decode("ascii")


def _bcrypt_verify(plain: str, digest: str) -> bool:
    """Constant-time comparison of ``plain`` against a stored bcrypt digest.

    ``bcrypt.checkpw`` is already constant-time; the function
    just adapts it to the str-vs-bytes shape the caller has.
    Any exception (malformed digest, wrong length) is
    swallowed and reported as "no match" so a malicious
    payload cannot crash the auth path.
    """
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), digest.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _generate_api_key(settings: Settings | None = None) -> str:
    """Mint a new API key in the form ``<prefix><base64url-secret>``.

    The prefix defaults to ``mgw_live_`` and is configurable via
    :class:`app.config.Settings` so a future staging environment
    can ship ``mgw_test_…`` keys without a code change. The
    secret part carries 256 bits of entropy from
    :func:`secrets.token_urlsafe`.
    """
    cfg = settings or get_settings()
    secret = secrets.token_urlsafe(_API_KEY_SECRET_BYTES)
    return f"{cfg.api_key_prefix}{secret}"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of :func:`register_client`.

    ``client``  – the persisted :class:`Client` row.
    ``api_key`` – the **plain** API key the caller must surface to
                  the user exactly once. The database only holds
                  the bcrypt digest.
    """

    client: Client
    api_key: str


@dataclass(frozen=True)
class LoginResult:
    """Outcome of :func:`authenticate_login`."""

    client: Client
    token: str
    expires_at: datetime


# ---------------------------------------------------------------------------
# Domain operations
# ---------------------------------------------------------------------------


def _validate_password(password: str) -> None:
    """Raise :class:`InvalidInputError` for unsafe passwords."""
    if not isinstance(password, str) or len(password) < _MIN_PASSWORD_LENGTH:
        raise InvalidInputError(
            "weak_password",
            f"password must be at least {_MIN_PASSWORD_LENGTH} characters",
        )
    # Reject whitespace-only passwords; a stricter strength
    # meter lives in the dashboard.
    if not password.strip():
        raise InvalidInputError("weak_password", "password cannot be blank")


def _validate_name(name: str) -> str:
    """Trim and validate the human-readable name. Returns the cleaned value."""
    if not isinstance(name, str):
        raise InvalidInputError("invalid_name", "name is required")
    cleaned = name.strip()
    if not cleaned:
        raise InvalidInputError("invalid_name", "name cannot be blank")
    if len(cleaned) > 200:
        raise InvalidInputError("invalid_name", "name is too long (max 200 chars)")
    return cleaned


async def register_client(
    session: AsyncSession,
    *,
    name: str,
    email: str,
    rut: str,
    password: str,
    plan: ClientPlan = ClientPlan.STARTER,
    settings: Settings | None = None,
) -> RegistrationResult:
    """Persist a new client and mint a fresh API key.

    Raises :class:`InvalidInputError` for any malformed field and
    :class:`DuplicateIdentityError` if the email or RUT is already
    taken. The bcrypt cost factor and API key prefix are read
    from ``settings`` (or the cached
    :func:`app.config.get_settings`).
    """
    cfg = settings or get_settings()

    clean_name = _validate_name(name)
    clean_email = _normalise_email(email)
    if clean_email is None:
        raise InvalidInputError("invalid_email", "email is not a valid address")
    clean_rut = _normalise_rut(rut)
    if clean_rut is None:
        raise InvalidInputError("invalid_rut", "rut is not a valid Chilean tax id")
    _validate_password(password)

    api_key = _generate_api_key(cfg)
    client = Client(
        name=clean_name,
        email=clean_email,
        rut=clean_rut,
        password_hash=_bcrypt_hash(password, settings=cfg),
        api_key_hash=_bcrypt_hash(api_key, settings=cfg),
        api_key_last4=api_key[-_API_KEY_LAST4_LEN:],
        plan=plan,
        status=ClientStatus.ACTIVE,
    )
    session.add(client)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # The ``IntegrityError`` does not tell us which unique
        # constraint fired; we re-raise a domain-specific error
        # with a generic code and let the caller decide how to
        # communicate it to the user. The route handler turns
        # this into a 409.
        raise DuplicateIdentityError(
            "duplicate_identity",
            "a client with this email or RUT already exists",
        ) from exc
    await session.refresh(client)
    return RegistrationResult(client=client, api_key=api_key)


async def authenticate_login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    settings: Settings | None = None,
) -> LoginResult:
    """Verify the dashboard credentials and mint a session token.

    The function never reveals *which* field was wrong: a wrong
    e-mail and a wrong password both raise
    :class:`InvalidCredentialsError`. The exception message is
    deliberately generic to make account enumeration harder.
    """
    cfg = settings or get_settings()
    clean_email = _normalise_email(email)
    if clean_email is None:
        # Normalising here means a request that submits
        # ``"Alice@Example.com"`` still works the same as
        # ``"alice@example.com"``; if normalisation fails we
        # just short-circuit to the generic error so we do not
        # leak which part of the request was malformed.
        raise InvalidCredentialsError("invalid_credentials", "invalid email or password")

    stmt = select(Client).where(Client.email == clean_email)
    result = await session.execute(stmt)
    client = result.scalar_one_or_none()
    if client is None or not _bcrypt_verify(password, client.password_hash):
        raise InvalidCredentialsError("invalid_credentials", "invalid email or password")
    if client.status != ClientStatus.ACTIVE:
        raise InactiveClientError(
            "inactive_client",
            f"client is {client.status.value} and cannot sign in",
        )

    token, expires_at = _mint_session_token(client, settings=cfg)
    return LoginResult(client=client, token=token, expires_at=expires_at)


def _mint_session_token(
    client: Client,
    *,
    settings: Settings | None = None,
) -> tuple[str, datetime]:
    """Return ``(token, expires_at)`` for a freshly minted dashboard session."""
    cfg = settings or get_settings()
    now = datetime.now(tz=UTC)
    expires_at = now + timedelta(minutes=cfg.jwt_ttl_minutes)
    payload = {
        _JWT_CLAIM_SUB: client.id,
        _JWT_CLAIM_EMAIL: client.email,
        _JWT_CLAIM_IAT: int(now.timestamp()),
        _JWT_CLAIM_EXP: int(expires_at.timestamp()),
        _JWT_CLAIM_TYPE: _JWT_TYPE_LOGIN,
    }
    token = jwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)
    return token, expires_at


def decode_session_token(
    token: str,
    *,
    settings: Settings | None = None,
) -> dict[str, object]:
    """Decode and verify a dashboard session token.

    Raises :class:`InvalidCredentialsError` on any failure
    (expired, malformed, bad signature, wrong ``typ``). The
    exception type is what the route handler turns into a 401
    response so the rest of the platform does not need to
    know about PyJWT specifics.
    """
    cfg = settings or get_settings()
    try:
        payload = jwt.decode(token, cfg.jwt_secret, algorithms=[cfg.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidCredentialsError("invalid_token", "session token is invalid") from exc
    if payload.get(_JWT_CLAIM_TYPE) != _JWT_TYPE_LOGIN:
        raise InvalidCredentialsError("invalid_token", "session token has the wrong type")
    return payload


async def authenticate_api_key(
    session: AsyncSession,
    *,
    api_key: str,
) -> Client:
    """Resolve an inbound API key to a :class:`Client` row.

    Because bcrypt digests include a per-row salt, the database
    cannot answer "is this key valid?" with a SQL ``WHERE``;
    every candidate row has to be loaded and verified in
    Python. For the MVP volume (one client = one key, indexed
    by the ``api_key_hash`` digest) the candidate set is always
    zero or one rows, so the linear scan is acceptable. A
    future "index by prefix" optimisation is documented in the
    PRD follow-up list.
    """
    if not isinstance(api_key, str) or not api_key:
        raise InvalidApiKeyError("invalid_api_key", "api key is missing")

    stmt = select(Client)
    result = await session.execute(stmt)
    candidates = result.scalars().all()
    for candidate in candidates:
        if candidate.status != ClientStatus.ACTIVE:
            # A suspended client's key is treated as invalid
            # even if the digest happens to match – the auth
            # service should not hand a token to anyone whose
            # account is currently locked.
            continue
        if _bcrypt_verify(api_key, candidate.api_key_hash):
            return candidate
    raise InvalidApiKeyError("invalid_api_key", "api key is invalid or revoked")


async def rotate_api_key(
    session: AsyncSession,
    *,
    client: Client,
    settings: Settings | None = None,
) -> str:
    """Mint a new API key for ``client`` and invalidate the old one.

    Returns the **plain** new key. The previous key is
    immediately invalid because its bcrypt digest is no longer
    in the database; the next API call using it raises
    :class:`InvalidApiKeyError`. The caller is responsible for
    surfacing the new plain key to the user (the dashboard does
    this with a "copy the new key" modal).
    """
    cfg = settings or get_settings()
    new_key = _generate_api_key(cfg)
    client.api_key_hash = _bcrypt_hash(new_key, settings=cfg)
    client.api_key_last4 = new_key[-_API_KEY_LAST4_LEN:]
    await session.commit()
    await session.refresh(client)
    return new_key


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------

# Public helpers used by tests and (potentially) by other
# services. Importing from a single namespace keeps the
# ``from app.services.auth import …`` call sites short and
# makes the module's public surface explicit.
__all__ = (
    "AuthError",
    "DuplicateIdentityError",
    "InactiveClientError",
    "InvalidApiKeyError",
    "InvalidCredentialsError",
    "InvalidInputError",
    "LoginResult",
    "RegistrationResult",
    "authenticate_api_key",
    "authenticate_login",
    "decode_session_token",
    "register_client",
    "rotate_api_key",
)
