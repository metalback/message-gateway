"""Unit tests for :mod:`app.services.provider_health` (issue #11).

The service owns three concerns:

- :func:`record_routing_attempt` and
  :func:`build_attempt_recorder` – the per-attempt
  ``routing_log`` writer the messaging service uses.
- :func:`probe_provider` and :func:`run_health_checks`
  – the periodic health probe the issue calls for.
- :func:`list_provider_health` and
  :func:`list_recent_routing_attempts` – the read APIs
  the admin endpoint consumes.

The tests stub the registry (so a real
:class:`~app.adapters.meta_whatsapp.MetaWhatsAppProvider`
never makes an HTTP call) and exercise the service
against an in-memory SQLite session.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import app.adapters.registry as registry
from app.adapters.base import BaseProvider, SendResult
from app.adapters.errors import (
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderValidationError,
)
from app.adapters.failover import FailoverProvider
from app.config import Settings
from app.models.message import Channel
from app.models.provider_config import ProviderConfig, ProviderHealth
from app.models.routing_log import RoutingLog, RoutingLogOutcome
from app.services import provider_health

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubProvider(BaseProvider):
    """Provider that records calls and returns a scripted sequence.

    Mirrors the pattern the existing
    :mod:`tests.adapters.test_failover` doubles use so the
    health-check tests can drive every branch (success /
    rate limit / unavailable / validation / unexpected
    exception) declaratively. The double consumes
    responses on **both** ``send`` and ``get_status``
    (FIFO, shared queue) so a test that exercises
    :class:`~app.adapters.failover.FailoverProvider` can
    script the chain end-to-end.
    """

    def __init__(
        self,
        name: str,
        *,
        responses: list[Exception | str] | None = None,
    ) -> None:
        self.name = name
        self._responses: list[Exception | str] = list(responses or [])
        self.status_calls: list[str] = []
        self.send_calls: list[dict[str, Any]] = []

    def push(self, response: Exception | str) -> None:
        """Schedule ``response`` for the next ``send`` /
        ``get_status`` call.

        Pass a :class:`str` for a successful response or
        an :class:`Exception` to raise it.
        """
        self._responses.append(response)

    async def send(self, *, to: str, body: str, **kwargs: object) -> SendResult:
        self.send_calls.append({"to": to, "body": body, **kwargs})
        if not self._responses:
            return SendResult(provider_msg_id=f"{self.name}-id", raw={})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SendResult(provider_msg_id=response, raw={})

    async def get_status(self, provider_msg_id: str) -> str:
        self.status_calls.append(provider_msg_id)
        if not self._responses:
            return "sent"
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def stub_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, _StubProvider]:
    """Patch the registry to return scripted :class:`_StubProvider` instances.

    The fixture registers two providers per channel so a
    test can exercise the primary / fallback split. A
    cleanup hook restores the original registry at the
    end of the test so a future case is not affected by
    the monkey-patch.
    """
    providers: dict[str, _StubProvider] = {
        "meta_whatsapp": _StubProvider("meta_whatsapp"),
        "twilio_whatsapp": _StubProvider("twilio_whatsapp"),
        "sms_aggregator": _StubProvider("sms_aggregator"),
        "twilio_sms": _StubProvider("twilio_sms"),
    }

    def _factory(name: str):
        def _build(_settings: Settings) -> BaseProvider:
            return providers[name]

        return _build

    monkeypatch.setitem(registry._BUILDERS, Channel.WHATSAPP, _factory("meta_whatsapp"))
    monkeypatch.setitem(registry._BUILDERS, Channel.SMS, _factory("sms_aggregator"))
    for name in providers:
        registry.register_failover_provider(name, _factory(name))
    return providers


def _settings(chains: dict[str, list[str]] | None = None) -> Settings:
    """Build a minimal :class:`Settings` with the chains the test needs.

    A test that does not care about chains can call
    ``_settings()`` to get a default instance.
    """
    return Settings(
        meta_whatsapp_access_token="t",
        meta_whatsapp_phone_number_id="p",
        sms_aggregator_api_url="https://sms.test",
        sms_aggregator_api_key="k",
        sms_aggregator_sender_id="MSGGTWY",
        provider_failover_chains=chains or {},
    )


# ---------------------------------------------------------------------------
# record_routing_attempt
# ---------------------------------------------------------------------------


async def test_record_routing_attempt_persists_a_row(async_session) -> None:
    """The helper writes a single :class:`RoutingLog` row
    with the expected column values. The function is
    used by the periodic health worker (which has no
    associated message), so ``message_id`` defaults to
    ``None`` and the row is committed in isolation."""
    row = await provider_health.record_routing_attempt(
        async_session,
        provider_name="meta_whatsapp",
        channel=Channel.WHATSAPP,
        outcome=RoutingLogOutcome.SUCCESS,
        latency_ms=42,
    )
    await async_session.commit()
    assert row.id is not None
    assert row.message_id is None
    assert row.provider_attempted == "meta_whatsapp"
    assert row.channel == Channel.WHATSAPP
    assert row.outcome == RoutingLogOutcome.SUCCESS
    assert row.latency_ms == 42
    assert row.error_code is None
    assert row.error_message is None


async def test_record_routing_attempt_truncates_long_error_message(
    async_session,
) -> None:
    """A verbose upstream response must not blow up the
    column: the helper truncates ``error_message`` to
    500 chars (the column ceiling). The test pins the
    contract that a misbehaving upstream cannot corrupt
    the audit log."""
    long_message = "x" * 1000
    row = await provider_health.record_routing_attempt(
        async_session,
        provider_name="meta_whatsapp",
        channel=Channel.WHATSAPP,
        outcome=RoutingLogOutcome.FAILURE,
        latency_ms=10,
        error_code="provider_unavailable",
        error_message=long_message,
    )
    await async_session.commit()
    assert row.error_message is not None
    assert len(row.error_message) == 500


async def test_record_routing_attempt_clamps_negative_latency(
    async_session,
) -> None:
    """A negative latency (e.g. a clock skew between
    the recorder and the upstream) is a programming
    error. The helper clamps to ``0`` so the column
    contract is honoured even when the input is
    malformed."""
    row = await provider_health.record_routing_attempt(
        async_session,
        provider_name="meta_whatsapp",
        channel=Channel.WHATSAPP,
        outcome=RoutingLogOutcome.SUCCESS,
        latency_ms=-5,
    )
    await async_session.commit()
    assert row.latency_ms == 0


# ---------------------------------------------------------------------------
# build_attempt_recorder
# ---------------------------------------------------------------------------


async def test_build_attempt_recorder_stages_row_on_session(
    async_session,
) -> None:
    """The recorder built by
    :func:`build_attempt_recorder` stages a
    :class:`RoutingLog` row on the same session the
    caller hands in. The row is not flushed until the
    caller's ``commit`` boundary, so the audit row and
    the parent ``Message`` row commit atomically.

    The test mirrors the production code path: a
    FailoverProvider is constructed with the recorder,
    a failing primary + a successful fallback is
    invoked, and the test asserts the session has *two*
    staged rows that land in the same commit.
    """
    primary = _StubProvider(
        "meta_whatsapp",
        responses=[ProviderUnavailableError("meta 5xx", provider="meta_whatsapp")],
    )
    fallback = _StubProvider("twilio_whatsapp", responses=["sent"])
    chain = FailoverProvider([primary, fallback])

    recorder = provider_health.build_attempt_recorder(
        async_session,
        channel=Channel.WHATSAPP,
        message_id="00000000-0000-0000-0000-000000000001",
    )
    # Sanity: the recorder is a callable, not a coroutine.
    assert callable(recorder)

    result = await chain.send(
        to="+56912345678",
        body="hola",
        attempt_callback=recorder,
    )
    assert result.provider_name == "twilio_whatsapp"

    # The session is dirty (two rows staged) but not
    # yet committed. A ``commit`` flushes them.
    assert async_session.in_transaction()
    await async_session.commit()

    rows = (
        await async_session.execute(
            __import__("sqlalchemy").select(RoutingLog)
        )
    ).scalars().all()
    assert len(rows) == 2
    providers = {r.provider_attempted for r in rows}
    outcomes = {(r.provider_attempted, str(r.outcome)) for r in rows}
    assert providers == {"meta_whatsapp", "twilio_whatsapp"}
    assert ("meta_whatsapp", "failure") in outcomes
    assert ("twilio_whatsapp", "success") in outcomes


async def test_build_attempt_recorder_truncates_error_message(
    async_session,
) -> None:
    """A verbose upstream error message (a Meta 503 with
    a multi-paragraph body, for example) must not blow
    up the column. The recorder truncates to the same
    500-char ceiling the helper uses, so the audit log
    cannot be poisoned by an upstream that returns
    unbounded text.
    """
    recorder = provider_health.build_attempt_recorder(
        async_session,
        channel=Channel.WHATSAPP,
        message_id="00000000-0000-0000-0000-000000000002",
    )
    recorder(
        "meta_whatsapp",
        RoutingLogOutcome.FAILURE,
        10,
        "provider_unavailable",
        "x" * 1000,
    )
    await async_session.commit()

    rows = (
        await async_session.execute(
            __import__("sqlalchemy").select(RoutingLog)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].error_message is not None
    assert len(rows[0].error_message) == 500


# ---------------------------------------------------------------------------
# probe_provider
# ---------------------------------------------------------------------------


async def test_probe_provider_records_success(
    async_session,
    stub_registry: dict[str, _StubProvider],
) -> None:
    """A successful probe writes a row, increments
    ``consecutive_successes``, marks the provider as
    :attr:`ProviderHealth.HEALTHY` and stores the
    latency in ``last_latency_ms``. The first probe of
    a brand-new provider also creates the
    :class:`ProviderConfig` row – the platform
    discovers providers lazily through the registry."""
    row, status, latency_ms = await provider_health.probe_provider(
        async_session,
        provider_name="meta_whatsapp",
        channel=Channel.WHATSAPP,
        settings=_settings(),
    )
    await async_session.commit()
    assert status == ProviderHealth.HEALTHY
    assert latency_ms >= 0
    assert row.health_status == ProviderHealth.HEALTHY
    assert row.consecutive_successes == 0  # reset on transition
    assert row.consecutive_failures == 0
    assert row.last_health_check is not None
    assert row.last_latency_ms == latency_ms


async def test_probe_provider_marks_degraded_after_threshold_failures(
    async_session,
    stub_registry: dict[str, _StubProvider],
) -> None:
    """Three consecutive failures (the
    :data:`HEALTH_FAILURE_THRESHOLD`) flip the row to
    :attr:`ProviderHealth.DEGRADED`. The first failure
    only increments the counter; the second keeps it
    counting; the third trips the threshold."""
    for _ in range(3):
        stub_registry["meta_whatsapp"].push(
            ProviderUnavailableError("meta 5xx", provider="meta_whatsapp")
        )
    for _ in range(3):
        await provider_health.probe_provider(
            async_session,
            provider_name="meta_whatsapp",
            channel=Channel.WHATSAPP,
            settings=_settings(),
        )
    await async_session.commit()
    # Re-read the row from the database to verify the
    # transition landed.
    from sqlalchemy import select

    stored = (
        await async_session.execute(
            select(ProviderConfig).where(ProviderConfig.name == "meta_whatsapp")
        )
    ).scalar_one()
    assert stored.health_status == ProviderHealth.DEGRADED
    assert stored.consecutive_failures == 3
    assert stored.consecutive_successes == 0


async def test_probe_provider_recovers_on_success(
    async_session,
    stub_registry: dict[str, _StubProvider],
) -> None:
    """A degraded provider that answers the probe
    successfully twice in a row is flipped back to
    :attr:`ProviderHealth.HEALTHY` and the failure
    counter is reset. The test pins the recovery
    half of the state machine."""
    for _ in range(3):
        stub_registry["meta_whatsapp"].push(
            ProviderUnavailableError("meta 5xx", provider="meta_whatsapp")
        )
    for _ in range(3):
        await provider_health.probe_provider(
            async_session,
            provider_name="meta_whatsapp",
            channel=Channel.WHATSAPP,
            settings=_settings(),
        )
    # Now the provider starts succeeding.
    for _ in range(provider_health.HEALTH_RECOVERY_THRESHOLD):
        await provider_health.probe_provider(
            async_session,
            provider_name="meta_whatsapp",
            channel=Channel.WHATSAPP,
            settings=_settings(),
        )
    await async_session.commit()

    from sqlalchemy import select

    stored = (
        await async_session.execute(
            select(ProviderConfig).where(ProviderConfig.name == "meta_whatsapp")
        )
    ).scalar_one()
    assert stored.health_status == ProviderHealth.HEALTHY
    assert stored.consecutive_failures == 0
    assert stored.consecutive_successes == 0  # reset on transition


async def test_probe_provider_treats_rate_limit_as_failure(
    async_session,
    stub_registry: dict[str, _StubProvider],
) -> None:
    """A 429 is a retryable error from the perspective
    of the failover router, but the health worker
    treats it the same as an unavailable: the upstream
    is not healthy. The test pins the
    "retryable-as-failure" decision so a future
    iteration that wants to special-case rate limits
    (e.g. mark them as "throttled" instead of
    "degraded") has a single, named test to amend."""
    stub_registry["meta_whatsapp"].push(
        ProviderRateLimitError("meta 429", provider="meta_whatsapp")
    )
    row, status, _latency = await provider_health.probe_provider(
        async_session,
        provider_name="meta_whatsapp",
        channel=Channel.WHATSAPP,
        settings=_settings(),
    )
    assert status == ProviderHealth.UNKNOWN  # first failure, threshold not met
    assert row.consecutive_failures == 1
    assert row.consecutive_successes == 0


async def test_probe_provider_treats_validation_error_as_failure(
    async_session,
    stub_registry: dict[str, _StubProvider],
) -> None:
    """A :class:`ProviderValidationError` on a status
    query is unexpected (the synthetic id is well-
    formed) but the worker still records a failure so
    the dashboard surfaces the misconfiguration. The
    test pins the "validation-on-probe == failure"
    contract."""
    stub_registry["meta_whatsapp"].push(
        ProviderValidationError("bad id", provider="meta_whatsapp")
    )
    row, _status, _latency = await provider_health.probe_provider(
        async_session,
        provider_name="meta_whatsapp",
        channel=Channel.WHATSAPP,
        settings=_settings(),
    )
    assert row.consecutive_failures == 1


async def test_probe_provider_swallows_unexpected_exception(
    async_session,
    stub_registry: dict[str, _StubProvider],
) -> None:
    """An unexpected exception (a programming error in
    a concrete adapter) must not crash the health
    worker. The test injects a bare ``Exception`` and
    asserts the probe completes without raising."""
    stub_registry["meta_whatsapp"].push(RuntimeError("boom"))
    row, _status, _latency = await provider_health.probe_provider(
        async_session,
        provider_name="meta_whatsapp",
        channel=Channel.WHATSAPP,
        settings=_settings(),
    )
    # The probe still updates the row; the failure
    # counter is incremented so the dashboard sees the
    # misconfiguration.
    assert row.consecutive_failures == 1


# ---------------------------------------------------------------------------
# run_health_checks
# ---------------------------------------------------------------------------


async def test_run_health_checks_probes_every_named_provider(
    async_session,
    stub_registry: dict[str, _StubProvider],
) -> None:
    """When ``providers`` is passed explicitly, the
    function probes each ``(name, channel)`` pair in
    the list. The test asserts every named provider
    sees a ``get_status`` call, regardless of the
    chain configuration."""
    targets = [
        ("meta_whatsapp", Channel.WHATSAPP),
        ("twilio_whatsapp", Channel.WHATSAPP),
    ]
    await provider_health.run_health_checks(
        async_session,
        settings=_settings(),
        providers=targets,
    )
    assert stub_registry["meta_whatsapp"].status_calls == [
        provider_health.HEALTHCHECK_PROVIDER_MSG_ID
    ]
    assert stub_registry["twilio_whatsapp"].status_calls == [
        provider_health.HEALTHCHECK_PROVIDER_MSG_ID
    ]


async def test_run_health_checks_derives_targets_from_chain(
    async_session,
    stub_registry: dict[str, _StubProvider],
) -> None:
    """Without an explicit ``providers`` argument the
    function walks the chain map from
    :class:`Settings` and probes every name it finds.
    The test pins the contract that the periodic
    worker does not need to be told which providers
    exist – the registry + chain map is the source of
    truth."""
    settings = _settings(
        chains={"whatsapp": ["meta_whatsapp", "twilio_whatsapp"]},
    )
    await provider_health.run_health_checks(
        async_session,
        settings=settings,
    )
    # Both providers saw the probe.
    assert stub_registry["meta_whatsapp"].status_calls
    assert stub_registry["twilio_whatsapp"].status_calls


# ---------------------------------------------------------------------------
# list_provider_health
# ---------------------------------------------------------------------------


async def test_list_provider_health_returns_all_rows(
    async_session,
    stub_registry: dict[str, _StubProvider],
) -> None:
    """The read API returns a :class:`ProviderHealthRow`
    per persisted :class:`ProviderConfig` row, sorted
    by ``(channel, priority, name)`` so the dashboard
    renders providers in the same order the routing
    layer will use them.
    """
    for name, channel in [
        ("meta_whatsapp", Channel.WHATSAPP),
        ("twilio_whatsapp", Channel.WHATSAPP),
        ("sms_aggregator", Channel.SMS),
    ]:
        await provider_health.probe_provider(
            async_session,
            provider_name=name,
            channel=channel,
            settings=_settings(),
        )
    await async_session.commit()

    rows = await provider_health.list_provider_health(async_session)
    names = [r.name for r in rows]
    # Sorted by (channel, priority, name). "sms"
    # alphabetically precedes "whatsapp" so the SMS
    # provider is at index 0, followed by the two
    # WhatsApp providers in alphabetic order.
    assert names == [
        "sms_aggregator",
        "meta_whatsapp",
        "twilio_whatsapp",
    ]
    for row in rows:
        assert row.health_status == ProviderHealth.HEALTHY.value
        assert row.last_health_check is not None


async def test_list_provider_health_filters_by_channel(
    async_session,
    stub_registry: dict[str, _StubProvider],
) -> None:
    """The ``channel`` filter lets the dashboard render
    a per-channel health card without filtering on the
    client side. The test pins the contract that the
    filter is applied at the database level (no rows
    of the wrong channel leak through)."""
    for name, channel in [
        ("meta_whatsapp", Channel.WHATSAPP),
        ("sms_aggregator", Channel.SMS),
    ]:
        await provider_health.probe_provider(
            async_session,
            provider_name=name,
            channel=channel,
            settings=_settings(),
        )
    await async_session.commit()

    rows = await provider_health.list_provider_health(
        async_session, channel=Channel.WHATSAPP
    )
    assert all(r.channel == "whatsapp" for r in rows)
    assert {r.name for r in rows} == {"meta_whatsapp"}


# ---------------------------------------------------------------------------
# list_recent_routing_attempts
# ---------------------------------------------------------------------------


async def test_list_recent_routing_attempts_returns_rows_newest_first(
    async_session,
) -> None:
    """The read API returns the most recent
    :class:`RoutingLog` rows first (``attempted_at``
    descending). The test inserts three rows with
    explicit ``attempted_at`` values so the ordering
    is deterministic regardless of the database's
    clock resolution.
    """
    base = datetime.now(tz=UTC)

    for offset in range(3):
        row = RoutingLog(
            provider_attempted="meta_whatsapp",
            channel=Channel.WHATSAPP,
            outcome=RoutingLogOutcome.SUCCESS,
            latency_ms=10 + offset,
            attempted_at=base + timedelta(seconds=offset),
        )
        async_session.add(row)
    await async_session.commit()

    items, total = await provider_health.list_recent_routing_attempts(
        async_session,
        limit=10,
        offset=0,
    )
    assert total == 3
    # Newest first: the row with the highest
    # ``attempted_at`` is at index 0.
    assert [r.latency_ms for r in items] == [12, 11, 10]


async def test_list_recent_routing_attempts_filters_by_message_id(
    async_session,
) -> None:
    """The optional ``message_id`` filter is the
    per-message trace view: only the attempts the
    chain made for a single message come back. The
    test pins the contract that the dashboard's
    "trace this message" drawer can be served from
    the same endpoint that powers the global log
    view."""
    await provider_health.record_routing_attempt(
        async_session,
        provider_name="meta_whatsapp",
        channel=Channel.WHATSAPP,
        outcome=RoutingLogOutcome.SUCCESS,
        latency_ms=5,
        message_id="00000000-0000-0000-0000-000000000001",
    )
    await provider_health.record_routing_attempt(
        async_session,
        provider_name="twilio_whatsapp",
        channel=Channel.WHATSAPP,
        outcome=RoutingLogOutcome.SUCCESS,
        latency_ms=8,
        message_id="00000000-0000-0000-0000-000000000002",
    )
    await async_session.commit()

    items, total = await provider_health.list_recent_routing_attempts(
        async_session,
        message_id="00000000-0000-0000-0000-000000000001",
    )
    assert total == 1
    assert items[0].provider == "meta_whatsapp"
    assert items[0].message_id == "00000000-0000-0000-0000-000000000001"


async def test_list_recent_routing_attempts_respects_pagination(
    async_session,
) -> None:
    """The ``limit`` and ``offset`` parameters let the
    dashboard walk the audit log without a full
    table scan. The test pins the contract that
    ``has_more`` can be derived from ``total`` and
    the page size.
    """
    for _ in range(5):
        await provider_health.record_routing_attempt(
            async_session,
            provider_name="meta_whatsapp",
            channel=Channel.WHATSAPP,
            outcome=RoutingLogOutcome.SUCCESS,
            latency_ms=1,
        )
    await async_session.commit()

    items, total = await provider_health.list_recent_routing_attempts(
        async_session,
        limit=2,
        offset=1,
    )
    assert total == 5
    assert len(items) == 2
    # Page 2 of a 5-row log: the offset skips the
    # first row and returns the next two.
    has_more = (1 + len(items)) < total
    assert has_more is True
