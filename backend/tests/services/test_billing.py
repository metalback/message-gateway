"""Unit tests for the billing domain service.

The tests exercise :mod:`app.services.billing` against the
in-memory SQLite fixture so the SQLAlchemy ORM round-trip
behaviour is verified. The DTE / Flow collaborators are
stubbed so no network is required.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

import pytest

from app.config import Settings
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.invoice import Invoice, InvoiceStatus
from app.models.message import Message, MessageStatus
from app.models.payment import PaymentStatus
from app.models.plan import Plan, PlanBillingPeriod
from app.services.billing import (
    BalanceSummary,
    BillingError,
    InvoiceAlreadyIssuedError,
    InvoiceNotFoundError,
    PlanNotFoundError,
    apply_flow_status,
    compute_invoice,
    create_payment,
    finalize_invoice,
    get_balance,
    get_invoice,
    get_plan_by_code,
    list_invoices,
    list_payments,
    list_plans,
    persist_invoice_draft,
    refresh_payment_status,
    switch_subscription,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_settings() -> Settings:
    """Settings with predictable billing defaults for unit tests."""
    return Settings(
        secret_key="test-secret",
        jwt_secret="test-jwt-secret",
        bcrypt_rounds=4,
        billing_iva_rate=0.19,
        billing_due_days=30,
        flow_api_key="test-flow-key",
        flow_secret_key="test-flow-secret",
        flow_base_url="https://sandbox.flow.cl/api",
    )


@pytest.fixture
async def starter_plan(async_session) -> Plan:
    """Seed the ``starter`` plan (1,000 msgs, CLP 19,990, overage CLP 25)."""
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
    return plan


@pytest.fixture
async def growth_plan(async_session) -> Plan:
    """Seed the ``growth`` plan (10,000 msgs, CLP 79,990, overage CLP 18)."""
    plan = Plan(
        id="00000000-0000-0000-0000-000000000002",
        code="growth",
        name="Growth",
        description="10.000 mensajes al mes",
        price_clp=79990,
        msg_limit=10000,
        extra_msg_price=18,
        active=True,
        sort_order=20,
        billing_period=PlanBillingPeriod.MONTHLY,
    )
    async_session.add(plan)
    await async_session.commit()
    await async_session.refresh(plan)
    return plan


@pytest.fixture
async def enterprise_plan(async_session) -> Plan:
    """Seed the ``enterprise`` plan (unlimited)."""
    plan = Plan(
        id="00000000-0000-0000-0000-000000000003",
        code="enterprise",
        name="Enterprise",
        description="Volumen ilimitado",
        price_clp=0,
        msg_limit=None,
        extra_msg_price=None,
        active=True,
        sort_order=30,
        billing_period=PlanBillingPeriod.MONTHLY,
    )
    async_session.add(plan)
    await async_session.commit()
    await async_session.refresh(plan)
    return plan


@pytest.fixture
async def starter_client(async_session, starter_plan: Plan) -> Client:
    """A registered client on the starter plan."""
    client = Client(
        name="Acme SpA",
        email="ops@acme.cl",
        rut="12345678-5",
        password_hash="h",
        api_key_hash="h",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(client)
    await async_session.commit()
    await async_session.refresh(client)
    return client


def _make_message(
    client_id: str,
    *,
    status: str,
    created_at: datetime,
    fee: int = 0,
) -> Message:
    """Build a :class:`Message` row with the minimum fields the service reads."""
    return Message(
        client_id=client_id,
        channel="sms",
        to_hash="hashed-recipient",
        body="hello",
        status=status,
        fee_clp=fee,
        cost_clp=0,
        created_at=created_at,
    )


async def _seed_messages(
    async_session, *, client_id: str, statuses: Iterable[str]
) -> list[Message]:
    """Insert a batch of messages with the given statuses, all dated today."""
    now = datetime.now(tz=UTC)
    rows = [_make_message(client_id, status=s, created_at=now) for s in statuses]
    async_session.add_all(rows)
    await async_session.commit()
    return rows


# ---------------------------------------------------------------------------
# list_plans / get_plan_by_code
# ---------------------------------------------------------------------------


async def test_list_plans_returns_only_active_in_order(
    async_session, starter_plan, growth_plan
) -> None:
    """Inactive plans are hidden; ordering matches ``sort_order``."""
    growth_plan.active = False
    await async_session.commit()

    plans = await list_plans(async_session)
    assert [plan.code for plan in plans] == ["starter"]


async def test_get_plan_by_code_returns_matching_plan(
    async_session, starter_plan
) -> None:
    """The code is the public lookup key for the catalog."""
    plan = await get_plan_by_code(async_session, "starter")
    assert plan.id == starter_plan.id


async def test_get_plan_by_code_rejects_unknown_plan(async_session) -> None:
    """An unknown code raises :class:`PlanNotFoundError`."""
    with pytest.raises(PlanNotFoundError) as exc:
        await get_plan_by_code(async_session, "does-not-exist")
    assert exc.value.code == "plan_not_found"


async def test_get_plan_by_code_rejects_empty(async_session) -> None:
    """An empty code is treated as a missing plan (no SQL query)."""
    with pytest.raises(PlanNotFoundError):
        await get_plan_by_code(async_session, "")


async def test_get_plan_by_code_rejects_inactive(
    async_session, starter_plan
) -> None:
    """Retired plans cannot be subscribed to."""
    starter_plan.active = False
    await async_session.commit()
    with pytest.raises(PlanNotFoundError):
        await get_plan_by_code(async_session, "starter")


# ---------------------------------------------------------------------------
# switch_subscription
# ---------------------------------------------------------------------------


async def test_switch_subscription_updates_client_plan(
    async_session, starter_client, growth_plan
) -> None:
    """A successful switch changes the client's plan to the new code."""
    plan = await switch_subscription(
        async_session, client=starter_client, plan_code="growth"
    )
    assert plan.code == "growth"
    assert starter_client.plan == ClientPlan.GROWTH


