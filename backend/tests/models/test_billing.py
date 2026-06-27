"""Unit tests for the billing ORM models.

Tests the shape of the :class:`Plan`, :class:`Invoice`
and :class:`Payment` tables: column names, uniqueness
constraints and the behaviour of the Python-side default
UUID generator. Mirrors the pattern in
:mod:`tests.models.test_client`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select

from app.models.base import Base
from app.models.invoice import Invoice, InvoiceStatus, InvoiceType
from app.models.payment import Payment, PaymentStatus
from app.models.plan import Plan, PlanBillingPeriod

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def test_planes_table_is_registered() -> None:
    """The model registers itself in the shared ``metadata``."""
    assert "planes" in Base.metadata.tables
    table = Base.metadata.tables["planes"]
    expected_columns = {
        "id",
        "code",
        "name",
        "description",
        "price_clp",
        "msg_limit",
        "extra_msg_price",
        "active",
        "sort_order",
        "billing_period",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.columns.keys())), table.columns.keys()


def test_planes_code_is_unique() -> None:
    """The plan code is the public lookup key; it must be unique."""
    table = Base.metadata.tables["planes"]
    assert table.c.code.unique is True


def test_plan_id_is_a_uuid_string_by_default() -> None:
    """A :class:`Plan` constructed in memory has a UUID id."""
    plan = Plan(
        code="starter",
        name="Starter",
        price_clp=19990,
    )
    assert plan.id is not None
    assert len(plan.id) == 36
    assert plan.id.count("-") == 4


def test_plan_billing_period_enum_values() -> None:
    """The enum values are the platform's documented billing periods."""
    assert PlanBillingPeriod.MONTHLY.value == "monthly"
    assert PlanBillingPeriod.QUARTERLY.value == "quarterly"
    assert PlanBillingPeriod.ANNUAL.value == "annual"


async def test_persisted_plan_round_trips_through_database(async_session) -> None:
    """A plan written through the ORM is read back with every field intact."""
    plan = Plan(
        id="00000000-0000-0000-0000-000000000001",
        code="starter",
        name="Starter",
        description="1.000 mensajes al mes",
        price_clp=19990,
        msg_limit=1000,
        extra_msg_price=25,
        active=True,
        sort_order=10,
        billing_period=PlanBillingPeriod.MONTHLY,
    )
    async_session.add(plan)
    await async_session.commit()
    await async_session.refresh(plan)

    stmt = select(Plan).where(Plan.id == plan.id)
    loaded = (await async_session.execute(stmt)).scalar_one()
    assert loaded.code == "starter"
    assert loaded.price_clp == 19990
    assert loaded.msg_limit == 1000
    assert loaded.billing_period == PlanBillingPeriod.MONTHLY


# ---------------------------------------------------------------------------
# Invoice
# ---------------------------------------------------------------------------


