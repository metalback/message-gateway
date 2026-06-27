"""Client ORM model.

A :class:`Client` row represents a single developer / company that
holds an API key against the platform. The shape mirrors the
``clientes`` table documented in the PRD (see
``PRD.md`` -> "Modelo de datos") and is intentionally narrow: only
the fields the auth / registration feature needs land in this
model. Related concerns (plan metadata, billing, webhooks…) get
their own models in follow-up tasks and link back to the
``clientes.id`` foreign key.

Security notes:

- The :attr:`Client.api_key_hash` column stores a **bcrypt** digest
  of the API key the client received at registration time. The
  plain key is shown to the caller exactly once (the response of
  ``POST /v1/auth/register``) and is never persisted. Every
  subsequent request authenticates by hashing the inbound key and
  comparing against the stored digest.
- The :attr:`Client.password_hash` column follows the same rule
  for the dashboard password: a bcrypt digest, never the clear
  text. ``POST /v1/auth/login`` accepts the clear text password
  and verifies it with the constant-time ``bcrypt.checkpw``
  helper.
- PII fields (``email``, ``rut``) live alongside the hashes so
  the application can look the client up by either identifier
  without an extra join. The redaction helpers in
  :mod:`app.observability.redact` are responsible for scrubbing
  these values before they reach the log stream.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TypeVar

from sqlalchemy import DateTime, Float, Integer, String, TypeDecorator, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _new_client_id() -> str:
    """Default factory for :attr:`Client.id`.

    Wrapping :func:`uuid.uuid4` in a function lets us
    reference it as a SQLAlchemy ``default`` (which expects
    a zero-argument callable) and from a unit test
    instantiating :class:`Client` directly: the id is
    populated at construction time, not at flush time, so
    the application code never has to wait for a commit
    before it can pass the row around.
    """
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enum column type
# ---------------------------------------------------------------------------

_E = TypeVar("_E", bound=enum.Enum)


class _StringEnum(TypeDecorator):
    """Map a string-backed ``Enum`` to a plain ``String`` column.

    The Alembic migration uses ``String(20)`` for both
    ``plan`` and ``status`` so a future release can introduce
    a new enum value without rewriting the column type.
    SQLAlchemy's built-in :class:`sqlalchemy.Enum` would
    require a different migration, so we bridge the gap with
    a :class:`TypeDecorator` that round-trips the value
    between the in-memory :class:`enum.Enum` member and the
    string the database stores.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[_E], length: int) -> None:
        super().__init__(length=length)
        self._enum_cls = enum_cls

    def process_bind_param(self, value: object, _dialect: object) -> str:
        if value is None:
            return value  # type: ignore[return-value]
        if isinstance(value, self._enum_cls):
            return value.value
        if isinstance(value, str):
            # Validate eagerly so a typo is caught at write
            # time instead of surfacing as a silent default
            # when the column is read back.
            self._enum_cls(value)
            return value
        raise TypeError(f"expected {self._enum_cls.__name__} or str, got {type(value).__name__}")

    def process_result_value(self, value: object, _dialect: object) -> object:
        if value is None or isinstance(value, self._enum_cls):
            return value
        try:
            return self._enum_cls(value)
        except ValueError:
            # An unknown value (e.g. a future enum that the
            # running code does not know about) is preserved
            # as the raw string so a deploy/rollback is not
            # blocked by stale application binaries.
            return value


class ClientStatus(enum.StrEnum):
    """Lifecycle states a :class:`Client` row can be in.

    Stored as a :class:`String` column (the ``Enum`` *value*) so a
    future migration that introduces a new state does not have to
    also rewrite the column type – the value column simply picks
    up the new string.
    """

    ACTIVE = "active"
    SUSPENDED = "suspended"
    PENDING = "pending"


class ClientPlan(enum.StrEnum):
    """Commercial plans the platform offers.

    Mirrors the plan list in the PRD (Starter / Growth /
    Enterprise). New plans land by extending this enum and
    shipping an Alembic migration that writes a *new* column or
    renames an existing one – we keep the schema narrow for the
    MVP and rely on the enum to fail loudly if a stray string
    sneaks into the table.
    """

    STARTER = "starter"
    GROWTH = "growth"
    ENTERPRISE = "enterprise"


class ClientRole(enum.StrEnum):
    """Authorization role the platform assigns to a :class:`Client`.

    The MVP supports two values:

    - :attr:`CLIENT` – a regular customer. Default for every
      fresh registration (``POST /v1/auth/register``). Has
      access to the customer-facing ``/v1/*`` surface and the
      per-customer dashboard.
    - :attr:`ADMIN` – a platform operator. Has access to the
      ``/v1/admin/*`` surface for client management, aggregated
      metrics and the error log. The value is stored as a
      ``String`` (via :class:`_StringEnum`) so a future
      ``support`` / ``read_only`` role can land without a
      schema change.
    """

    CLIENT = "client"
    ADMIN = "admin"


