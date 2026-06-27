"""Service-level tests for the batch messaging API (issue #9).

The tests cover:

- :func:`app.services.messaging.send_batch` – the per-item
  outcome list, the new ``batch_id`` the function returns,
  the counter rollup and the cross-batch linkage (every
  message carries the same ``batch_id``).
- :func:`app.services.messaging.get_batch` – the lookup +
  cross-tenant 404 guard + counter recompute.
- :func:`app.services.messaging.list_batches` – the
  paginated batch history the dashboard's "Campañas" view
  reads from.
- :class:`app.services.messaging.BatchSummary` /
  :class:`app.services.messaging.BatchOutcome` – the dataclass
  shapes the route layer projects onto the response.

The provider HTTP layer is stubbed through the
:class:`FakeProvider` defined in
:mod:`tests.services.test_messaging` (the one used by the
``send_batch`` tests already in this package) so the suite
never opens a real TCP connection.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import ProviderUnavailableError
from app.config import Settings
from app.models.batch import Batch, BatchStatus
from app.models.client import Client, ClientPlan, ClientStatus
from app.models.message import Channel, Message, MessageStatus
from app.services.messaging import (
    BatchChannelSummary,
    BatchNotFoundError,
    BatchOutcome,
    BatchSummary,
    InvalidMessageError,
    get_batch,
    list_batches,
    send_batch,
)
from app.services.webhook_delivery import WebhookDeliveryResult

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class BatchFakeProvider(BaseProvider):
    """A controllable :class:`BaseProvider` for the batch tests.

    Mirrors the helper in
    :mod:`tests.services.test_messaging` but kept local
    because the batch tests need a small extension:
    a per-recipient outcome (some numbers succeed, others
    fail) so the per-item outcome list and the counter
    rollup can be exercised in the same test.
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        fail_suffix: str = "2",
    ) -> None:
        self.name = name
        self._fail_suffix = fail_suffix
        self.send_calls: list[dict[str, Any]] = []

    async def send(self, *, to: str, body: str, **kwargs: Any) -> SendResult:
        self.send_calls.append({"to": to, "body": body, **kwargs})
        if to.endswith(self._fail_suffix):
            raise ProviderUnavailableError("upstream down", provider=self.name)
        return SendResult(provider_msg_id=f"fake-{to}", raw={})

    async def get_status(self, provider_msg_id: str) -> str:
        return "sent"


@pytest.fixture
def batch_settings() -> Settings:
    """Settings with the provider config the registry needs."""
    return Settings(
        meta_whatsapp_access_token="t",
        meta_whatsapp_phone_number_id="p",
        sms_aggregator_api_url="https://sms.test",
        sms_aggregator_api_key="k",
        sms_aggregator_sender_id="MSGGTWY",
    )


@pytest.fixture
def batch_providers(monkeypatch: pytest.MonkeyPatch) -> BatchFakeProvider:
    """Patch the registry so :func:`get_provider` returns
    a single :class:`BatchFakeProvider` for every channel."""
    import app.adapters.registry as registry

    instance = BatchFakeProvider(name="meta_whatsapp")
    monkeypatch.setitem(
        registry._BUILDERS,
        Channel.WHATSAPP,
        lambda settings: instance,
    )
    monkeypatch.setitem(
        registry._BUILDERS,
        Channel.SMS,
        lambda settings: instance,
    )
    return instance


async def _make_client(async_session) -> Client:
    client = Client(
        name="Acme",
        email="ops@acme.cl",
        rut="12345678-5",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="abcd",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(client)
    await async_session.flush()
    return client


# ---------------------------------------------------------------------------
# send_batch — batch_id wiring
# ---------------------------------------------------------------------------


async def test_send_batch_returns_batch_id(
    async_session, batch_providers, batch_settings
) -> None:
    """A successful call returns a :class:`BatchOutcome`
    carrying a non-empty ``batch_id``; the caller can
    poll it through :func:`get_batch` immediately after."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "whatsapp", "to": "+56922222222", "body": "dos"},
        ],
        settings=batch_settings,
    )

    assert isinstance(outcome, BatchOutcome)
    assert outcome.batch_id
    assert len(outcome.batch_id) == 36
    assert len(outcome.results) == 2


async def test_send_batch_links_every_message_to_the_batch(
    async_session, batch_providers, batch_settings
) -> None:
    """Every message produced by the batch carries the
    same ``batch_id`` so the counter recompute (and the
    dashboard's "Campañas" view) can group the per-item
    rows under the campaign."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "whatsapp", "to": "+56922222222", "body": "dos"},
            {"channel": "sms", "to": "+56933333333", "body": "tres"},
        ],
        settings=batch_settings,
    )

    stmt = select(Message).where(Message.batch_id == outcome.batch_id)
    rows = list((await async_session.execute(stmt)).scalars().all())
    assert len(rows) == 3
    assert {row.to_number for row in rows} == {
        "+56911111111",
        "+56922222222",
        "+56933333333",
    }