async def test_switch_subscription_rejects_unknown_plan(
    async_session, starter_client
) -> None:
    """Switching to a non-existent plan raises :class:`PlanNotFoundError`."""
    with pytest.raises(PlanNotFoundError):
        await switch_subscription(
            async_session, client=starter_client, plan_code="ghost"
        )
    # The original plan is preserved when the switch fails.
    assert starter_client.plan == ClientPlan.STARTER


# ---------------------------------------------------------------------------
# compute_invoice
# ---------------------------------------------------------------------------


async def test_compute_invoice_under_limit_includes_only_monthly_fee(
    async_session, fast_settings, starter_client
) -> None:
    """Usage under the plan's limit yields a single line item (the fee)."""
    await _seed_messages(
        async_session,
        client_id=starter_client.id,
        statuses=[MessageStatus.SENT.value] * 250,
    )

    draft = await compute_invoice(
        async_session,
        client=starter_client,
        period=date(2026, 6, 15),
        settings=fast_settings,
    )

    assert draft.invoice.subtotal_clp == 19990
    assert draft.invoice.iva_clp == int(round(19990 * 0.19))  # 3798
    assert draft.invoice.total_clp == 19990 + int(round(19990 * 0.19))
    assert draft.invoice.included_msgs == 250
    assert draft.invoice.overage_msgs == 0
    assert len(draft.line_items) == 1
    assert draft.line_items[0].description == "Plan Starter (mensual)"


async def test_compute_invoice_at_exact_limit_has_no_overage(
    async_session, fast_settings, starter_client
) -> None:
    """Exactly the plan's limit is treated as fully included."""
    await _seed_messages(
        async_session,
        client_id=starter_client.id,
        statuses=[MessageStatus.SENT.value] * 1000,
    )

    draft = await compute_invoice(
        async_session,
        client=starter_client,
        period=date(2026, 6, 15),
        settings=fast_settings,
    )

    assert draft.invoice.included_msgs == 1000
    assert draft.invoice.overage_msgs == 0
    assert draft.invoice.subtotal_clp == 19990


