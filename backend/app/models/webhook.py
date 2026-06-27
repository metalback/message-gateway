"""Webhook subscription ORM model.

A :class:`Webhook` row represents a single HTTP endpoint a
:class:`~app.models.client.Client` has registered to receive
asynchronous **delivery receipts** for the messages the
platform sends on their behalf. The shape mirrors the
``webhooks`` table documented in the PRD (see ``PRD.md`` ->
"Modelo de datos") and is intentionally narrow: only the
fields the subscription / delivery feature needs land in this
model. Related concerns (delivery attempts, retry state) get
their own tables in follow-up tasks.

Lifecycle
---------

A :class:`Webhook` row goes through three observable states:

- ``active=True``  – the platform will POST delivery
  receipts to ``url`` whenever a message the client owns
  transitions to a status the row is subscribed to.
- ``active=False`` – the row is preserved (so the dashboard
  can still render "webhook disabled") but no outbound POSTs
  are attempted. The platform's contract is "disabling is
  reversible", so ``active`` is a soft flag rather than a
  delete.
- Deleted         – the row is removed; any in-flight
  delivery attempts that already started may still
  complete, but no new ones are scheduled.

Security notes
--------------

- :attr:`Webhook.secret` is the HMAC-SHA256 key the
  platform uses to sign every outbound delivery receipt.
  The secret is generated client-side (32 bytes of CSPRNG
  entropy) and shown to the user **once** – the same
  flow :mod:`app.services.auth` uses for API keys. The
  database only ever stores the plain secret in the
  ``secret`` column; the column is never exposed by the
  GET endpoint (the secret is only returned by the POST
  response that created it).
- :attr:`Webhook.url` is the destination endpoint. The
  service layer enforces ``https://`` at create time so a
  misconfigured client cannot accidentally ship receipts
  over the public internet in clear text. The migration
  also adds a length cap (``<=500`` chars) so a stray
  payload cannot blow up the column.
- :attr:`Webhook.events` is a comma-separated list of
  event names (``"message.delivered"``,
  ``"message.failed"``, ``"message.sent"``). The MVP
  accepts any string; the service layer whitelists the
  known set so a typo surfaces as a 422 instead of
  silently dropping receipts.
"""

from __future__ import annotations

import enum
import secrets
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _new_webhook_id() -> str:
    """Default factory for :attr:`Webhook.id`.

    Mirrors the rationale in
    :func:`app.models.client._new_client_id`: the id is
    populated at construction time so the service layer can
    hand a freshly-built row back to the caller without
    waiting for a database commit.
    """
    return str(uuid.uuid4())


def _new_webhook_secret() -> str:
    """Default factory for :attr:`Webhook.secret`.

    32 bytes of CSPRNG entropy, hex-encoded for portability
    across transports (URL parameters, header values, log
    lines). 64 hex characters = 256 bits, the OWASP
    recommendation for HMAC-SHA256 keys.
    """
    return secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Event vocabulary
# ---------------------------------------------------------------------------


class WebhookEvent(enum.StrEnum):
    """Event types a :class:`Webhook` subscription can opt in to.

    The values are deliberately short and dotted so a future
    "add a new event" change is a one-line addition here plus
    a documentation update; the database column is a
    comma-separated string so the migration never has to
    rewrite the column type.

    The set is conservative: every event maps to a real
    :class:`~app.models.message.MessageStatus` transition
    the platform already knows how to detect. Adding an
    event that the messaging pipeline cannot emit would
    just confuse the user when no receipts ever arrive.
    """

    MESSAGE_SENT = "message.sent"
    MESSAGE_DELIVERED = "message.delivered"
    MESSAGE_FAILED = "message.failed"