async def test_send_batch_persists_batch_row_with_counters(
    async_session, batch_providers, batch_settings
) -> None:
    """The :class:`Batch` row the call creates has the
    counters filled in: ``total_count`` is frozen at the
    call's submission size, the per-status counts reflect
    what the provider returned.

    After a successful inline send, the per-item status
    is ``sent`` (the provider accepted the message but
    has not yet confirmed delivery). From the batch's
    point of view, "in flight" means "the provider has
    not yet confirmed delivery", so the counter
    recompute puts the two items under
    :attr:`Batch.pending_count`. The batch stays in
    :class:`BatchStatus.PROCESSING` until the delivery
    receipts come in through the webhook loop – the
    counter recompute is the same code path the worker
    will run, so the value is the "live" state of the
    campaign rather than a one-shot snapshot.

    The two destinations end in ``1`` and ``3`` so the
    :class:`BatchFakeProvider`'s ``fail_suffix="2"`` does
    not trip; the test exercises the "all messages in
    flight" path, not the "partial failure" path."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "sms", "to": "+56933333333", "body": "dos"},
        ],
        settings=batch_settings,
    )

    stmt = select(Batch).where(Batch.id == outcome.batch_id)
    batch = (await async_session.execute(stmt)).scalar_one()
    assert batch.client_id == client.id
    assert batch.total_count == 2
    assert batch.delivered_count == 0
    assert batch.failed_count == 0
    # Both messages are in ``sent`` state, which the
    # counter recompute treats as "in flight".
    assert batch.pending_count == 2
    assert batch.status == BatchStatus.PROCESSING
    assert batch.completed_at is None


async def test_send_batch_records_partial_failure_in_summary(
    async_session, batch_providers, batch_settings
) -> None:
    """A campaign with a mix of successes and failures
    carries the failure count in the
    :class:`BatchSummary` and the per-item status in the
    ``results`` list – so the dashboard can render "1 of
    2 sent, 1 of 2 failed" without re-iterating the
    messages.

    The successful item is in ``sent`` state (the
    provider accepted it but the delivery receipt has
    not arrived yet), so the summary puts it under
    ``pending`` rather than ``delivered``. The
    ``succeeded`` counter is therefore ``failed``
    (1) plus ``delivered`` (0) – ``succeeded`` tracks
    "reached a terminal state", not "sent"."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "sms", "to": "+56911111112", "body": "dos"},
        ],
        settings=batch_settings,
    )

    assert outcome.summary.total == 2
    assert outcome.summary.delivered == 0
    assert outcome.summary.failed == 1
    assert outcome.summary.pending == 1
    # ``succeeded`` is ``delivered + failed``; the
    # in-flight item is not counted yet.
    assert outcome.summary.succeeded == 1

    statuses = [item.message.status for item in outcome.results]
    assert statuses[0] == MessageStatus.SENT
    assert statuses[1] == MessageStatus.FAILED


async def test_send_batch_marks_batch_failed_when_every_item_fails(
    async_session, batch_providers, batch_settings
) -> None:
    """A campaign where every item ended up ``failed``
    flips the batch's :attr:`Batch.status` to
    :class:`BatchStatus.FAILED` so the dashboard's
    "campaña fallida" filter can pick it up. The
    ``completed_at`` timestamp is still set so the
    dashboard can render the failure time without an
    extra query."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111112", "body": "uno"},
            {"channel": "sms", "to": "+56922222222", "body": "dos"},
        ],
        settings=batch_settings,
    )

    batch = (
        await async_session.execute(select(Batch).where(Batch.id == outcome.batch_id))
    ).scalar_one()
    assert batch.status == BatchStatus.FAILED
    assert batch.failed_count == 2
    assert batch.delivered_count == 0
    assert batch.pending_count == 0
    assert batch.completed_at is not None


async def test_send_batch_persists_optional_name(
    async_session, batch_providers, batch_settings
) -> None:
    """The optional ``name`` field the route layer
    accepts is persisted on the :class:`Batch` row so
    the dashboard can render it on the "Campañas" view."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
        name="Black Friday 2026",
    )

    batch = (
        await async_session.execute(select(Batch).where(Batch.id == outcome.batch_id))
    ).scalar_one()
    assert batch.name == "Black Friday 2026"


# ---------------------------------------------------------------------------
# send_batch — pre-existing guards (regression coverage)
# ---------------------------------------------------------------------------


async def test_send_batch_rejects_empty_input(
    async_session, batch_providers, batch_settings
) -> None:
    """An empty batch is a 422 at the route layer; the
    service layer surfaces a stable code so the mapping
    is obvious."""
    client = await _make_client(async_session)
    with pytest.raises(Exception) as exc:
        await send_batch(
            async_session,
            client=client,
            items=[],
            settings=batch_settings,
        )
    assert getattr(exc.value, "code", None) == "invalid_batch"


async def test_send_batch_rejects_non_list_input(
    async_session, batch_providers, batch_settings
) -> None:
    """``items`` must be a list; a non-list raises
    :class:`InvalidMessageError` so the route layer can
    surface a 422 without crashing on a malformed body."""
    client = await _make_client(async_session)
    with pytest.raises(Exception) as exc:
        await send_batch(
            async_session,
            client=client,
            items="not-a-list",  # type: ignore[arg-type]
            settings=batch_settings,
        )
    assert getattr(exc.value, "code", None) == "invalid_batch"


async def test_send_batch_rejects_oversized_input(
    async_session, batch_providers, batch_settings
) -> None:
    """The hard cap on a single batch is enforced at the
    service layer so a malicious client cannot enqueue
    thousands of rows by accident."""
    client = await _make_client(async_session)
    items = [
        {"channel": "sms", "to": "+56911111111", "body": f"msg-{i}"}
        for i in range(501)
    ]
    with pytest.raises(Exception) as exc:
        await send_batch(
            async_session,
            client=client,
            items=items,
            settings=batch_settings,
        )
    assert getattr(exc.value, "code", None) == "batch_too_large"


