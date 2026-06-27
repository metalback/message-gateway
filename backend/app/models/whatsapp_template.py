"""WhatsApp template ORM model.

A :class:`WhatsAppTemplate` row represents a single message template
the customer has registered with Meta's WhatsApp Business API
(``graph.facebook.com`` -> ``/<waba_id>/message_templates``). Meta
requires every "non-session" outbound message to reference an
approved template, so the CRUD surface in
:mod:`app.routes.templates` is what lets a customer stay compliant
with the WhatsApp Business Policy without leaving the Message
Gateway dashboard.

The shape mirrors the ``plantillas_whatsapp`` table documented in
the PRD (see ``PRD.md`` -> "Modelo de datos"):

- ``id``               – UUIDv4 primary key, generated client-side.
- ``client_id``        – FK to ``clientes.id`` (the customer that
                         owns the template).
- ``name``             – Meta-side template name. Lowercase, ASCII,
                         alphanumerics + underscore – the same
                         constraints Meta enforces on its side.
- ``language``         – BCP-47 language tag (``"es_CL"``,
                         ``"en_US"`` …). Stored as a string rather
                         than an enum because Meta publishes
                         language tags on a rolling basis and
                         pinning a closed enum would force a
                         migration on every addition.
- ``category``         – ``utility`` / ``marketing`` /
                         ``authentication``. Mirrors Meta's
                         categories so the dashboard can render
                         the same label Meta shows in the WABA
                         console.
- ``status``           – ``draft`` / ``pending`` / ``approved`` /
                         ``rejected``. ``draft`` means the
                         customer has not yet asked the platform
                         to submit the template to Meta;
                         ``pending`` means the platform has
                         submitted it and is waiting for Meta's
                         review; ``approved`` / ``rejected`` are
                         Meta's final answer.
- ``meta_template_id`` – the ID Meta returns after the template
                         is submitted. ``None`` while the row is
                         in ``draft``.
- ``components``       – JSON-serialised list of components
                         (header / body / footer / buttons). The
                         schema is too rich and too Meta-specific
                         to model as a relational table on the
                         MVP, so we store the canonical
                         representation as JSON and let the
                         service layer validate the shape.
- ``description``      – free-form note the customer attaches to
                         the row (e.g. "comprobante de pago").
                         Optional – the dashboard shows it as a
                         tooltip.
- ``created_at`` / ``updated_at`` – server-side timestamps.

Security notes:

- The :attr:`WhatsAppTemplate.components` column is plain JSON.
  Meta's content is not PII in the Chilean-LAW-19628 sense, but
  the platform still scrubs the field with the redaction
  helpers in :mod:`app.observability.redact` before it reaches
  the log stream – a future regulator might widen the
  definition and the cost of scrubbing is low.
- The :attr:`WhatsAppTemplate.meta_template_id` is **not** a
  secret. It is the public identifier Meta uses in the
  ``messaging`` API and can safely appear in URLs.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _new_template_id() -> str:
    """Default factory for :attr:`WhatsAppTemplate.id`.

    Mirrors :func:`app.models.message._new_message_id` so the
    row is referenceable (logged, returned to the caller)
    before it is flushed.
    """
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class WhatsAppTemplateCategory(enum.StrEnum):
    """Meta-side template category.

    Values mirror Meta's vocabulary verbatim so the dashboard
    can render the same label Meta shows in the WABA console.
    The MVP only accepts a closed set; new categories land by
    extending the enum and shipping an Alembic migration.
    """

    UTILITY = "utility"
    MARKETING = "marketing"
    AUTHENTICATION = "authentication"


class WhatsAppTemplateStatus(enum.StrEnum):
    """Lifecycle states a :class:`WhatsAppTemplate` row can be in.

    The transitions are:

    - ``draft``     – freshly created; the platform has not yet
                      asked Meta to review it.
    - ``pending``   – the platform has submitted the template to
                      Meta and is waiting for review.
    - ``approved``  – Meta has approved the template; the row
                      can be used as the ``template`` reference
                      in a WhatsApp message.
    - ``rejected``  – Meta has rejected the template; the
                      ``rejection_reason`` column carries the
                      upstream's feedback.

    Stored as a ``String`` so a new state (e.g. ``"paused"``) can
    land in a future release without rewriting the column type.
    """

    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class WhatsAppTemplate(Base):
    """A WhatsApp message template the customer has registered.

    The table is named ``plantillas_whatsapp`` (Spanish for
    "WhatsApp templates") to match the PRD's vocabulary and the
    rest of the customer-facing Spanish copy in the dashboard.
    """

    __tablename__ = "plantillas_whatsapp"

    # --- Identity --------------------------------------------------------
    # UUID primary key – same rationale as
    # :attr:`Message.id`. The surrogate key is never exposed to
    # API consumers; :attr:`WhatsAppTemplate.name` is the
    # public handle customers recognise (it is the same name
    # Meta displays in the WABA console).
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_template_id,
    )

    # --- Foreign keys ----------------------------------------------------
    # ``client_id`` links the template to the customer that
    # owns it. The FK is declared as a no-cascade reference
    # (mirroring the ``clientes`` <-> ``mensajes`` relationship)
    # so a suspended customer keeps its template history
    # available for audit.
    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("clientes.id"),
        nullable=False,
        index=True,
    )

    # --- Identity on Meta ------------------------------------------------
    # Template name as registered with Meta. Lowercase, ASCII,
    # alphanumerics + underscore – the same rules Meta
    # enforces on its side. The service layer validates the
    # pattern before persisting so a typo does not silently
    # become an uneditable row.
    name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)

    # BCP-47 language tag (``"es_CL"``, ``"en_US"`` …). Stored
    # as a ``String`` rather than an enum because Meta
    # publishes language tags on a rolling basis and a closed
    # enum would force a migration on every addition.
    language: Mapped[str] = mapped_column(String(16), nullable=False)

    category: Mapped[WhatsAppTemplateCategory] = mapped_column(
        String(32),
        nullable=False,
        default=WhatsAppTemplateCategory.UTILITY,
    )

    # --- Lifecycle -------------------------------------------------------
    # The platform-managed status column. The values match
    # :class:`WhatsAppTemplateStatus` and are stored as
    # strings so a future release can introduce a new state
    # without rewriting the column type.
    status: Mapped[WhatsAppTemplateStatus] = mapped_column(
        String(32),
        nullable=False,
        default=WhatsAppTemplateStatus.DRAFT,
        index=True,
    )

    # Identifier Meta returns after the template is submitted.
    # ``None`` while the row is in ``draft`` – the service
    # layer sets the value when the platform forwards the
    # template to Meta's WABA endpoint. Indexed because the
    # future "send message using template" flow looks the
    # row up by ``meta_template_id`` rather than by the
    # platform surrogate key.
    meta_template_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )

    # Reason Meta gave when rejecting the template (e.g. the
    # offending variable pattern). Kept separate from the
    # human-readable ``description`` so the dashboard can
    # surface the upstream's exact wording without parsing
    # free-form text.
    rejection_reason: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
    )

    # Free-form note the customer attaches to the row. Used
    # by the dashboard for tooltip-style help (e.g. which
    # campaign the template belongs to). Optional.
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Payload ---------------------------------------------------------
    # The Meta-side "components" array (header / body / footer
    # / buttons) serialised as JSON. The structure is too rich
    # and too Meta-specific to model as a relational table on
    # the MVP; the platform stores the canonical representation
    # as JSON and lets the service layer validate the shape.
    # ``Text`` (no length cap at the DB level) so a
    # button-heavy template (multiple ``button`` components)
    # can fit; the practical ceiling is enforced at the API
    # edge.
    components: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

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
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"WhatsAppTemplate(id={self.id!r}, name={self.name!r}, "
            f"language={self.language!r}, status={self.status!r})"
        )

    def __init__(self, **kwargs: object) -> None:
        """Initialise a :class:`WhatsAppTemplate` and pre-fill the UUID PK.

        Same rationale as
        :meth:`app.models.client.Client.__init__`: the
        application code needs the ``id`` *before* the row is
        flushed so it can return it to the caller immediately.
        """
        if "id" not in kwargs:
            kwargs["id"] = _new_template_id()
        super().__init__(**kwargs)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
# Composite indexes the templates routes actually use are
# declared on the table (rather than via ``__table_args__``) so
# Alembic's autogenerate picks them up automatically. The two
# queries we expect most often are:
#
# - "list my templates" – ``WHERE client_id = ? ORDER BY created_at DESC``
# - "find a template by Meta id" – ``WHERE meta_template_id = ?``
#   (used by the future "send WhatsApp template message" flow).


Index(
    "ix_plantillas_whatsapp_client_created",
    WhatsAppTemplate.__table__.c.client_id,
    WhatsAppTemplate.__table__.c.created_at,
)


# Unique constraint that mirrors the real-world Meta
# constraint: a customer cannot register two templates with
# the same ``(name, language)`` pair (Meta itself rejects the
# second submission). Declared as an ``Index(unique=True)``
# rather than ``UniqueConstraint`` so autogenerate picks it up
# the same way it picks up the other indexes.
Index(
    "uq_plantillas_whatsapp_client_name_language",
    WhatsAppTemplate.__table__.c.client_id,
    WhatsAppTemplate.__table__.c.name,
    WhatsAppTemplate.__table__.c.language,
    unique=True,
)


__all__ = (
    "WhatsAppTemplate",
    "WhatsAppTemplateCategory",
    "WhatsAppTemplateStatus",
)