# Default set the platform subscribes to when the request
# body omits ``events``. Mirrors the "send me everything
# important" default the dashboard wires up.
DEFAULT_EVENTS: tuple[str, ...] = (
    WebhookEvent.MESSAGE_SENT.value,
    WebhookEvent.MESSAGE_DELIVERED.value,
    WebhookEvent.MESSAGE_FAILED.value,
)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Webhook(Base):
    """A delivery-receipt subscription owned by a single :class:`Client`.

    The table is named ``webhooks`` (English – matches the
    rest of the English API surface; the customer-facing
    Spanish copy lives in the dashboard, not the database).
    """

    __tablename__ = "webhooks"

    # --- Identity -----------------------------------------------------
    # UUID primary key for the same reasons
    # :attr:`app.models.client.Client.id` is a UUID: the
    # sequence never leaks how many webhooks the platform
    # serves, and the value is safe to embed in URL paths.
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_webhook_id,
    )

    # --- Foreign key --------------------------------------------------
    # ``clientes.id`` is a string UUID, so the FK is also
    # a string. ``NO ACTION`` on delete (the default): a
    # suspended client keeps its webhook subscriptions so
    # the audit trail survives account closure – matching
    # the policy :class:`Message` already follows.
    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("clientes.id"),
        nullable=False,
        index=True,
    )

    # --- Destination --------------------------------------------------
    # The URL the platform POSTs delivery receipts to.
    # Length cap (500) matches the same ceiling the
    # ``pagos.flow_redirect_url`` column uses so the
    # database stays free of accidentally-large payloads.
    url: Mapped[str] = mapped_column(String(500), nullable=False)

    # --- Subscription -------------------------------------------------
    # Comma-separated list of
    # :class:`WebhookEvent` values the client is
    # interested in. Stored as a plain ``String`` so a
    # future release can introduce a new event without a
    # migration that rewrites the column type. The service
    # layer is responsible for splitting the value back
    # into a ``list[str]`` before doing any work.
    events: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        default=",".join(DEFAULT_EVENTS),
    )

    # --- Signing secret ----------------------------------------------
    # HMAC-SHA256 key the platform uses to sign every
    # outbound receipt. Returned to the caller **once** at
    # create time (mirrors the API-key flow in
    # :mod:`app.services.auth`); the dashboard is expected
    # to surface it to the user and discard it from
    # memory as soon as the user has confirmed they have
    # stored it. The column itself is ``nullable=False``
    # so a stray insert that forgets the secret fails
    # loudly.
    secret: Mapped[str] = mapped_column(String(128), nullable=False)

    # --- Active flag --------------------------------------------------
    # ``True`` by default; flipping to ``False`` disables
    # the subscription without losing history. The
    # migration creates an index on ``(client_id, active)``
    # so the worker's "find subscriptions for this
    # message" query is a single lookup.
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    # --- Timestamps ---------------------------------------------------
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
        # ``secret`` is intentionally absent from the repr
        # so a copy-paste of the debug output never leaks
        # the HMAC key.
        return (
            f"Webhook(id={self.id!r}, client_id={self.client_id!r}, "
            f"url={self.url!r}, active={self.active!r})"
        )

    def __init__(self, **kwargs: object) -> None:
        """Initialise a :class:`Webhook` and pre-fill the UUID
        primary key and the HMAC secret.

        Mirrors :meth:`app.models.client.Client.__init__`:
        the ``id`` is set eagerly so the application code
        can hand a freshly-built row back to the caller
        without waiting for a database flush.

        The :attr:`secret` default fires on instantiation
        rather than on flush (the latter is SQLAlchemy's
        default) so the POST endpoint can return the
        plain secret in the same response it returns the
        ``id`` – the alternative (flush-then-read) would
        require a round-trip and the secret would be lost
        the moment the session expires.
        """
        if "id" not in kwargs:
            kwargs["id"] = _new_webhook_id()
        if "secret" not in kwargs:
            kwargs["secret"] = _new_webhook_secret()
        super().__init__(**kwargs)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
# Composite index that backs the delivery-receipt fan-out
# query: ``SELECT … FROM webhooks WHERE client_id = ? AND
# active = true``. Declared on the table (rather than via
# ``__table_args__``) so Alembic's autogenerate picks it up
# automatically.


Index(
    "ix_webhooks_client_active",
    Webhook.__table__.c.client_id,
    Webhook.__table__.c.active,
)


__all__ = (
    "DEFAULT_EVENTS",
    "Webhook",
    "WebhookEvent",
)