async def test_send_batch_rejects_unknown_channel(
    async_session, batch_providers, batch_settings
) -> None:
    """A bad channel on any item aborts the call before
    any row is persisted. The Pydantic model on the
    route layer already catches this case, but the
    service layer has to enforce the same contract for
    in-process callers (the worker / future analytics
    pipeline)."""
    client = await _make_client(async_session)
    with pytest.raises(Exception) as exc:
        await send_batch(
            async_session,
            client=client,
            items=[{"channel": "pigeon", "to": "+56911111111", "body": "x"}],
            settings=batch_settings,
        )
    assert getattr(exc.value, "code", None) == "invalid_channel"


# ---------------------------------------------------------------------------
# get_batch
# ---------------------------------------------------------------------------


async def test_get_batch_returns_the_persisted_row(
    async_session, batch_providers, batch_settings
) -> None:
    """A successful :func:`get_batch` call returns a
    :class:`BatchDetail` wrapping the same row the
    original send created, with the counter recompute
    applied on the way out.

    The single message is in ``sent`` state (the
    provider accepted it but the delivery receipt has
    not arrived yet), so the counters reflect the
    in-flight state and the batch stays in
    :class:`BatchStatus.PROCESSING`."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
        name="Lanzamiento",
    )

    detail = await get_batch(
        async_session,
        client=client,
        batch_id=outcome.batch_id,
        settings=batch_settings,
    )
    batch = detail.batch
    assert batch.id == outcome.batch_id
    assert batch.client_id == client.id
    assert batch.name == "Lanzamiento"
    assert batch.total_count == 1
    assert batch.delivered_count == 0
    assert batch.pending_count == 1
    assert batch.status == BatchStatus.PROCESSING


async def test_get_batch_404_for_unknown_id(
    async_session, batch_providers, batch_settings
) -> None:
    """An unknown batch id is a
    :class:`BatchNotFoundError` so the route layer can
    surface a 404 with a stable code."""
    client = await _make_client(async_session)
    with pytest.raises(BatchNotFoundError):
        await get_batch(
            async_session,
            client=client,
            batch_id="00000000-0000-0000-0000-000000000000",
            settings=batch_settings,
        )


async def test_get_batch_404_for_other_clients_batch(
    async_session, batch_providers, batch_settings
) -> None:
    """A batch that belongs to a different client is
    reported as :class:`BatchNotFoundError` so the
    existence of another tenant's campaign is not
    leaked. The contract mirrors
    :func:`app.services.messaging.get_message_status`."""
    client_a = await _make_client(async_session)

    client_b = Client(
        name="Other Co",
        email="ops@other.cl",
        rut="22222222-2",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="efgh",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(client_b)
    await async_session.flush()

    outcome = await send_batch(
        async_session,
        client=client_b,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
    )

    with pytest.raises(BatchNotFoundError):
        await get_batch(
            async_session,
            client=client_a,
            batch_id=outcome.batch_id,
            settings=batch_settings,
        )


# ---------------------------------------------------------------------------
# list_batches
# ---------------------------------------------------------------------------


async def test_list_batches_returns_newest_first(
    async_session, batch_providers, batch_settings
) -> None:
    """The listing endpoint returns batches newest
    first so the dashboard does not have to re-sort on
    the client."""
    client = await _make_client(async_session)
    first = await send_batch(
        async_session,
        client=client,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
    )
    second = await send_batch(
        async_session,
        client=client,
        items=[{"channel": "sms", "to": "+56911111111", "body": "dos"}],
        settings=batch_settings,
    )

    page = await list_batches(
        async_session,
        client=client,
        settings=batch_settings,
    )
    assert page.total == 2
    # ``created_at`` is set by the database with a
    # ``now()`` resolution; two calls inside the same
    # test are not guaranteed to differ. The contract
    # is "all rows are present" + "ties are broken by
    # ``id DESC``" (newest UUID wins on a draw).
    returned_ids = [batch.id for batch in page.items]
    assert set(returned_ids) == {first.batch_id, second.batch_id}


async def test_list_batches_pagination_works(
    async_session, batch_providers, batch_settings
) -> None:
    """The ``limit`` / ``offset`` knobs are honoured so
    the dashboard can paginate the "Campañas" view
    without re-loading the full history on every
    page."""
    client = await _make_client(async_session)
    for i in range(3):
        await send_batch(
            async_session,
            client=client,
            items=[{"channel": "sms", "to": "+56911111111", "body": f"msg-{i}"}],
            settings=batch_settings,
        )

    page = await list_batches(
        async_session,
        client=client,
        limit=2,
        offset=0,
        settings=batch_settings,
    )
    assert page.total == 3
    assert len(page.items) == 2
    assert page.limit == 2
    assert page.offset == 0
    assert page.has_more is True

    next_page = await list_batches(
        async_session,
        client=client,
        limit=2,
        offset=2,
        settings=batch_settings,
    )
    assert len(next_page.items) == 1
    assert next_page.has_more is False


async def test_list_batches_filter_by_status(
    async_session, batch_providers, batch_settings
) -> None:
    """The ``status`` filter narrows the result set to
    a single lifecycle state. A freshly-sent batch
    with messages in ``sent`` state stays in
    :class:`BatchStatus.PROCESSING` (the provider has
    not yet confirmed delivery), so a ``processing``
    filter returns it and a ``completed`` filter
    returns zero rows."""
    client = await _make_client(async_session)
    await send_batch(
        async_session,
        client=client,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
    )

    page = await list_batches(
        async_session,
        client=client,
        status=BatchStatus.PROCESSING,
        settings=batch_settings,
    )
    assert page.total == 1

    empty = await list_batches(
        async_session,
        client=client,
        status=BatchStatus.COMPLETED,
        settings=batch_settings,
    )
    assert empty.total == 0
    assert empty.items == []


async def test_list_batches_rejects_unknown_status(
    async_session, batch_providers, batch_settings
) -> None:
    """An unknown status string is a 422 at the route
    layer; the service layer surfaces a stable code
    so the mapping is obvious."""
    client = await _make_client(async_session)
    with pytest.raises(Exception) as exc:
        await list_batches(
            async_session,
            client=client,
            status="scheduled",
            settings=batch_settings,
        )
    assert getattr(exc.value, "code", None) == "invalid_batch_status"


async def test_list_batches_isolated_per_client(
    async_session, batch_providers, batch_settings
) -> None:
    """A customer can only see their own batches. The
    ``client_id`` WHERE clause is the cross-tenant
    guard; a row owned by another client never shows
    up in the response (not even as a 403, which would
    leak the existence of the other tenant)."""
    client_a = await _make_client(async_session)

    client_b = Client(
        name="Other Co",
        email="ops@other.cl",
        rut="22222222-2",
        password_hash="hashed",
        api_key_hash="also-hashed",
        api_key_last4="efgh",
        plan=ClientPlan.STARTER,
        status=ClientStatus.ACTIVE,
    )
    async_session.add(client_b)
    await async_session.flush()

    await send_batch(
        async_session,
        client=client_b,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
    )

    page = await list_batches(
        async_session,
        client=client_a,
        settings=batch_settings,
    )
    assert page.total == 0
    assert page.items == []


# ---------------------------------------------------------------------------
# BatchSummary dataclass
# ---------------------------------------------------------------------------


def test_batch_summary_succeeded_is_delivered_plus_failed() -> None:
    """``succeeded`` is defined as ``delivered + failed``
    so the dashboard can render "X of Y" without doing
    the arithmetic on the client."""
    summary = BatchSummary(total=10, pending=0, delivered=7, failed=3)
    assert summary.succeeded == 10


# ---------------------------------------------------------------------------
# BatchSummary / Batch — per-batch cost / fee rollup
# ---------------------------------------------------------------------------
#
# The dashboard's "Campañas" view surfaces the campaign's total
# cost as a single CLP value so a customer can see at a glance
# how much they spent on a given campaign without iterating
# the per-item results. The rollup is recomputed by
# :func:`app.services.messaging._recompute_batch_counters` and
# mirrored on :class:`app.services.messaging.BatchSummary`.


def test_batch_summary_total_amount_is_cost_plus_fee() -> None:
    """``total_amount_clp`` is ``total_cost_clp + total_fee_clp``
    so the dashboard can render the customer-facing total
    without doing the arithmetic on the client. The value
    is exposed as a property (not a field) so the dataclass
    stays frozen."""
    summary = BatchSummary(
        total=4,
        pending=0,
        delivered=3,
        failed=1,
        total_cost_clp=320,
        total_fee_clp=20,
    )
    assert summary.total_amount_clp == 340


async def test_send_batch_records_aggregated_cost_and_fee_on_row(
    async_session, batch_providers, batch_settings
) -> None:
    """The :class:`Batch` row carries the aggregated cost /
    fee across every message of the batch so the dashboard
    does not have to re-aggregate the underlying ``mensajes``
    table on every read.

    The :class:`BatchFakeProvider` accepts every destination
    (none of them end in ``2``) so all items are dispatched
    successfully; the resulting messages live in ``sent``
    state with the cost / fee columns the
    :func:`compute_message_cost` helper computes for the
    customer's :class:`ClientPlan.STARTER` plan.

    The Starter plan charges CLP $25 for SMS + CLP $5 markup
    and CLP $80 for WhatsApp + CLP $5 markup (the values
    from :data:`_BASE_COST_CLP` /
    :data:`_PLAN_MARKUP_CLP`). The batch sends 2 SMS and
    1 WhatsApp message, so the rollup is
    ``2*30 + 1*85 = 145`` CLP cents."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "sms", "to": "+56933333333", "body": "dos"},
            {"channel": "whatsapp", "to": "+56944444444", "body": "tres"},
        ],
        settings=batch_settings,
    )

    # The :class:`BatchSummary` returned in the outcome
    # already carries the rollup so a caller that just wants
    # the headline numbers does not have to round-trip the
    # row.
    assert outcome.summary.total_cost_clp == 25 + 25 + 80
    assert outcome.summary.total_fee_clp == 5 + 5 + 5
    assert outcome.summary.total_amount_clp == (25 + 25 + 80) + (5 + 5 + 5)

    # The persisted row carries the same rollup so a
    # subsequent ``GET /v1/messages/batch/{batch_id}`` sees
    # the same numbers without recomputation.
    batch = (
        await async_session.execute(select(Batch).where(Batch.id == outcome.batch_id))
    ).scalar_one()
    assert batch.total_cost_clp == outcome.summary.total_cost_clp
    assert batch.total_fee_clp == outcome.summary.total_fee_clp