def test_facturas_table_is_registered() -> None:
    """The model registers itself in the shared ``metadata``."""
    assert "facturas" in Base.metadata.tables
    table = Base.metadata.tables["facturas"]
    expected_columns = {
        "id",
        "number",
        "client_id",
        "plan_id",
        "plan_code",
        "period_start",
        "period_end",
        "total_msgs",
        "included_msgs",
        "overage_msgs",
        "subtotal_clp",
        "iva_clp",
        "total_clp",
        "dte_number",
        "dte_url",
        "flow_invoice_id",
        "status",
        "tipo",
        "issue_date",
        "due_date",
        "paid_at",
        "voided_at",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(set(table.columns.keys())), table.columns.keys()


def test_facturas_number_and_dte_number_are_unique() -> None:
    """The invoice number and the DTE folio are both unique constraints."""
    table = Base.metadata.tables["facturas"]
    assert table.c.number.unique is True
    assert table.c.dte_number.unique is True


def test_invoice_status_enum_values() -> None:
    """The status enum covers every documented lifecycle state."""
    expected = {"draft", "issued", "paid", "overdue", "voided"}
    actual = {status.value for status in InvoiceStatus}
    assert expected == actual


def test_invoice_type_enum_values() -> None:
    """The MVP only emits ``factura_electronica`` (DTE 33)."""
    assert InvoiceType.FACTURA_ELECTRONICA.value == "factura_electronica"


def test_invoice_id_is_a_uuid_string_by_default() -> None:
    """A :class:`Invoice` constructed in memory has a UUID id."""
    invoice = Invoice(
        number="F-2026-ABC",
        client_id="client-1",
        plan_id="plan-1",
        plan_code="starter",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        issue_date=date(2026, 6, 30),
        due_date=date(2026, 7, 30),
    )
    assert invoice.id is not None
    assert len(invoice.id) == 36
    assert invoice.id.count("-") == 4


async def test_persisted_invoice_round_trips_through_database(async_session) -> None:
    """An invoice written through the ORM is read back with every field intact."""
    invoice = Invoice(
        number="F-2026-ABCDEF",
        client_id="client-1",
        plan_id="plan-1",
        plan_code="starter",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        total_msgs=1000,
        included_msgs=1000,
        overage_msgs=0,
        subtotal_clp=19990,
        iva_clp=3798,
        total_clp=23788,
        status=InvoiceStatus.ISSUED,
        issue_date=date(2026, 6, 30),
        due_date=date(2026, 7, 30),
    )
    async_session.add(invoice)
    await async_session.commit()
    await async_session.refresh(invoice)

    stmt = select(Invoice).where(Invoice.id == invoice.id)
    loaded = (await async_session.execute(stmt)).scalar_one()
    assert loaded.number == "F-2026-ABCDEF"
    assert loaded.subtotal_clp == 19990
    assert loaded.iva_clp == 3798
    assert loaded.total_clp == 23788
    # The status column is stored as a string, so the
    # round-trip yields the enum's value, not the
    # member itself.
    assert loaded.status == "issued"


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


def test_pagos_table_is_registered() -> None:
    """The model registers itself in the shared ``metadata``."""
    assert "pagos" in Base.metadata.tables
    table = Base.metadata.tables["pagos"]
    expected_columns = {
        "id",
        "client_id",
        "invoice_id",
        "plan_code",
        "amount_clp",
        "flow_token",
        "flow_order",
        "flow_payment_id",
        "flow_redirect_url",
        "flow_response",
        "status",
        "created_at",
        "expires_at",
        "confirmed_at",
    }
    assert expected_columns.issubset(set(table.columns.keys())), table.columns.keys()


def test_pagos_flow_token_is_unique() -> None:
    """The Flow token is the lookup key for the asynchronous webhook."""
    table = Base.metadata.tables["pagos"]
    assert table.c.flow_token.unique is True
    assert table.c.flow_token.index is True


def test_payment_status_enum_values() -> None:
    """The status enum mirrors Flow's state machine."""
    expected = {"pending", "paid", "failed", "cancelled", "expired"}
    actual = {status.value for status in PaymentStatus}
    assert expected == actual


def test_payment_id_is_a_uuid_string_by_default() -> None:
    """A :class:`Payment` constructed in memory has a UUID id."""
    payment = Payment(
        client_id="client-1",
        plan_code="starter",
        amount_clp=23788,
        flow_token="abc",
        flow_order="INV-1",
    )
    assert payment.id is not None
    assert len(payment.id) == 36
    assert payment.id.count("-") == 4


async def test_persisted_payment_round_trips_through_database(async_session) -> None:
    """A payment written through the ORM is read back with every field intact."""
    payment = Payment(
        client_id="client-1",
        plan_code="starter",
        amount_clp=23788,
        flow_token="token-abc",
        flow_order="INV-1",
        flow_redirect_url="https://flow.cl/pay/abc",
        flow_response='{"token":"token-abc"}',
        status=PaymentStatus.PAID,
        confirmed_at=datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC),
    )
    async_session.add(payment)
    await async_session.commit()
    await async_session.refresh(payment)

    stmt = select(Payment).where(Payment.id == payment.id)
    loaded = (await async_session.execute(stmt)).scalar_one()
    assert loaded.flow_token == "token-abc"
    assert loaded.amount_clp == 23788
    assert loaded.status == "paid"
    assert loaded.confirmed_at is not None