async def test_compute_invoice_over_limit_includes_overage(
    async_session, fast_settings, starter_client
) -> None:
    """Usage above the limit adds a per-message overage line item."""
    await _seed_messages(
        async_session,
        client_id=starter_client.id,
        statuses=[MessageStatus.SENT.value] * 1500,
    )

    draft = await compute_invoice(
        async_session,
        client=starter_client,
        period=date(2026, 6, 15),
        settings=fast_settings,
    )

    assert draft.invoice.included_msgs == 1000
    assert draft.invoice.overage_msgs == 500
    # 19,990 (fee) + 500 * 25 (overage) = 32,490
    assert draft.invoice.subtotal_clp == 19990 + 500 * 25
    assert len(draft.line_items) == 2
    overage_line = draft.line_items[1]
    assert overage_line.quantity == 500
    assert overage_line.unit_price_clp == 25
    assert overage_line.total_clp == 12500


async def test_compute_invoice_excludes_non_billable_messages(
    async_session, fast_settings, starter_client
) -> None:
    """Queued / failed / undelivered messages do not count towards billing."""
    statuses = [MessageStatus.SENT.value] * 800
    statuses += [MessageStatus.QUEUED.value] * 100
    statuses += [MessageStatus.FAILED.value] * 50
    statuses += [MessageStatus.UNDELIVERED.value] * 25
    await _seed_messages(
        async_session, client_id=starter_client.id, statuses=statuses
    )

    draft = await compute_invoice(
        async_session,
        client=starter_client,
        period=date(2026, 6, 15),
        settings=fast_settings,
    )

    # Only the 800 ``SENT`` messages count; the rest are free.
    assert draft.invoice.total_msgs == 975
    assert draft.invoice.included_msgs == 800
    assert draft.invoice.overage_msgs == 0
    assert draft.invoice.subtotal_clp == 19990


async def test_compute_invoice_enterprise_has_no_overage(
    async_session, fast_settings, starter_client, enterprise_plan
) -> None:
    """Enterprise customers are billed the (negotiated) fee only."""
    starter_client.plan = ClientPlan.ENTERPRISE
    await async_session.commit()
    await _seed_messages(
        async_session,
        client_id=starter_client.id,
        statuses=[MessageStatus.SENT.value] * 100_000,
    )

    draft = await compute_invoice(
        async_session,
        client=starter_client,
        period=date(2026, 6, 15),
        settings=fast_settings,
    )

    assert draft.invoice.subtotal_clp == enterprise_plan.price_clp
    assert draft.invoice.overage_msgs == 0
    # The Enterprise plan ships at CLP 0 in the MVP – the
    # negotiation is an offline conversation.
    assert draft.invoice.total_clp == 0


async def test_compute_invoice_period_defaults_to_current_month(
    async_session, fast_settings, starter_client
) -> None:
    """Omitting the ``period`` kwarg computes the current month."""
    await _seed_messages(
        async_session,
        client_id=starter_client.id,
        statuses=[MessageStatus.SENT.value] * 5,
    )

    draft = await compute_invoice(
        async_session, client=starter_client, settings=fast_settings
    )
    today = datetime.now(tz=UTC).date()
    assert draft.invoice.period_start == today.replace(day=1)
    last_day = calendar.monthrange(today.year, today.month)[1]
    assert draft.invoice.period_end == today.replace(day=last_day)


async def test_compute_invoice_sets_due_date(
    async_session, fast_settings, starter_client
) -> None:
    """The due date is ``issue_date + billing_due_days``."""
    draft = await compute_invoice(
        async_session,
        client=starter_client,
        period=date(2026, 6, 15),
        settings=fast_settings,
    )
    expected_due = draft.invoice.issue_date + timedelta(days=30)
    assert draft.invoice.due_date == expected_due


async def test_compute_invoice_rejects_invalid_client(
    async_session, fast_settings
) -> None:
    """Passing a non-Client raises :class:`BillingError`."""
    with pytest.raises(BillingError) as exc:
        await compute_invoice(
            async_session,
            client="not-a-client",  # type: ignore[arg-type]
            settings=fast_settings,
        )
    assert exc.value.code == "invalid_client"