class Client(Base):
    """A registered customer of the platform.

    The table is named ``clientes`` (Spanish for "clients") to
    match the PRD and the rest of the customer-facing Spanish
    copy in the dashboard.
    """

    __tablename__ = "clientes"

    # --- Identity --------------------------------------------------------
    # UUIDs are used so the primary key never leaks business
    # meaning (an integer ``id`` would let an attacker enumerate
    # the customer base by walking the sequence). The value is
    # generated client-side so the same row works in unit tests
    # against SQLite without an extension; the
    # :func:`_new_client_id` factory fires on instantiation so a
    # caller can hand a freshly built row to the service layer
    # without a round-trip through the database.
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_client_id,
    )

    # --- Profile ---------------------------------------------------------
    # The ``name`` field stores the human-readable label the
    # customer enters at registration (full name or company
    # name). Kept short – 200 chars is enough for any reasonable
    # company name in the Chilean market.
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # E-mail is the dashboard login identifier AND the primary
    # contact channel for billing notifications, so it is
    # unique across the table.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)

    # Chilean RUT (role único tributario). Stored normalised
    # ("12345678-5") so equality comparisons in the auth service
    # are deterministic. The :func:`app.services.auth.normalise_rut`
    # helper owns the canonical representation.
    rut: Mapped[str] = mapped_column(String(12), nullable=False, unique=True, index=True)

    # --- Auth material ---------------------------------------------------
    # Bcrypt digest of the dashboard password. ``None`` would
    # break login, but the column is ``NOT NULL`` so a stray
    # insert that forgets the digest fails loudly.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Bcrypt digest of the API key. The plain key never lives in
    # the database; we only ever persist this hash.
    api_key_hash: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # The last 4 characters of the plain API key – kept so the
    # dashboard can render "key ending in 7a3f" without ever
    # being able to reconstruct the full key. Sized to match the
    # tail length exposed to operators.
    api_key_last4: Mapped[str] = mapped_column(String(4), nullable=False)

    # --- Commercial state ----------------------------------------------
    plan: Mapped[ClientPlan] = mapped_column(
        _StringEnum(ClientPlan, length=20),
        nullable=False,
        default=ClientPlan.STARTER,
    )

    status: Mapped[ClientStatus] = mapped_column(
        _StringEnum(ClientStatus, length=20),
        nullable=False,
        default=ClientStatus.ACTIVE,
    )

    # --- Authorization role --------------------------------------------
    # Drives the :func:`app.routes.admin.require_admin` dependency.
    # Defaults to :attr:`ClientRole.CLIENT` so the regular
    # registration path is unchanged. The initial admin is
    # seeded by Alembic migration ``0005_admin_role_and_markup``
    # (and the docs walk through the manual SQL alternative for
    # an environment that bootstraps a fresh database from a
    # non-migration entry point).
    role: Mapped[ClientRole] = mapped_column(
        _StringEnum(ClientRole, length=20),
        nullable=False,
        default=ClientRole.CLIENT,
        index=True,
    )

    # --- Per-client pricing (issue #10) --------------------------------
    # ``markup_percent`` is the percentage fee the platform
    # adds on top of the provider's cost (a value of ``0.25``
    # means "charge 25% above cost"). ``markup_fixed_clp`` is
    # a flat CLP surcharge the customer pays per billable
    # message. Both default to ``0`` so the pricing engine
    # used by the customer-facing billing flow is unchanged
    # unless an operator deliberately turns them on.
    markup_percent: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    markup_fixed_clp: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # --- Timestamps ------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Client(id={self.id!r}, email={self.email!r}, "
            f"plan={self.plan!r}, status={self.status!r}, role={self.role!r})"
        )

    def __init__(self, **kwargs: object) -> None:
        """Initialise a :class:`Client` and pre-fill the UUID primary key.

        SQLAlchemy's ``default`` callable fires on flush, not
        at construction time. The auth service needs the
        ``id`` *before* the row is persisted (it logs it,
        returns it to the caller and so on), so we set it
        eagerly here. Callers may still pass ``id=…`` to
        override the default – the override is required by
        unit tests that exercise the autogenerate path.

        The ``plan`` and ``status`` fields are coerced by
        :class:`_StringEnum` on read and write, so callers
        can pass either an :class:`enum.Enum` member or a
        plain ``str``.
        """
        if "id" not in kwargs:
            kwargs["id"] = _new_client_id()
        super().__init__(**kwargs)