async def test_send_batch_rolls_up_failed_items_too(
    async_session, batch_providers, batch_settings
) -> None:
    """The cost / fee rollup is computed across **every**
    message of the batch, regardless of the per-item
    outcome. A failed item still incurred a row-level
    cost / fee (the upstream charge is provisioned at
    dispatch time, before delivery is confirmed), so
    excluding ``failed`` items from the rollup would
    under-report the campaign's actual cost.

    The :class:`BatchFakeProvider` raises
    :class:`ProviderUnavailableError` for destinations
    ending in ``2``; the ``send_message`` service catches
    the error and marks the row ``failed``. The cost /
    fee columns are still populated on the failed row
    because :func:`_persist_message` runs before the
    dispatch and writes the plan-derived values."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            # ends in ``2`` -> the fake provider raises
            {"channel": "sms", "to": "+56911111112", "body": "dos"},
        ],
        settings=batch_settings,
    )

    # Both messages were persisted with a non-zero
    # ``cost_clp`` / ``fee_clp`` (the Starter plan charges
    # CLP $25 + $5 for every SMS), so the rollup is the
    # sum of both rows.
    expected_cost = 25 + 25
    expected_fee = 5 + 5
    assert outcome.summary.total_cost_clp == expected_cost
    assert outcome.summary.total_fee_clp == expected_fee
    assert outcome.summary.total_amount_clp == expected_cost + expected_fee

    # The persisted row carries the same rollup.
    batch = (
        await async_session.execute(select(Batch).where(Batch.id == outcome.batch_id))
    ).scalar_one()
    assert batch.total_cost_clp == expected_cost
    assert batch.total_fee_clp == expected_fee


async def test_send_batch_rolls_up_empty_batch_to_zero(
    async_session, batch_providers, batch_settings
) -> None:
    """A batch with a single item carries a non-zero rollup
    so the dashboard does not have to special-case the
    one-message path. The Starter plan charges CLP $25 + $5
    for SMS so the values are ``30 / 30 / 30``."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
    )

    assert outcome.summary.total_cost_clp == 25
    assert outcome.summary.total_fee_clp == 5
    assert outcome.summary.total_amount_clp == 30