# ---------------------------------------------------------------------------
# finalize_invoice
# ---------------------------------------------------------------------------


class _StubDteService:
    """In-memory DTE collaborator for unit tests."""

    def __init__(self, folio: int = 12345) -> None:
        self._folio = folio
        self.calls: list[Invoice] = []

    async def emit(self, *, invoice: Invoice):
        from app.services.dte import DteDocument

        self.calls.append(invoice)
        return DteDocument(
            folio=self._folio,
            url=f"https://factura.msg-gateway.cl/dte/{self._folio}.pdf",
            xml="<DTE/>",
        )


async def test_finalize_invoice_persists_dte_metadata(
    async_session, fast_settings, starter_client
) -> None:
    """After finalization the invoice carries the DTE folio + URL."""
    draft = await compute_invoice(
        async_session,
        client=starter_client,
        period=date(2026, 6, 15),
        settings=fast_settings,
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    stub = _StubDteService(folio=99999)

    finalized = await finalize_invoice(
        async_session,
        invoice=invoice,
        dte_service=stub,  # type: ignore[arg-type]
        settings=fast_settings,
    )

    assert finalized.status == InvoiceStatus.ISSUED
    assert finalized.dte_number == 99999
    assert finalized.dte_url and finalized.dte_url.endswith("99999.pdf")
    assert stub.calls and stub.calls[0].id == invoice.id


async def test_finalize_invoice_rejects_non_draft_invoice(
    async_session, fast_settings, starter_client
) -> None:
    """Re-finalising an already-issued invoice raises :class:`InvoiceAlreadyIssuedError`."""
    draft = await compute_invoice(
        async_session,
        client=starter_client,
        period=date(2026, 6, 15),
        settings=fast_settings,
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    await finalize_invoice(
        async_session, invoice=invoice, dte_service=_StubDteService()  # type: ignore[arg-type]
    )

    with pytest.raises(InvoiceAlreadyIssuedError):
        await finalize_invoice(
            async_session,
            invoice=invoice,
            dte_service=_StubDteService(),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# list_invoices / get_invoice
# ---------------------------------------------------------------------------


async def test_list_invoices_returns_only_client_invoices(
    async_session, fast_settings, starter_client, starter_plan
) -> None:
    """Invoices from another client are not surfaced."""
    other = Client(
        name="Other",
        email="other@x.cl",
        rut="11111111-1",
        password_hash="h",
        api_key_hash="h",
        api_key_last4="zzzz",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(other)
    await async_session.commit()

    mine = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 5, 1)
    )
    mine_inv = await persist_invoice_draft(async_session, draft=mine)
    theirs = await compute_invoice(
        async_session, client=other, period=date(2026, 5, 1)
    )
    await persist_invoice_draft(async_session, draft=theirs)

    invoices = await list_invoices(async_session, client=starter_client)
    assert len(invoices) == 1
    assert invoices[0].id == mine_inv.id


async def test_list_invoices_sorted_newest_first(
    async_session, fast_settings, starter_client
) -> None:
    """The newest invoice is first in the list."""
    a = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 4, 1)
    )
    b = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 5, 1)
    )
    c = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    for d in (a, b, c):
        await persist_invoice_draft(async_session, draft=d)

    invoices = await list_invoices(async_session, client=starter_client)
    assert [i.period_start for i in invoices] == [
        date(2026, 6, 1),
        date(2026, 5, 1),
        date(2026, 4, 1),
    ]


async def test_get_invoice_returns_owned_invoice(
    async_session, fast_settings, starter_client
) -> None:
    """A client can fetch their own invoice by id."""
    draft = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    persisted = await persist_invoice_draft(async_session, draft=draft)

    fetched = await get_invoice(
        async_session, client=starter_client, invoice_id=persisted.id
    )
    assert fetched.id == persisted.id


