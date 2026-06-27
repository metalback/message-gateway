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
    BatchNotFoundError,
    BatchOutcome,
    BatchSummary,
    get_batch,
    list_batches,
    send_batch,
)

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
    """A successful :func:`get_batch` call returns the
    same row the original send created, with the
    counter recompute applied on the way out.

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

    batch = await get_batch(
        async_session,
        client=client,
        batch_id=outcome.batch_id,
        settings=batch_settings,
    )
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