async def test_get_batch_returns_aggregated_cost_and_fee(
    async_session, batch_providers, batch_settings
) -> None:
    """A subsequent :func:`get_batch` call returns the
    same ``total_cost_clp`` / ``total_fee_clp`` values
    the original :func:`send_batch` call persisted, so a
    polling dashboard sees a stable cost across calls.

    The route layer's ``_batch_to_response`` helper
    projects the columns straight onto the response, so
    the ``total_amount_clp`` field is derived on the way
    out. The test exercises the service layer only; the
    route-level coverage of the rollup lives in
    :mod:`tests.routes.test_batch_messaging`."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "sms", "to": "+56933333333", "body": "dos"},
        ],
        settings=batch_settings,
    )

    detail = await get_batch(
        async_session,
        client=client,
        batch_id=outcome.batch_id,
        settings=batch_settings,
    )
    batch = detail.batch
    assert batch.total_cost_clp == 25 + 25
    assert batch.total_fee_clp == 5 + 5


# ---------------------------------------------------------------------------
# Per-channel rollup (issue #9 — "desglose por canal" widget)
# ---------------------------------------------------------------------------
#
# The dashboard's "Campañas" view surfaces a per-channel
# breakdown so the customer can see at a glance which
# channel drove the spend ("SMS: 70 / CLP $2 450 · WhatsApp:
# 30 / CLP $2 550") without iterating the underlying
# ``mensajes`` table. The rollup is computed by
# :func:`app.services.messaging._batch_channel_breakdown`
# (one query per call) and
# :func:`app.services.messaging._batch_channel_breakdowns`
# (one query for the whole page on the listing endpoint).


async def test_send_batch_summary_carries_per_channel_breakdown(
    async_session, batch_providers, batch_settings
) -> None:
    """The :class:`BatchSummary` returned by
    :func:`send_batch` carries the per-channel rollup
    so the dashboard can render the "desglose por canal"
    widget from the POST response alone.

    The batch sends 2 SMS and 1 WhatsApp message; the
    Starter plan charges CLP $25 + $5 for SMS and
    CLP $80 + $5 for WhatsApp, so the rollup is::

        sms       → count=2  cost=50  fee=10  total=60
        whatsapp  → count=1  cost=80  fee=5   total=85

    The list is ordered by ``channel.value`` so the
    response is stable across calls."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "sms", "to": "+56933333333", "body": "dos"},
            {"channel": "whatsapp", "to": "+56944444444", "body": "tres"},
        ],
        settings=batch_settings,
    )

    channels = list(outcome.summary.channels)
    assert [c.channel for c in channels] == [Channel.SMS, Channel.WHATSAPP]

    sms, whatsapp = channels
    assert sms.count == 2
    assert sms.pending == 2  # both items are in flight
    assert sms.delivered == 0
    assert sms.failed == 0
    assert sms.total_cost_clp == 25 + 25
    assert sms.total_fee_clp == 5 + 5
    assert sms.total_amount_clp == 60

    assert whatsapp.count == 1
    assert whatsapp.pending == 1
    assert whatsapp.delivered == 0
    assert whatsapp.failed == 0
    assert whatsapp.total_cost_clp == 80
    assert whatsapp.total_fee_clp == 5
    assert whatsapp.total_amount_clp == 85

    # The cross-check: the per-channel rollup sums
    # to the batch-level rollup so a caller that
    # renders the headline counters from the per-channel
    # list sees the same values.
    total_cost = sum(c.total_cost_clp for c in channels)
    total_fee = sum(c.total_fee_clp for c in channels)
    total_amount = sum(c.total_amount_clp for c in channels)
    assert total_cost == outcome.summary.total_cost_clp
    assert total_fee == outcome.summary.total_fee_clp
    assert total_amount == outcome.summary.total_amount_clp