async def test_get_invoice_rejects_other_clients_invoice(
    async_session, fast_settings, starter_client
) -> None:
    """Fetching another client's invoice is a :class:`InvoiceNotFoundError`."""
    other = Client(
        name="Other",
        email="other@x.cl",
        rut="11111111-1",
        password_hash="h",
        api_key_hash="h",
        api_key_last4="zzzz",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(other)
    await async_session.commit()
    draft = await compute_invoice(
        async_session, client=other, period=date(2026, 6, 1)
    )
    persisted = await persist_invoice_draft(async_session, draft=draft)

    with pytest.raises(InvoiceNotFoundError):
        await get_invoice(
            async_session, client=starter_client, invoice_id=persisted.id
        )


# ---------------------------------------------------------------------------
# get_balance
# ---------------------------------------------------------------------------


async def test_get_balance_returns_plan_and_usage(
    async_session, fast_settings, starter_client
) -> None:
    """The summary carries the plan, the period and the usage counters."""
    await _seed_messages(
        async_session,
        client_id=starter_client.id,
        statuses=[MessageStatus.SENT.value] * 700,
    )
    summary = await get_balance(
        async_session, client=starter_client, settings=fast_settings
    )
    assert isinstance(summary, BalanceSummary)
    assert summary.plan_code == "starter"
    assert summary.msg_limit == 1000
    assert summary.used_msgs == 700
    assert summary.billable_msgs == 700
    assert summary.overage_msgs == 0
    assert summary.estimated_total_clp == int(round(19990 * 1.19))


async def test_get_balance_counts_overage(
    async_session, fast_settings, starter_client
) -> None:
    """Usage above the plan's limit surfaces as ``overage_msgs`` and a cost."""
    await _seed_messages(
        async_session,
        client_id=starter_client.id,
        statuses=[MessageStatus.SENT.value] * 1300,
    )
    summary = await get_balance(
        async_session, client=starter_client, settings=fast_settings
    )
    assert summary.overage_msgs == 300
    assert summary.overage_cost_clp == 300 * 25
    # 19,990 (fee) + 7,500 (overage) = 27,490
    expected_subtotal = 19990 + 300 * 25
    assert summary.estimated_total_clp == int(round(expected_subtotal * 1.19))


# ---------------------------------------------------------------------------
# create_payment
# ---------------------------------------------------------------------------


class _StubFlowClient:
    """In-memory Flow collaborator that returns canned responses."""

    def __init__(
        self,
        token: str = "stub-token",
        redirect_url: str = "https://flow.cl/pay",
    ) -> None:
        self._token = token
        self._redirect_url = redirect_url
        self.create_calls: list[dict] = []
        self.status_calls: list[str] = []

    async def create_order(self, *, commerce_order, subject, amount_clp, email):
        from app.adapters.flow import FlowOrder

        self.create_calls.append(
            {
                "commerce_order": commerce_order,
                "subject": subject,
                "amount_clp": amount_clp,
                "email": email,
            }
        )
        return FlowOrder(
            token=self._token,
            redirect_url=self._redirect_url,
            raw_json='{"token": "stub-token"}',
        )

    async def get_status(self, *, token):
        from app.adapters.flow import FlowPaymentStatus

        self.status_calls.append(token)
        return FlowPaymentStatus(status=1, payment_id=None, raw_json="{}")


async def test_create_payment_persists_payment_row(
    async_session, fast_settings, starter_client
) -> None:
    """A successful Flow order creation lands in the ``pagos`` table."""
    draft = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    flow = _StubFlowClient()

    payment = await create_payment(
        async_session,
        client=starter_client,
        invoice=invoice,
        flow_client=flow,  # type: ignore[arg-type]
        settings=fast_settings,
    )

    assert payment.id
    assert payment.flow_token == "stub-token"
    assert payment.amount_clp == invoice.total_clp
    assert payment.status == PaymentStatus.PENDING
    assert payment.invoice_id == invoice.id
    assert invoice.flow_invoice_id == f"INV-{invoice.id}"
    assert flow.create_calls[0]["amount_clp"] == invoice.total_clp


async def test_create_payment_rejects_invoice_for_another_client(
    async_session, fast_settings, starter_client
) -> None:
    """An invoice belonging to a different client is rejected."""
    other = Client(
        name="Other",
        email="other@x.cl",
        rut="11111111-1",
        password_hash="h",
        api_key_hash="h",
        api_key_last4="zzzz",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(other)
    await async_session.commit()
    draft = await compute_invoice(
        async_session, client=other, period=date(2026, 6, 1)
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)

    with pytest.raises(BillingError) as exc:
        await create_payment(
            async_session,
            client=starter_client,
            invoice=invoice,
            flow_client=_StubFlowClient(),  # type: ignore[arg-type]
            settings=fast_settings,
        )
    assert exc.value.code == "invoice_mismatch"


async def test_create_payment_rejects_already_paid_invoice(
    async_session, fast_settings, starter_client
) -> None:
    """A ``PAID`` invoice cannot be paid again."""
    draft = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    invoice.status = InvoiceStatus.PAID
    await async_session.commit()

    with pytest.raises(InvoiceAlreadyIssuedError):
        await create_payment(
            async_session,
            client=starter_client,
            invoice=invoice,
            flow_client=_StubFlowClient(),  # type: ignore[arg-type]
            settings=fast_settings,
        )


# ---------------------------------------------------------------------------
# apply_flow_status
# ---------------------------------------------------------------------------


async def test_apply_flow_status_marks_payment_paid_and_invoice_paid(
    async_session, fast_settings, starter_client
) -> None:
    """Flow's ``status=2`` flips the local payment and the invoice to ``paid``."""
    draft = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    payment = await create_payment(
        async_session,
        client=starter_client,
        invoice=invoice,
        flow_client=_StubFlowClient(),  # type: ignore[arg-type]
        settings=fast_settings,
    )

    from app.adapters.flow import FlowPaymentStatus

    reconciled = await apply_flow_status(
        async_session,
        payment=payment,
        flow_status=FlowPaymentStatus(status=2, payment_id="flow-abc", raw_json=""),
    )

    assert reconciled.status == PaymentStatus.PAID
    assert reconciled.flow_payment_id == "flow-abc"
    assert reconciled.confirmed_at is not None

    await async_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.PAID
    assert invoice.paid_at is not None


async def test_apply_flow_status_marks_payment_failed(
    async_session, fast_settings, starter_client
) -> None:
    """Flow's ``status=3`` flips the local payment to ``failed``."""
    draft = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    payment = await create_payment(
        async_session,
        client=starter_client,
        invoice=invoice,
        flow_client=_StubFlowClient(),  # type: ignore[arg-type]
        settings=fast_settings,
    )

    from app.adapters.flow import FlowPaymentStatus

    reconciled = await apply_flow_status(
        async_session,
        payment=payment,
        flow_status=FlowPaymentStatus(status=3, payment_id=None, raw_json=""),
    )
    assert reconciled.status == PaymentStatus.FAILED


async def test_apply_flow_status_marks_payment_cancelled(
    async_session, fast_settings, starter_client
) -> None:
    """Flow's ``status=4`` maps to the ``CANCELLED`` enum value."""
    draft = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    payment = await create_payment(
        async_session,
        client=starter_client,
        invoice=invoice,
        flow_client=_StubFlowClient(),  # type: ignore[arg-type]
        settings=fast_settings,
    )

    from app.adapters.flow import FlowPaymentStatus

    reconciled = await apply_flow_status(
        async_session,
        payment=payment,
        flow_status=FlowPaymentStatus(status=4, payment_id=None, raw_json=""),
    )
    assert reconciled.status == PaymentStatus.CANCELLED


async def test_apply_flow_status_marks_payment_expired(
    async_session, fast_settings, starter_client
) -> None:
    """Flow's ``status=5`` maps to the ``EXPIRED`` enum value."""
    draft = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    payment = await create_payment(
        async_session,
        client=starter_client,
        invoice=invoice,
        flow_client=_StubFlowClient(),  # type: ignore[arg-type]
        settings=fast_settings,
    )

    from app.adapters.flow import FlowPaymentStatus

    reconciled = await apply_flow_status(
        async_session,
        payment=payment,
        flow_status=FlowPaymentStatus(status=5, payment_id=None, raw_json=""),
    )
    assert reconciled.status == PaymentStatus.EXPIRED


async def test_apply_flow_status_leaves_pending_payment_alone(
    async_session, fast_settings, starter_client
) -> None:
    """A pending Flow status does not mutate the local payment."""
    draft = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    payment = await create_payment(
        async_session,
        client=starter_client,
        invoice=invoice,
        flow_client=_StubFlowClient(),  # type: ignore[arg-type]
        settings=fast_settings,
    )

    from app.adapters.flow import FlowPaymentStatus

    reconciled = await apply_flow_status(
        async_session,
        payment=payment,
        flow_status=FlowPaymentStatus(status=1, payment_id=None, raw_json=""),
    )
    assert reconciled.status == PaymentStatus.PENDING


# ---------------------------------------------------------------------------
# refresh_payment_status
# ---------------------------------------------------------------------------


class _StatusFlowClient(_StubFlowClient):
    """Stub whose ``get_status`` returns a configurable numeric state."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self._status = status

    async def get_status(self, *, token):
        from app.adapters.flow import FlowPaymentStatus

        self.status_calls.append(token)
        return FlowPaymentStatus(status=self._status, payment_id="flow-1", raw_json="{}")


async def test_refresh_payment_status_calls_flow_for_pending_payment(
    async_session, fast_settings, starter_client
) -> None:
    """A pending payment triggers a Flow status query and is updated."""
    draft = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    payment = await create_payment(
        async_session,
        client=starter_client,
        invoice=invoice,
        flow_client=_StubFlowClient(),  # type: ignore[arg-type]
        settings=fast_settings,
    )

    flow = _StatusFlowClient(status=2)
    refreshed = await refresh_payment_status(
        async_session,
        client=starter_client,
        payment_id=payment.id,
        flow_client=flow,  # type: ignore[arg-type]
        settings=fast_settings,
    )
    assert refreshed.status == PaymentStatus.PAID
    assert flow.status_calls == [payment.flow_token]


async def test_refresh_payment_status_skips_terminal_states(
    async_session, fast_settings, starter_client
) -> None:
    """A payment in a terminal state is returned as-is, without a Flow call."""
    draft = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    payment = await create_payment(
        async_session,
        client=starter_client,
        invoice=invoice,
        flow_client=_StubFlowClient(),  # type: ignore[arg-type]
        settings=fast_settings,
    )
    payment.status = PaymentStatus.CANCELLED
    await async_session.commit()

    flow = _StatusFlowClient(status=2)
    refreshed = await refresh_payment_status(
        async_session,
        client=starter_client,
        payment_id=payment.id,
        flow_client=flow,  # type: ignore[arg-type]
        settings=fast_settings,
    )
    assert refreshed.status == PaymentStatus.CANCELLED
    assert flow.status_calls == []


async def test_refresh_payment_status_rejects_unknown_payment(
    async_session, fast_settings, starter_client
) -> None:
    """An unknown payment id raises :class:`BillingError`."""
    with pytest.raises(BillingError) as exc:
        await refresh_payment_status(
            async_session,
            client=starter_client,
            payment_id="ghost",
            flow_client=_StatusFlowClient(status=2),  # type: ignore[arg-type]
            settings=fast_settings,
        )
    assert exc.value.code == "payment_not_found"


# ---------------------------------------------------------------------------
# list_payments
# ---------------------------------------------------------------------------


async def test_list_payments_returns_only_client_payments(
    async_session, fast_settings, starter_client
) -> None:
    """The list is scoped to the authenticated client."""
    draft = await compute_invoice(
        async_session, client=starter_client, period=date(2026, 6, 1)
    )
    invoice = await persist_invoice_draft(async_session, draft=draft)
    await create_payment(
        async_session,
        client=starter_client,
        invoice=invoice,
        flow_client=_StubFlowClient(),  # type: ignore[arg-type]
        settings=fast_settings,
    )

    payments = await list_payments(async_session, client=starter_client)
    assert len(payments) == 1
    assert payments[0].invoice_id == invoice.id
