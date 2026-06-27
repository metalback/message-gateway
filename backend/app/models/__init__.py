"""SQLAlchemy ORM models.

One model per file under ``app/models/`` (e.g. ``client.py``,
``message.py``). Database tables follow the schema described in
``PRD.md`` §"Modelo de datos".

The base class lives in ``app.models.base`` and is the single
source of truth for the declarative ``metadata`` Alembic reads
when generating migrations.

Model modules are imported here for their side effects: every
``Base`` subclass must register itself in
``app.models.base.Base.metadata`` so the autogenerate pass in
:mod:`app.alembic.env` can pick the table up. Keeping the
imports in a single place also gives contributors a single
location to discover the full set of tables in the database.
"""

from app.models.base import Base
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.invoice import Invoice, InvoiceStatus, InvoiceType
from app.models.message import BILLABLE_STATUSES, Message, MessageChannel, MessageStatus
from app.models.payment import Payment, PaymentStatus
from app.models.plan import Plan, PlanBillingPeriod

__all__ = (
    "Base",
    "Client",
    "ClientPlan",
    "ClientStatus",
    "Invoice",
    "InvoiceStatus",
    "InvoiceType",
    "Message",
    "MessageChannel",
    "MessageStatus",
    "BILLABLE_STATUSES",
    "Payment",
    "PaymentStatus",
    "Plan",
    "PlanBillingPeriod",
)
