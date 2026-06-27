"""Plan ORM model.

A :class:`Plan` row represents one of the commercial plans the
platform offers. The shape mirrors the ``planes`` table documented
in the PRD (see ``PRD.md`` -> "Modelo de datos") and adds the
``code`` discriminator the rest of the platform uses to refer to
a plan without leaking the database surrogate key.

The pricing model documented in the PRD is "fixed monthly fee +
per-message overage":

- Starter:  1,000 msgs/mes por CLP 19.990, extra CLP 25 c/u
- Growth:   10,000 msgs/mes por CLP 79.990, extra CLP 18 c/u
- Enterprise: precio a medida – volume-based, no overage

The fields below encode that contract:

- :attr:`Plan.price_clp`        – the monthly fee, in CLP (whole
  pesos; the SII requires integer amounts on the DTE).
- :attr:`Plan.msg_limit`        – messages included in the
  monthly fee. ``None`` (nullable) means "unlimited" – used by
  Enterprise.
- :attr:`Plan.extra_msg_price`  – per-message price (CLP) once
  the customer exceeds :attr:`Plan.msg_limit`. ``None`` for
  Enterprise where overage is negotiated bilaterally.

The :attr:`Plan.code` column is a short, stable identifier
(``"starter"``, ``"growth"``, ``"enterprise"``) that is safe to
expose in URLs and to use as a foreign key in client
configuration. It is also the value the dashboard passes to
``POST /v1/billing/subscriptions`` to switch plans.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _new_plan_id() -> str:
    """Default factory for :attr:`Plan.id`.

    Same rationale as :func:`app.models.client._new_client_id`:
    UUIDs are generated client-side so a unit test can
    instantiate a :class:`Plan` and reference its ``id`` before
    any database round-trip.
    """
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enum column type
#
# Shared with :mod:`app.models.client` so the same ``String(20)``
# shape is used for every enum-typed column. The class is
# duplicated here (instead of imported) to keep the two models
# independent: a future refactor that splits the client and plan
# enums into their own files is then a one-line change.
# ---------------------------------------------------------------------------


class PlanBillingPeriod(enum.StrEnum):
    """How often a plan is invoiced.

    The MVP supports only :attr:`MONTHLY`; ``quarterly`` /
    ``annual`` are exposed for future commercial packages
    without forcing a schema change. The column is stored as a
    plain ``String`` (via :class:`_StringEnum`) so a new value
    does not have to be introduced through a migration that
    rewrites the column type.
    """

    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class _StringEnum:
    """Lightweight enum-to-string bridge (see :mod:`app.models.client`).

    Replicated here (rather than imported) so the two model
    files are decoupled – a future split can move either
    without churning the other.
    """


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Plan(Base):
    """A commercial plan offered by the platform.

    The table is named ``planes`` (Spanish for "plans") to
    match the PRD's vocabulary and the rest of the
    customer-facing Spanish copy in the dashboard.
    """

    __tablename__ = "planes"

    # --- Identity -----------------------------------------------------
    # UUID primary key – same rationale as
    # :attr:`Client.id`. The surrogate key is never exposed to
    # API consumers; :attr:`Plan.code` is the public handle.
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_new_plan_id,
    )

    # Public, stable identifier (``starter`` / ``growth`` /
    # ``enterprise``). Unique so the platform can switch a
    # customer to a plan by code without an extra round-trip to
    # resolve the UUID.
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    # Human-readable name shown on the dashboard and the
    # invoice. Spanish so the customer-facing copy matches the
    # rest of the dashboard.
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Short marketing blurb (one sentence). Optional so the
    # seed migration can leave it empty; the dashboard falls
    # back to a default copy block when the field is ``None``.
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Pricing ------------------------------------------------------
    # Monthly fee in CLP. The integer is required by the SII –
    # electronic invoices cannot carry sub-peso amounts. Set to
    # ``0`` for a free tier (the MVP does not ship one, but the
    # column shape supports it).
    price_clp: Mapped[int] = mapped_column(Integer, nullable=False)

    # Messages included in the monthly fee. ``None`` means
    # "unlimited" – used by the Enterprise plan where usage is
    # negotiated bilaterally.
    msg_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Per-message price (CLP) for messages that exceed
    # :attr:`msg_limit`. ``None`` when overage is not charged
    # per-message (Enterprise). The value is stored as an
    # integer because the SII still requires integer CLP on
    # invoices; the per-message calculation rounds up.
    extra_msg_price: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Lifecycle ----------------------------------------------------
    # Whether new customers can subscribe to this plan. Set to
    # ``False`` to retire a plan from the public catalog while
    # keeping the row around so existing customers keep
    # being invoiced.
    active: Mapped[bool] = mapped_column(nullable=False, default=True)

    # Display order on the pricing page. Lower values render
    # first; the seed data uses 10 / 20 / 30 so a future
    # "Starter+" tier can land at 15 without renumbering.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    # How often a subscription is invoiced. The MVP is
    # monthly-only; the column is exposed so a future
    # "annual with 2 months free" package is a config change.
    billing_period: Mapped[PlanBillingPeriod] = mapped_column(
        String(20),
        nullable=False,
        default=PlanBillingPeriod.MONTHLY,
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
        return (
            f"Plan(id={self.id!r}, code={self.code!r}, name={self.name!r}, "
            f"price_clp={self.price_clp!r}, msg_limit={self.msg_limit!r})"
        )

    def __init__(self, **kwargs: object) -> None:
        """Initialise a :class:`Plan` and pre-fill the UUID primary key.

        Mirrors :meth:`Client.__init__`: the ``id`` is set
        eagerly so the application code never has to wait for a
        commit to reference the row.
        """
        if "id" not in kwargs:
            kwargs["id"] = _new_plan_id()
        super().__init__(**kwargs)