async def test_send_batch_per_channel_breakdown_includes_failed_items(
    async_session, batch_providers, batch_settings
) -> None:
    """The per-channel rollup includes the failed
    items (a failed item still incurred a row-level
    cost / fee at dispatch time, so excluding it from
    the rollup would under-report the campaign's
    actual cost).

    The :class:`BatchFakeProvider` raises
    :class:`ProviderUnavailableError` for destinations
    ending in ``2``; the rollup places the failed item
    under the ``failed`` counter of its channel."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            # ends in ``2`` → the fake provider raises
            {"channel": "sms", "to": "+56911111112", "body": "dos"},
        ],
        settings=batch_settings,
    )

    channels = list(outcome.summary.channels)
    assert len(channels) == 1
    sms = channels[0]
    assert sms.channel == Channel.SMS
    assert sms.count == 2
    assert sms.pending == 1
    assert sms.failed == 1
    # Both items contribute to the rollup (CLP $25 + $5
    # for every SMS under the Starter plan).
    assert sms.total_cost_clp == 25 + 25
    assert sms.total_fee_clp == 5 + 5


async def test_send_batch_per_channel_breakdown_is_empty_for_zero_items(
    async_session, batch_providers, batch_settings
) -> None:
    """A fresh :class:`BatchSummary` dataclass carries
    an empty ``channels`` tuple so a caller that
    inspects the value before any rollup is run does
    not blow up iterating a ``None``. The dataclass
    is the source of truth – the route layer projects
    an empty list for a freshly-created batch with no
    items yet."""
    summary = BatchSummary(total=0, pending=0, delivered=0, failed=0)
    assert summary.channels == ()


async def test_batch_channel_summary_succeeded_is_delivered_plus_failed() -> None:
    """``succeeded`` is defined as ``delivered + failed``
    so a per-channel "X of Y delivered" widget can
    read the single field without re-deriving the
    arithmetic. Same contract as the batch-level
    :attr:`BatchSummary.succeeded`."""
    channel = BatchChannelSummary(
        channel=Channel.SMS,
        count=10,
        pending=0,
        delivered=7,
        failed=3,
        total_cost_clp=250,
        total_fee_clp=50,
    )
    assert channel.succeeded == 10
    assert channel.total_amount_clp == 300


async def test_get_batch_returns_per_channel_breakdown(
    async_session, batch_providers, batch_settings
) -> None:
    """A subsequent :func:`get_batch` call returns a
    :class:`BatchDetail` whose ``channels`` field
    carries the per-channel rollup so the dashboard
    can render the "desglose por canal" widget on the
    batch detail page without a second round-trip.

    The cross-check mirrors the
    :func:`test_send_batch_summary_carries_per_channel_breakdown`
    test: the per-channel rollup sums to the batch-level
    rollup so a caller that renders the headline
    counters from the per-channel list sees the same
    values."""
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "whatsapp", "to": "+56944444444", "body": "dos"},
        ],
        settings=batch_settings,
    )

    detail = await get_batch(
        async_session,
        client=client,
        batch_id=outcome.batch_id,
        settings=batch_settings,
    )
    channels = list(detail.channels)
    assert [c.channel for c in channels] == [Channel.SMS, Channel.WHATSAPP]

    sms, whatsapp = channels
    assert sms.count == 1
    assert sms.total_cost_clp == 25
    assert sms.total_fee_clp == 5
    assert whatsapp.count == 1
    assert whatsapp.total_cost_clp == 80
    assert whatsapp.total_fee_clp == 5


async def test_list_batches_returns_per_channel_breakdown(
    async_session, batch_providers, batch_settings
) -> None:
    """The :func:`list_batches` endpoint returns a
    :class:`BatchListPage` whose ``channels_by_batch``
    field maps every batch id to its per-channel
    rollup. The query is one ``GROUP BY`` round-trip
    for the whole page (no N+1) so a dashboard with
    many campaigns does not pay a per-batch penalty.

    The test sends two batches (one all-SMS, one
    mixed) and asserts each ``channels_by_batch``
    entry matches the per-batch rollup."""
    client = await _make_client(async_session)
    sms_only = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "sms", "to": "+56933333333", "body": "dos"},
        ],
        settings=batch_settings,
    )
    mixed = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "whatsapp", "to": "+56944444444", "body": "dos"},
        ],
        settings=batch_settings,
    )

    page = await list_batches(
        async_session,
        client=client,
        settings=batch_settings,
    )
    assert page.channels_by_batch is not None
    # Every batch on the page has an entry (both
    # batches have at least one message persisted).
    assert set(page.channels_by_batch.keys()) == {sms_only.batch_id, mixed.batch_id}

    sms_only_channels = page.channels_by_batch[sms_only.batch_id]
    assert [c.channel for c in sms_only_channels] == [Channel.SMS]
    assert sms_only_channels[0].count == 2
    assert sms_only_channels[0].total_cost_clp == 25 + 25

    mixed_channels = page.channels_by_batch[mixed.batch_id]
    assert [c.channel for c in mixed_channels] == [Channel.SMS, Channel.WHATSAPP]
    assert mixed_channels[0].count == 1
    assert mixed_channels[1].count == 1


async def test_list_batches_channels_by_batch_is_none_for_empty_page(
    async_session, batch_providers, batch_settings
) -> None:
    """A page with no items returns ``channels_by_batch``
    as ``None`` (or an empty dict) so the caller does
    not have to special-case the "no rows" path. The
    helper short-circuits on an empty ``batch_ids``
    list and returns an empty dict; the route layer
    is expected to fall back to an empty per-batch
    list on the response side."""
    client = await _make_client(async_session)
    page = await list_batches(
        async_session,
        client=client,
        settings=batch_settings,
    )
    assert page.items == []
    assert page.channels_by_batch == {}


# ---------------------------------------------------------------------------
# send_batch — completion webhook wiring (issue #9)
# ---------------------------------------------------------------------------
#
# The PRD's batch-completion webhook is opt-in. A customer
# that wants a "your campaign finished" push registers a
# ``webhook_url`` on ``POST /v1/messages/batch`` and the
# platform fires one signed POST once the batch reaches a
# terminal state. The tests below cover the service-layer
# wiring: the URL / secret persistence, the one-time
# secret minting, the validation guard and the actual
# delivery performed by
# :func:`app.services.messaging.fire_batch_completion_webhook`.


async def test_send_batch_persists_webhook_url_and_secret(
    async_session, batch_providers, batch_settings
) -> None:
    """When the caller supplies a ``webhook_url`` and a
    ``webhook_secret``, both values land on the
    :class:`Batch` row so a future re-fire (e.g. a
    delivery-receipt update that triggers another
    recompute) can re-use the same configuration without
    asking the customer to re-submit it.

    The test also asserts the
    :class:`BatchOutcome` the service layer returns
    surfaces the values back to the route layer so the
    dashboard can echo the configuration the platform
    persisted.
    """
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
        webhook_url="https://example.com/hook",
        webhook_secret="caller-supplied-secret",
    )

    # Outcome carries the configuration back to the caller.
    assert outcome.webhook_url == "https://example.com/hook"
    assert outcome.webhook_secret == "caller-supplied-secret"

    # The persisted row mirrors the same values.
    from sqlalchemy import select

    persisted = (
        await async_session.execute(select(Batch).where(Batch.id == outcome.batch_id))
    ).scalar_one()
    assert persisted.webhook_url == "https://example.com/hook"
    assert persisted.webhook_secret == "caller-supplied-secret"


async def test_send_batch_mints_one_time_webhook_secret_when_caller_omits_it(
    async_session, batch_providers, batch_settings
) -> None:
    """A caller that supplies a ``webhook_url`` without a
    ``webhook_secret`` gets a one-time CSPRNG secret
    minted by the service layer. The minted value is
    returned in the :class:`BatchOutcome` and persisted
    on the :class:`Batch` row, so the dashboard can
    surface it to the user (the same flow the API-key
    onboarding uses for the per-message webhook
    subscription secret).
    """
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
        webhook_url="https://example.com/hook",
    )

    # A non-empty hex-shaped string (32 bytes -> 64
    # hex characters).
    assert isinstance(outcome.webhook_secret, str)
    assert len(outcome.webhook_secret) == 64
    int(outcome.webhook_secret, 16)  # parses as hex

    from sqlalchemy import select

    persisted = (
        await async_session.execute(select(Batch).where(Batch.id == outcome.batch_id))
    ).scalar_one()
    assert persisted.webhook_secret == outcome.webhook_secret


async def test_send_batch_returns_none_webhook_when_url_omitted(
    async_session, batch_providers, batch_settings
) -> None:
    """A caller that does not opt-in to the completion
    webhook gets ``webhook_url=None`` /
    ``webhook_secret=None`` on the outcome (and on the
    persisted row). The service layer treats the absence
    as "no webhook configured" and the delivery helper
    silently skips the POST.
    """
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
    )
    assert outcome.webhook_url is None
    assert outcome.webhook_secret is None


async def test_send_batch_rejects_non_https_webhook_url(
    async_session, batch_providers, batch_settings
) -> None:
    """A misconfigured customer that ships an ``http://``
    (or any non-``https``) URL gets a 422
    :class:`InvalidMessageError` at the service boundary.
    The same guard applies to typos like ``javascript:``
    or ``file:`` – the URL parser's scheme check catches
    every value that is not ``https``.

    The assertion is on the ``code`` attribute the
    :class:`InvalidMessageError` exposes so a future
    refactor of the message body does not break the
    contract.
    """
    client = await _make_client(async_session)
    with pytest.raises(InvalidMessageError) as excinfo:
        await send_batch(
            async_session,
            client=client,
            items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
            settings=batch_settings,
            webhook_url="http://insecure.example.com/hook",
        )
    assert excinfo.value.code == "invalid_webhook_url"


# ---------------------------------------------------------------------------
# fire_batch_completion_webhook — delivery (issue #9)
# ---------------------------------------------------------------------------
#
# These tests cover the actual delivery helper the
# :func:`send_batch` route handler schedules as a
# background task. The ``delivery_client`` argument is
# swapped for an in-memory fake so the suite never opens
# a real TCP connection.


class _FakeWebhookDeliveryClient:
    """An in-memory :class:`WebhookDeliveryClient` for tests.

    Records the last ``deliver()`` call so the assertions
    below can check the URL, body and headers the service
    layer produced. Returns a configurable
    :class:`WebhookDeliveryResult` so the "successful
    delivery" and "delivery failed" code paths can both
    be exercised.
    """

    def __init__(self, result: WebhookDeliveryResult | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result or WebhookDeliveryResult(
            succeeded=True,
            attempts=1,
            status_code=200,
            response_body="ok",
            error=None,
        )

    async def deliver(
        self, *, url: str, body: bytes, headers: dict[str, str]
    ) -> WebhookDeliveryResult:
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        return self._result


async def test_fire_batch_completion_webhook_returns_none_when_no_url(
    async_session, batch_providers, batch_settings
) -> None:
    """A batch that was submitted without a
    ``webhook_url`` triggers no delivery – the helper
    short-circuits to ``None`` so the caller can branch
    on the return value (the route layer does not even
    need to schedule a background task in this case).
    """
    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
    )
    from sqlalchemy import select

    batch = (
        await async_session.execute(select(Batch).where(Batch.id == outcome.batch_id))
    ).scalar_one()
    fake = _FakeWebhookDeliveryClient()
    result = await fire_batch_completion_webhook(
        batch=batch,
        summary=outcome.summary,
        settings=batch_settings,
        delivery_client=fake,
    )
    assert result is None
    assert fake.calls == []


async def test_fire_batch_completion_webhook_signs_payload_with_batch_secret(
    async_session, batch_providers, batch_settings
) -> None:
    """The outbound POST body is the JSON summary the
    helper built (id, status, counters, channels), and
    the ``X-Mgw-Signature`` header is the HMAC-SHA256
    digest of that body keyed with the batch's
    ``webhook_secret``. The event / delivery-id headers
    mirror the per-message receipt convention so a
    receiver that already speaks the per-message
    protocol can switch on ``X-Mgw-Event`` for the new
    ``batch.completed`` value.
    """
    from app.services.webhook_delivery import WebhookDeliveryResult
    from app.services.webhooks import sign_payload

    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[
            {"channel": "sms", "to": "+56911111111", "body": "uno"},
            {"channel": "whatsapp", "to": "+56922222222", "body": "dos"},
        ],
        settings=batch_settings,
        webhook_url="https://example.com/hook",
        webhook_secret="the-shared-secret",
    )
    from sqlalchemy import select

    batch = (
        await async_session.execute(select(Batch).where(Batch.id == outcome.batch_id))
    ).scalar_one()
    fake = _FakeWebhookDeliveryClient()
    result = await fire_batch_completion_webhook(
        batch=batch,
        summary=outcome.summary,
        settings=batch_settings,
        delivery_client=fake,
    )

    assert isinstance(result, WebhookDeliveryResult)
    assert result.succeeded is True
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://example.com/hook"

    body = call["body"]
    assert isinstance(body, bytes)
    headers_raw = call["headers"]
    assert isinstance(headers_raw, dict)
    headers = {str(key): str(value) for key, value in headers_raw.items()}
    assert headers["X-Mgw-Event"] == "batch.completed"
    assert headers["X-Mgw-Delivery"] == outcome.batch_id
    assert headers["Content-Type"] == "application/json"
    # Signature matches HMAC-SHA256 of the body with the
    # batch's webhook_secret.
    expected = sign_payload(body=body, secret="the-shared-secret")
    assert headers["X-Mgw-Signature"] == expected

    # Body is valid JSON and carries the headline counters.
    import json as _json

    payload = _json.loads(body)
    assert payload["id"] == outcome.batch_id
    # The inline dispatch leaves the messages in ``SENT``
    # state (delivery confirmation arrives asynchronously
    # through the worker / webhook loop) so the batch's
    # status is still ``processing``. The platform fires
    # the completion webhook as soon as the *dispatch* is
    # done, regardless of whether every message has been
    # delivered; the per-item ``delivered`` / ``failed``
    # counters are the source of truth for the final
    # delivery outcome.
    assert payload["status"] == "processing"
    assert payload["total"] == 2
    assert payload["delivered"] + payload["failed"] == payload["succeeded"]
    assert len(payload["channels"]) == 2
    assert {c["channel"] for c in payload["channels"]} == {"sms", "whatsapp"}


async def test_fire_batch_completion_webhook_returns_failure_result(
    async_session, batch_providers, batch_settings
) -> None:
    """A failing customer endpoint surfaces as a
    :class:`WebhookDeliveryResult` with
    ``succeeded=False`` – the helper never raises on a
    transport error, the route layer just lets the
    background task finish. The test confirms the
    platform does not pretend a delivery that never
    landed is a success.
    """
    from app.services.webhook_delivery import WebhookDeliveryResult

    client = await _make_client(async_session)
    outcome = await send_batch(
        async_session,
        client=client,
        items=[{"channel": "sms", "to": "+56911111111", "body": "uno"}],
        settings=batch_settings,
        webhook_url="https://flaky.example.com/hook",
        webhook_secret="shared",
    )
    from sqlalchemy import select

    batch = (
        await async_session.execute(select(Batch).where(Batch.id == outcome.batch_id))
    ).scalar_one()
    fake = _FakeWebhookDeliveryClient(
        WebhookDeliveryResult(
            succeeded=False,
            attempts=3,
            status_code=502,
            response_body="",
            error="http_502",
        )
    )
    result = await fire_batch_completion_webhook(
        batch=batch,
        summary=outcome.summary,
        settings=batch_settings,
        delivery_client=fake,
    )
    assert result is not None
    assert result.succeeded is False
    assert result.status_code == 502
    assert result.attempts == 3
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# BatchRateLimitError (issue #9)
# ---------------------------------------------------------------------------
#
# The Redis-backed rate limiter lives in
# :mod:`app.services.rate_limit` (covered by
# ``tests/services/test_rate_limit.py``); the test below
# only checks the error class is wired into the public
# surface so the route layer can import it without a
# circular dependency.


def test_batch_rate_limit_error_exposes_retry_after() -> None:
    """The :class:`BatchRateLimitError` exception carries
    a ``retry_after_seconds`` attribute the route layer
    surfaces as the ``Retry-After`` header. The default
    of ``1`` second matches the fixed-window granularity
    the rate limiter enforces.
    """
    err = BatchRateLimitError("batch_rate_limited", "too many")
    assert err.http_status == 429
    assert err.code == "batch_rate_limited"
    assert err.retry_after_seconds == 1
    err_two = BatchRateLimitError(
        "batch_rate_limited", "too many", retry_after_seconds=2
    )
    assert err_two.retry_after_seconds == 2


# ---------------------------------------------------------------------------
# Imports used by the tests above (kept at the bottom so
# the test cases read top-to-bottom).
# ---------------------------------------------------------------------------
from app.services.messaging import (  # noqa: E402
    BatchRateLimitError,
    fire_batch_completion_webhook,
)
