"""Messaging service.

This module orchestrates the message-sending flow: it picks the
right provider for a given channel, calls the provider, persists
the outcome and translates the result into a domain object the
route layer can serialise.

The service deliberately **does not** do any HTTP I/O – that
lives in the route handlers – and it **does not** enqueue
background work: the worker (added in a follow-up task) will
take the persisted message and dispatch it. The service is
synchronous from the caller's perspective: the provider is
``await``-ed inline, the row is committed, and the function
returns. A future iteration can swap the inline ``await`` for
"persist with status=``pending`` and let the worker pick it
up" without changing the route handler.

Provider failover (issue #11) is wired transparently through
:func:`app.adapters.registry.get_provider`: when
``Settings.provider_failover_chains`` names more than one
provider for the channel, the registry returns a
:class:`~app.adapters.failover.FailoverProvider` and the
service treats it like any other adapter. The
``Message.provider`` column records the *actual* upstream that
handled the call (taken from
:attr:`app.adapters.base.SendResult.provider_name`), so a
operator can tell a failover happened just by reading the row.

Public functions:

- :func:`send_message`  – persist + dispatch a single message.
- :func:`send_batch`    – persist + dispatch up to N messages
                          in one call (the hard cap is enforced
                          here so the route layer does not have
                          to care). Returns a
                          :class:`BatchOutcome` carrying the
                          ``batch_id`` the caller can poll
                          through :func:`get_batch` /
                          :func:`list_batches`.
- :func:`get_batch`     – read a single batch with its latest
                          counters. Cross-tenant access is
                          reported as :class:`BatchNotFoundError`.
- :func:`list_batches`  – paginated read of the authenticated
                          customer's batch history, ordered
                          newest first.
- :func:`get_message_status` – read a message's current state
                                from the database, refreshing
                                the provider status if the
                                row is still ``pending`` /
                                ``queued``.
- :func:`list_messages` – paginated read of the authenticated
                          customer's message history. Supports
                          the ``channel`` / ``status`` /
                          ``since`` / ``until`` filters the
                          usage dashboard uses to drive its
                          "historial" view.
- :func:`message_status_summary` – per-status aggregate of the
                                   customer's traffic for the
                                   "desglose por estado"
                                   card on the dashboard
                                   (delivered / failed /
                                   pending / queued / sent /
                                   unknown).
- :func:`daily_message_counts` – per-day, per-channel counts
                                 for the dashboard's "gráfico
                                 de barras" widget.
- :func:`compute_message_cost` – pure helper that maps a
                                  channel + plan to the cost /
                                  fee the message should be
                                  billed at. Kept separate from
                                  :func:`send_message` so the
                                  billing service can use the
                                  same logic without having to
                                  fire a real provider call.
- :func:`fire_batch_completion_webhook` – POST the
                                  campaign summary to the
                                  customer's ``webhook_url``
                                  when a batch reaches a
                                  terminal state. Opt-in
                                  (issue #9 acceptance
                                  criterion: "Webhook de
                                  batch completion
                                  funciona").
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.adapters.base import SendResult
from app.adapters.errors import ProviderError
from app.adapters.registry import get_provider
from app.config import Settings, get_settings
from app.models.batch import Batch, BatchStatus
from app.models.client import Client, ClientPlan
from app.models.message import Channel, Message, MessageStatus
from app.observability import get_logger, normalise_phone

if TYPE_CHECKING:
    from app.adapters.base import BaseProvider
    from app.services.webhook_delivery import WebhookDeliveryResult

    class _WebhookDeliveryLike(Protocol):
        """Structural type for the webhook delivery seam.

        :func:`fire_batch_completion_webhook` only depends
        on a single ``deliver`` method, so accepting the
        protocol (rather than the concrete
        :class:`WebhookDeliveryClient`) keeps the unit
        tests honest: a hand-rolled fake with the right
        shape is a valid injection, and a future
        refactor that swaps the underlying client (e.g.
        for an aiohttp-based one) does not have to
        inherit the current class."""

        async def deliver(
            self, *, url: str, body: bytes, headers: dict[str, str]
        ) -> WebhookDeliveryResult: ...


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Default cap on a single ``POST /v1/messages/batch`` request. The
# PRD targets campaigns of a few hundred messages at a time, so
# 100 leaves plenty of headroom while still preventing a single
# customer from monopolising a worker.
DEFAULT_BATCH_SIZE = 100
_BATCH_HARD_LIMIT = 500

# Default page size for the ``GET /v1/messages`` history endpoint.
# 50 rows covers a "last few days" view on a typical Starter-tier
# customer without rendering a multi-megabyte payload. The hard
# cap is enforced at the service layer so the route handler does
# not have to.
DEFAULT_LIST_LIMIT = 50
_LIST_HARD_LIMIT = 200

# Hard cap on the number of rows the CSV export endpoint will
# stream. A monthly report on a Growth-tier customer is well under
# this number; 10,000 leaves room for a multi-year archive dump
# while still preventing a malicious or runaway client from
# asking the API to render an unbounded CSV.
_EXPORT_HARD_LIMIT = 10_000

# Channel-specific character limits. The numbers match the
# operational ceilings the upstream providers enforce: WhatsApp
# text messages are capped at 4096 characters, GSM-7 SMS at
# 160. A stricter ``concatenated`` SMS goes up to 1530
# characters (10 segments of 153); we leave the segmenting to
# the provider and only enforce the conservative cap.
_CHANNEL_LIMITS: dict[Channel, int] = {
    Channel.SMS: 1600,
    Channel.WHATSAPP: 4096,
}

# Minimum acceptable length for a message body. We reject
# empty / whitespace-only payloads at the service layer so the
# provider never sees a body that would just burn quota.
_MIN_BODY_LENGTH = 1

# Provider base cost (in CLP cents) per channel. The MVP uses
# flat numbers so a deployment can configure the plan markup
# through ``Settings`` without forking the service. A future
# iteration will read these from a ``planes`` table.
_BASE_COST_CLP: dict[Channel, int] = {
    Channel.SMS: 25,  # CLP $25
    Channel.WHATSAPP: 80,  # CLP $0.80 per Meta conversation
}

# Plan-level markup (CLP cents per message). The numbers mirror
# the PRD's "Starter / Growth / Enterprise" plans: a flat
# markup of CLP $5 / $3 / $1 respectively.
_PLAN_MARKUP_CLP: dict[ClientPlan, int] = {
    ClientPlan.STARTER: 5,
    ClientPlan.GROWTH: 3,
    ClientPlan.ENTERPRISE: 1,
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MessagingError(Exception):
    """Base class for every messaging-domain exception.

    The route layer converts subclasses of this exception into a
    uniform HTTP response so the rest of the platform does not
    need to know which provider surfaced the failure.
    """

    http_status: int = 400
    code: str = "messaging_error"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidMessageError(MessagingError):
    """The request body did not pass validation."""

    http_status = 422
    code = "invalid_message"


class MessageNotFoundError(MessagingError):
    """The requested message id does not exist (or belongs to a
    different client – the error is the same so we do not leak
    the existence of someone else's resource)."""

    http_status = 404
    code = "message_not_found"


class BatchTooLargeError(MessagingError):
    """The batch request exceeded the hard cap."""

    http_status = 422
    code = "batch_too_large"


class BatchNotFoundError(MessagingError):
    """The requested batch id does not exist (or belongs to a
    different client – the error is the same so we do not leak
    the existence of someone else's resource)."""

    http_status = 404
    code = "batch_not_found"


class InvalidListFilterError(MessagingError):
    """The history query carries an invalid filter value.

    Raised when the dashboard sends a ``channel`` / ``status``
    value the platform does not know about, or a malformed
    ``since`` / ``until`` timestamp. Surfaced as a 422 so the
    caller can fix the input without having to back the
    history view out of the URL bar.
    """

    http_status = 422
    code = "invalid_list_filter"


class BatchRateLimitError(MessagingError):
    """The per-tenant batch rate limit was exceeded.

    The platform throttles ``POST /v1/messages/batch`` per
    client id (see :mod:`app.services.rate_limit`) so a
    single customer cannot flood the worker queue. The
    default ceiling is :attr:`Settings.batch_rate_limit_per_second`
    (100/s). The exception is surfaced as HTTP 429 by the
    route layer; the ``retry_after`` header is populated
    from :attr:`retry_after_seconds` so a well-behaved
    client backs off for the remainder of the window
    rather than busy-looping.
    """

    http_status = 429
    code = "batch_rate_limited"

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_seconds: int = 1,
    ) -> None:
        super().__init__(code, message)
        self.retry_after_seconds = int(retry_after_seconds)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendOutcome:
    """Outcome of a single :func:`send_message` call.

    ``message``         – the persisted :class:`Message` row.
    ``provider_msg_id`` – the identifier the upstream assigned;
                          ``None`` if the provider did not
                          acknowledge the message (the row is
                          then marked ``failed``).
    """

    message: Message
    provider_msg_id: str | None


@dataclass(frozen=True)
class BatchOutcome:
    """Outcome of a :func:`send_batch` call.

    ``batch_id``        – UUID of the :class:`app.models.batch.Batch`
                          row the call created. The route layer
                          surfaces it in the response so the caller
                          can poll progress through
                          :func:`get_batch` / :func:`list_batches`.
    ``results``         – per-item outcome in the same order as
                          the request. The route layer uses this
                          to render the ``results`` array in the
                          response.
    ``summary``         – rollup counters the route layer projects
                          onto the response so a caller can render
                          a "X delivered / Y failed" widget
                          without re-iterating the results.
    ``webhook_url``     – the canonical ``https://`` endpoint the
                          platform will POST the completion
                          summary to. ``None`` when the caller
                          did not configure a completion webhook
                          on the request. Echoed back to the
                          caller so the dashboard can confirm
                          the value the platform persisted.
    ``webhook_secret``  – the HMAC-SHA256 key the platform will
                          sign the completion POST with. Returned
                          exactly once: the caller is expected
                          to surface it to the user (or store it
                          in their secret manager) and discard it
                          from memory as soon as the call
                          returns. ``None`` when no webhook was
                          configured; a one-time secret the
                          platform just minted is surfaced under
                          :attr:`webhook_secret` so the customer
                          can verify the body out-of-band. The
                          value is also persisted on the
                          :class:`Batch` row so a future
                          re-fire does not have to mint a
                          second one.
    """

    batch_id: str
    results: list[SendOutcome]
    summary: BatchSummary
    webhook_url: str | None = None
    webhook_secret: str | None = None


@dataclass(frozen=True)
class BatchChannelSummary:
    """Per-channel rollup of a :class:`Batch`.

    Mirrors the same per-batch cost / fee rollup the
    :class:`BatchSummary` carries, but **broken down by
    channel** so the dashboard can render a one-line
    "SMS: 70 mensajes / CLP $2 450 · WhatsApp: 30 mensajes
    / CLP $2 550" widget without re-iterating the
    underlying ``mensajes`` table.

    The fields mirror the Batch-level rollup so a caller
    can sum ``channels[i].count`` to recover
    :attr:`BatchSummary.total` and
    ``sum(channels[i].total_cost_clp for ...)`` to recover
    :attr:`BatchSummary.total_cost_clp`:

    - ``channel``       – the :class:`Channel` this row
                          summarises.
    - ``count``         – total messages of the batch
                          bound for this channel.
    - ``pending``       – items still in flight
                          (``pending`` / ``queued`` /
                          ``sent`` / ``unknown``).
    - ``delivered``     – items in ``delivered`` state.
    - ``failed``        – items in ``failed`` state.
    - ``total_cost_clp`` – aggregated upstream cost (CLP
                           cents) for this channel.
    - ``total_fee_clp``  – aggregated platform markup
                           (CLP cents) for this channel.
    """

    channel: Channel
    count: int
    pending: int
    delivered: int
    failed: int
    total_cost_clp: int
    total_fee_clp: int

    @property
    def succeeded(self) -> int:
        """Return ``delivered + failed`` for this channel.

        Same contract as :attr:`BatchSummary.succeeded`:
        the value is the number of items that reached a
        terminal state. A dashboard that wants a
        per-channel "X of Y delivered" widget can read
        the single field without re-deriving the
        arithmetic.
        """
        return self.delivered + self.failed

    @property
    def total_amount_clp(self) -> int:
        """Return ``total_cost_clp + total_fee_clp``.

        Same convenience as
        :attr:`BatchSummary.total_amount_clp`; the value
        is the customer-facing total for this channel
        (upstream cost + platform markup).
        """
        return self.total_cost_clp + self.total_fee_clp


@dataclass(frozen=True)
class BatchSummary:
    """Headline counters of a :class:`Batch`.

    The dashboard renders the values directly on the
    "campañas" view without re-aggregating the underlying
    ``mensajes`` table. The fields are kept flat (no nested
    object) so the route layer can project them straight
    onto a Pydantic response model.

    ``total``        – number of items the caller submitted.
                       Mirrors :attr:`Batch.total_count`.
    ``pending``      – items still in flight. Mirrors
                       :attr:`Batch.pending_count`.
    ``delivered``    – items that reached ``delivered``. Mirrors
                       :attr:`Batch.delivered_count`.
    ``failed``       – items that ended up ``failed``. Mirrors
                       :attr:`Batch.failed_count`.
    ``succeeded``    – ``delivered + failed`` items. Computed
                       here so a caller that just wants
                       "how many made it through?" does not
                       have to re-derive the value.
    ``total_cost_clp`` – aggregated upstream cost (CLP cents)
                         across every message of the batch.
                         Mirrors :attr:`Batch.total_cost_clp`.
    ``total_fee_clp``  – aggregated platform markup (CLP cents)
                         across every message of the batch.
                         Mirrors :attr:`Batch.total_fee_clp`.
    ``total_amount_clp`` – ``total_cost_clp + total_fee_clp``
                           convenience for the dashboard's
                           "costo total de la campaña" widget.
    ``channels``     – per-channel rollup (list of
                       :class:`BatchChannelSummary`). One
                       row per :class:`Channel` value the
                       batch actually used, ordered by
                       the channel name for stable
                       rendering. The list is empty for
                       a freshly-created batch with no
                       items yet (the service layer only
                       ever sets ``channels`` once at
                       least one message has been
                       persisted).
    """

    total: int
    pending: int
    delivered: int
    failed: int
    total_cost_clp: int = 0
    total_fee_clp: int = 0
    channels: tuple[BatchChannelSummary, ...] = ()

    @property
    def succeeded(self) -> int:
        """Return the number of items that reached a terminal state.

        Defined as ``delivered + failed`` so the value is in
        sync with the underlying ``Batch`` row (a counter
        recompute sets the batch to ``completed`` when
        ``pending`` hits zero, which is exactly the condition
        where ``total == succeeded``).
        """
        return self.delivered + self.failed

    @property
    def total_amount_clp(self) -> int:
        """Return ``total_cost_clp + total_fee_clp``.

        Convenience for the dashboard's "costo total" widget:
        the customer-facing number is the sum of the upstream
        cost and the platform markup, so the value is a
        one-line render rather than a client-side sum.
        """
        return self.total_cost_clp + self.total_fee_clp


@dataclass(frozen=True)
class BatchDetail:
    """A :class:`Batch` row paired with its per-channel rollup.

    Returned by :func:`get_batch` so the route layer can
    render the batch's headline counters and the
    per-channel breakdown (``"SMS: 70 / CLP $2 450 ·
    WhatsApp: 30 / CLP $2 550"``) in a single
    round-trip. Splitting the result into ``batch`` /
    ``channels`` keeps the existing route projection
    helpers in :mod:`app.routes.messages` (which already
    know how to render a bare :class:`Batch` row) usable
    unchanged – the only new field is the per-channel
    list, surfaced as the ``channels`` field on
    :class:`BatchDetailResponse`.

    The pair is returned as a frozen dataclass (rather
    than attaching a transient attribute to the
    :class:`Batch` ORM object) so the route layer does
    not have to know whether the call refreshed the
    in-memory copy of the row.
    """

    batch: Batch
    channels: tuple[BatchChannelSummary, ...]


@dataclass(frozen=True)
class BatchListPage:
    """A single page of a customer's batch history.

    The shape mirrors :class:`MessageListPage` – the dashboard
    iterates the two with the same "items / total / has_more"
    idiom so a new feature ("campañas" view) can reuse the
    existing pagination component without a second code path.

    ``items``      – batches on this page, newest first.
    ``total``      – total number of batches matching the
                     filter across the full history.
    ``limit``      – the page size that was applied.
    ``offset``     – the offset that was applied.
    ``has_more``   – ``True`` when at least one more batch
                     exists after this page.
    ``channels_by_batch`` – per-batch :class:`BatchChannelSummary`
                            rollup keyed by the batch id. The
                            map only contains a key for
                            batches that have at least one
                            message persisted; the route
                            layer is expected to render an
                            empty ``channels`` list for
                            batches that do not appear in
                            the map. Computing the rollup
                            here (one query for the whole
                            page) avoids the N+1 trap a
                            per-row query would introduce.
    """

    items: list[Batch]
    total: int
    limit: int
    offset: int
    has_more: bool
    channels_by_batch: dict[str, tuple[BatchChannelSummary, ...]] | None = None


@dataclass(frozen=True)
class MessageListPage:
    """A single page of a customer's message history.

    ``items``      – messages on this page, newest first.
    ``total``      – total number of rows matching the filter
                     across the full history (not just the
                     current page). The dashboard uses the
                     value to render the "showing N of M"
                     counter.
    ``limit``      – the page size that was applied; useful
                     so the UI can echo the limit back to
                     the user.
    ``offset``     – the offset that was applied; same
                     rationale as ``limit``.
    ``has_more``   – ``True`` when there is at least one more
                     row after the current page, so the
                     dashboard can render a "cargar más"
                     button without having to do a count
                     query of its own.
    """

    items: list[Message]
    total: int
    limit: int
    offset: int
    has_more: bool


@dataclass(frozen=True)
class MessageExport:
    """A bulk slice of the customer's message history for export.

    The shape mirrors :class:`MessageListPage` minus the
    pagination knobs (a CSV export is single-shot, not a
    paged read) plus a ``truncated`` flag so the caller can
    warn the user when the result was capped at
    :data:`_EXPORT_HARD_LIMIT`. ``truncated=True`` means
    "the full history is longer than what you got back";
    a UI that ignores the flag will silently drop the older
    rows.
    """

    items: list[Message]
    total: int
    truncated: bool


@dataclass(frozen=True)
class DailyMessageCount:
    """The number of messages dispatched on a single calendar day.

    The dashboard uses the value to drive the "gráfico de
    barras" of daily usage (PRD user story #8: "ver en un
    dashboard cuántos mensajes envié en el mes"). The
    ``channel`` field is ``"all"`` for the totals row and
    one of :class:`Channel` for a per-channel breakdown; the
    service layer returns both shapes from the same query
    so the route handler does not have to issue a second
    round-trip just to colour-code the bars.
    """

    day: date
    channel: str
    count: int


@dataclass(frozen=True)
class DailyUsagePage:
    """The output of :func:`daily_message_counts`.

    The dataclass pairs the per-day aggregation buckets
    with the resolved ``since`` / ``until`` window the
    service picked. The route layer echoes the window in
    the response so the dashboard can render the chart
    axis without having to mirror the default-window
    logic on the client.
    """

    items: list[DailyMessageCount]
    since: datetime
    until: datetime


@dataclass(frozen=True)
class MessageStatusCount:
    """A single ``(status, count)`` row in a status summary.

    The dashboard uses the value to render the
    "desglose por estado" widget (the breakdown of
    delivered / failed / pending / queued / sent /
    unknown messages for the active period). A
    :class:`MessageStatusSummary` always carries one
    entry per :class:`MessageStatus` value – the
    service layer zero-fills any status with no
    traffic so the dashboard does not have to
    special-case missing rows.
    """

    status: MessageStatus
    count: int


@dataclass(frozen=True)
class MessageStatusSummary:
    """The output of :func:`message_status_summary`.

    The dataclass is the source of truth for the
    "desglose por estado" card on the dashboard. It
    pairs the per-status counts with the headline
    ``total`` counter, the resolved ``since`` /
    ``until`` window, the summed ``cost_clp`` /
    ``fee_clp`` amounts and the ``delivery_rate`` the
    dashboard renders as a progress bar.

    The window is echoed back so the dashboard can
    show "resumen del 1 al 30 de junio" without
    having to mirror the default-31-day-window
    logic on the client (the same rationale as
    :class:`DailyUsagePage`).
    """

    items: list[MessageStatusCount]
    total: int
    delivered: int
    failed: int
    pending: int
    cost_clp: int
    fee_clp: int
    since: datetime
    until: datetime

    @property
    def delivery_rate(self) -> float:
        """Fraction of messages that reached ``delivered``.

        The value is in the closed interval ``[0.0, 1.0]``
        and defaults to ``0.0`` when the customer has not
        sent any messages in the window – a dashboard
        that divides by ``total`` would otherwise blow up
        on a brand-new account. The route layer projects
        the value onto a 0-100 percentage so the client
        does not have to multiply by 100 itself.
        """
        if self.total <= 0:
            return 0.0
        return min(1.0, max(0.0, self.delivered / self.total))


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


# A loose body-shape check: at least one non-whitespace
# character. The Pydantic model on the route layer already
# rejects empty strings, but the service layer is the one that
# gets called from the worker (or any future in-process
# caller), so the check has to live here too.
_BODY_RE = re.compile(r"\S")


def _validate_body(channel: Channel, body: str) -> str:
    """Return the cleaned body or raise :class:`InvalidMessageError`."""
    if not isinstance(body, str):
        raise InvalidMessageError("invalid_body", "body must be a string")
    cleaned = body.strip()
    if len(cleaned) < _MIN_BODY_LENGTH or not _BODY_RE.search(cleaned):
        raise InvalidMessageError("invalid_body", "body cannot be empty")
    limit = _CHANNEL_LIMITS.get(channel, 4096)
    if len(cleaned) > limit:
        raise InvalidMessageError(
            "body_too_long",
            f"body exceeds the {channel.value} limit of {limit} characters",
        )
    return cleaned


def _validate_destination(channel: Channel, to: str) -> str:
    """Return the canonical ``+56…`` destination or raise."""
    if not isinstance(to, str) or not to.strip():
        raise InvalidMessageError("invalid_destination", "destination is required")
    canonical = normalise_phone(to)
    if canonical is None:
        raise InvalidMessageError(
            "invalid_destination",
            "destination must be a valid Chilean mobile number",
        )
    return canonical


# Maximum length (in characters) of a batch-completion
# webhook URL. The database column already enforces
# ``<=500``; the service layer asserts the same value
# eagerly so a request that would have failed at the INSERT
# round-trip fails at the service boundary instead.
_BATCH_WEBHOOK_URL_MAX_LENGTH = 500

# Maximum length of a customer-supplied webhook signing
# secret. 128 matches the same ceiling the
# :class:`app.models.webhook.Webhook.secret` column uses.
_BATCH_WEBHOOK_SECRET_MAX_LENGTH = 128


def _validate_webhook_url(url: str) -> str:
    """Return a clean ``https://`` URL or raise.

    Mirrors the same scheme + length checks
    :func:`app.services.webhooks._validate_url` applies to
    the per-message delivery-receipt subscriptions so a
    misconfigured customer cannot accidentally ship the
    completion notification over the public internet in
    clear text. The function is duplicated here (rather
    than imported) to keep the dependency arrow pointing
    from ``messaging`` -> ``webhooks`` one-way; a future
    consolidation can lift the helper into a shared
    ``validators`` module.
    """
    if not isinstance(url, str):
        raise InvalidMessageError("invalid_webhook_url", "url must be a string")
    cleaned = url.strip()
    if not cleaned:
        raise InvalidMessageError("invalid_webhook_url", "url is required")
    if len(cleaned) > _BATCH_WEBHOOK_URL_MAX_LENGTH:
        raise InvalidMessageError(
            "invalid_webhook_url",
            f"url must be at most {_BATCH_WEBHOOK_URL_MAX_LENGTH} characters",
        )
    try:
        from urllib.parse import urlparse

        parsed = urlparse(cleaned)
    except ValueError as exc:  # pragma: no cover - urlparse is permissive
        raise InvalidMessageError("invalid_webhook_url", str(exc)) from exc
    if parsed.scheme != "https":
        raise InvalidMessageError(
            "invalid_webhook_url",
            "url must use the https scheme",
        )
    if not parsed.netloc:
        raise InvalidMessageError(
            "invalid_webhook_url", "url is missing the host"
        )
    return cleaned


def _validate_webhook_secret(secret: str) -> str:
    """Return the canonical webhook signing secret or raise.

    Accepts any non-empty string up to the 128-character
    column ceiling. The value is treated as opaque – we do
    not impose a minimum entropy floor because the customer
    might be using a deliberately long passphrase and we
    do not want to second-guess the secret they chose.
    """
    if not isinstance(secret, str):
        raise InvalidMessageError(
            "invalid_webhook_secret", "secret must be a string"
        )
    cleaned = secret.strip()
    if not cleaned:
        raise InvalidMessageError(
            "invalid_webhook_secret", "secret cannot be empty"
        )
    if len(cleaned) > _BATCH_WEBHOOK_SECRET_MAX_LENGTH:
        raise InvalidMessageError(
            "invalid_webhook_secret",
            f"secret must be at most {_BATCH_WEBHOOK_SECRET_MAX_LENGTH} characters",
        )
    return cleaned


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------


def compute_message_cost(*, channel: Channel, plan: ClientPlan) -> tuple[int, int]:
    """Return ``(cost_clp, fee_clp)`` for a single message.

    The cost is the upstream's flat per-message price (the
    values are placeholders – the real numbers will land in a
    ``planes`` table). The fee is the markup the platform
    adds on top, indexed by the client's plan.

    Exposed as a public helper so the billing service can use
    the same logic without firing a real provider call.
    """
    cost = _BASE_COST_CLP.get(channel, 0)
    fee = _PLAN_MARKUP_CLP.get(plan, 0)
    return cost, fee


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


async def _persist_message(
    session: AsyncSession,
    *,
    client: Client,
    channel: Channel,
    to: str,
    body: str,
    settings: Settings,
    batch_id: str | None = None,
) -> Message:
    """Create and persist a :class:`Message` row in ``pending`` state.

    The caller is responsible for the actual provider dispatch;
    this helper just makes sure the row exists with the right
    defaults so the worker's "pick up pending messages" query
    can find it.

    ``batch_id`` is ``None`` for the single-message path
    (``POST /v1/messages``); the batch path always sets it so
    every message it produces is grouped under the
    :class:`app.models.batch.Batch` row the same call
    creates.
    """
    cost, fee = compute_message_cost(channel=channel, plan=client.plan)
    message = Message(
        client_id=client.id,
        provider=_provider_name_for(channel, settings=settings),
        channel=channel,
        to_number=to,
        body=body,
        status=MessageStatus.PENDING,
        cost_clp=cost,
        fee_clp=fee,
        batch_id=batch_id,
    )
    session.add(message)
    await session.flush()  # populate server defaults
    return message


def _provider_name_for(channel: Channel, *, settings: Settings) -> str:
    """Return the registered provider's name for ``channel``.

    Looking the name up through the registry (rather than
    hard-coding ``"meta_whatsapp"`` / ``"sms_aggregator"``)
    keeps the persistence layer in sync with the routing
    layer: a future swap of the SMS provider would only need
    a registry change, not a database migration.
    """
    provider = get_provider(channel, settings=settings)
    return provider.name


async def _dispatch(
    provider: BaseProvider,
    message: Message,
) -> SendResult:
    """Call ``provider.send`` and translate the result.

    The helper is kept thin: it returns the provider's
    :class:`SendResult` on success and converts
    :class:`ProviderError` into a :class:`MessagingError` so
    the route layer only has to know about one exception
    hierarchy.
    """
    try:
        return await provider.send(to=message.to_number, body=message.body)
    except ProviderError:
        # The provider layer already classifies the failure
        # (unavailable / validation / rate limit). We re-raise
        # so the route layer can surface the correct HTTP
        # status without re-deriving it from a string.
        raise


def _map_provider_status(raw: str) -> MessageStatus:
    """Translate a provider-specific status string into our enum.

    The mapping is deliberately conservative: anything we do
    not recognise becomes :class:`MessageStatus.UNKNOWN` so a
    future provider status does not silently downgrade a
    message to ``delivered`` just because the string changed.
    """
    if not raw:
        return MessageStatus.UNKNOWN
    normalised = raw.strip().lower()
    if normalised in {"delivered", "read"}:
        return MessageStatus.DELIVERED
    if normalised in {"sent", "accepted", "submitted"}:
        return MessageStatus.SENT
    if normalised in {"queued", "accepted_by_carrier"}:
        return MessageStatus.QUEUED
    if normalised in {"failed", "rejected", "undelivered", "error"}:
        return MessageStatus.FAILED
    if normalised in {"pending", "accepted_by_provider"}:
        return MessageStatus.PENDING
    return MessageStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


async def send_message(
    session: AsyncSession,
    *,
    client: Client,
    channel: Channel,
    to: str,
    body: str,
    settings: Settings | None = None,
    batch_id: str | None = None,
) -> SendOutcome:
    """Persist + dispatch a single message and return the outcome.

    The function commits the row before returning so a
    successful ``POST /v1/messages`` is durable even if the
    worker that picks up the delivery receipt crashes in
    parallel.

    ``batch_id`` is ``None`` for the single-message path; the
    batch path always passes the id of the
    :class:`app.models.batch.Batch` row it just created so
    every message in the batch is grouped under the same
    parent.
    """
    cfg = settings or get_settings()
    canonical_to = _validate_destination(channel, to)
    canonical_body = _validate_body(channel, body)

    message = await _persist_message(
        session,
        client=client,
        channel=channel,
        to=canonical_to,
        body=canonical_body,
        settings=cfg,
        batch_id=batch_id,
    )
    # Flush + commit before the network call: a failed
    # dispatch must not roll back the row (the worker / ops
    # team still needs to be able to see *what* was attempted).
    await session.commit()
    await session.refresh(message)

    provider = get_provider(channel, settings=cfg)
    try:
        result: SendResult = await _dispatch(provider, message)
    except ProviderError as exc:
        message.status = MessageStatus.FAILED
        message.error_code = exc.code
        message.error_message = exc.message[:500]
        # If the failover wrapper re-raised a non-retryable
        # error from one of its underlyings, the synthetic
        # chain name in ``message.provider`` is misleading –
        # the operator wants to know *which* upstream
        # surfaced the rejection. ``exc.provider`` carries the
        # underlying adapter's name (set by the concrete
        # adapter) so we can keep the column accurate even
        # on a failed dispatch.
        if exc.provider and exc.provider != message.provider:
            message.provider = exc.provider
        await session.commit()
        await session.refresh(message)
        return SendOutcome(message=message, provider_msg_id=None)

    message.status = MessageStatus.SENT
    message.provider_msg_id = result.provider_msg_id
    # The failover router may have switched providers mid-call;
    # ``result.provider_name`` carries the *actual* upstream that
    # accepted the message so an operator looking at the
    # ``Message.provider`` column can tell a failover happened.
    actual_provider = result.provider_name or provider.name
    if actual_provider != message.provider:
        message.provider = actual_provider
    await session.commit()
    await session.refresh(message)
    return SendOutcome(message=message, provider_msg_id=result.provider_msg_id)


async def send_batch(
    session: AsyncSession,
    *,
    client: Client,
    items: list[dict[str, str]],
    settings: Settings | None = None,
    name: str | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> BatchOutcome:
    """Persist + dispatch a batch of messages.

    ``items`` is a list of dicts with the same shape as the
    :class:`SendMessageRequest` model the route layer accepts
    (``channel``, ``to``, ``body``). The hard cap is enforced
    before any persistence work so a malicious client cannot
    enqueue thousands of rows by accident.

    The function groups every message under a fresh
    :class:`app.models.batch.Batch` row, then returns the
    :class:`BatchOutcome` so the route layer can surface the
    ``batch_id`` in the response. A future iteration can swap
    the inline ``await`` for "persist with status=``pending``
    and let the worker pick it up" without changing the route
    handler.

    ``webhook_url`` (optional, issue #9 acceptance criterion
    "Webhook de batch completion funciona") is the
    ``https://`` endpoint the platform POSTs a JSON summary
    to once the batch reaches a terminal state. ``None``
    disables the completion webhook – the customer can still
    poll progress through :func:`get_batch` /
    :func:`list_batches`.

    ``webhook_secret`` is the HMAC-SHA256 key the platform
    uses to sign the completion POST. When the caller omits
    the value, the function mints a one-time secret (32 bytes
    of CSPRNG entropy, hex-encoded) so the customer can
    verify the body out-of-band; the generated value is
    surfaced on the returned :class:`BatchOutcome` so the
    route layer can echo it back to the caller.

    Cross-item isolation: a single bad item (``invalid_channel``
    / ``body_too_long`` …) raises before the loop runs, so the
    whole batch is rejected and the caller's retry policy
    stays simple. A per-item provider failure (rate limit,
    upstream down) is recorded on the row (``status=failed``)
    but does **not** abort the batch – a campaign with one
    bad number should still let the other 99 messages
    through.
    """
    cfg = settings or get_settings()
    if not isinstance(items, list):
        raise InvalidMessageError("invalid_batch", "items must be a list")
    if not items:
        raise InvalidMessageError("invalid_batch", "items cannot be empty")
    if len(items) > _BATCH_HARD_LIMIT:
        raise BatchTooLargeError(
            "batch_too_large",
            f"batch size {len(items)} exceeds the hard limit of {_BATCH_HARD_LIMIT}",
        )

    # Pre-validate the channel of every item so a single bad
    # channel does not abort the batch *after* half the
    # messages have been persisted. The Pydantic model on the
    # route layer already catches this case, but the worker /
    # in-process caller does not necessarily go through the
    # route, so the check has to live here too.
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise InvalidMessageError(
                "invalid_batch",
                f"item {index} is not an object",
            )
        try:
            Channel(str(item.get("channel", "")))
        except ValueError as exc:
            raise InvalidMessageError("invalid_channel", str(exc)) from exc

    # Normalise the webhook configuration eagerly. We accept
    # a missing URL (caller does not want a completion
    # notification) but reject a URL that is not a
    # well-formed ``https://`` endpoint, so a misconfigured
    # customer cannot accidentally ship the secret to an
    # ``http://`` listener or a typo'd ``javascript:`` URL.
    canonical_webhook_url: str | None = None
    canonical_webhook_secret: str | None = None
    if webhook_url is not None and webhook_url != "":
        canonical_webhook_url = _validate_webhook_url(webhook_url)
        if webhook_secret is not None and webhook_secret != "":
            canonical_webhook_secret = _validate_webhook_secret(webhook_secret)
        else:
            # Mint a one-time secret. 32 bytes of CSPRNG
            # entropy (64 hex characters) matches the same
            # ceiling the per-message
            # :class:`app.models.webhook.Webhook` model
            # uses for its signing key.
            import secrets

            canonical_webhook_secret = secrets.token_hex(32)

    # Create the Batch row up-front so every message can
    # carry its ``batch_id`` and the counters can be updated
    # in the same transaction. The ``total_count`` is frozen
    # at submission time so the dashboard can render "X of Y"
    # without re-deriving the denominator.
    batch = Batch(
        client_id=client.id,
        name=(name or None),
        total_count=len(items),
        pending_count=len(items),
        delivered_count=0,
        failed_count=0,
        status=BatchStatus.PROCESSING,
        webhook_url=canonical_webhook_url,
        webhook_secret=canonical_webhook_secret,
    )
    session.add(batch)
    await session.flush()  # populate ``batch.id`` so messages can FK it

    outcomes: list[SendOutcome] = []
    for item in items:
        # We dispatch sequentially so a single bad item does
        # not consume the upstream's rate-limit budget. The
        # worker (added in a follow-up task) will run batches
        # in parallel; the synchronous path here is the
        # "one-shot" behaviour the API edge advertises.
        channel = Channel(str(item.get("channel", "")))
        outcome = await send_message(
            session,
            client=client,
            channel=channel,
            to=item.get("to", ""),
            body=item.get("body", ""),
            settings=cfg,
            batch_id=batch.id,
        )
        outcomes.append(outcome)

    # All messages have reached a terminal state
    # (``sent`` / ``failed``) by the time ``send_message``
    # returns, so the counter recompute is the last step
    # before the route layer surfaces the response.
    await _recompute_batch_counters(session, batch=batch)
    await session.commit()
    await session.refresh(batch)

    # The per-channel breakdown is computed once the
    # per-item status has settled so the response carries
    # the same numbers the dashboard's "campañas" widget
    # renders. The query is one indexed read on
    # ``mensajes.batch_id`` (the same index the
    # ``_recompute_batch_counters`` ``GROUP BY`` already
    # uses) so the cost is bounded by the batch's hard
    # cap (``_BATCH_HARD_LIMIT``).
    channels = await _batch_channel_breakdown(session, batch_id=batch.id)

    return BatchOutcome(
        batch_id=batch.id,
        results=outcomes,
        summary=BatchSummary(
            total=batch.total_count,
            pending=batch.pending_count,
            delivered=batch.delivered_count,
            failed=batch.failed_count,
            total_cost_clp=batch.total_cost_clp,
            total_fee_clp=batch.total_fee_clp,
            channels=channels,
        ),
        webhook_url=canonical_webhook_url,
        webhook_secret=canonical_webhook_secret,
    )


async def _recompute_batch_counters(session: AsyncSession, *, batch: Batch) -> None:
    """Recompute the denormalised counters on a :class:`Batch` row.

    The single source of truth for the counters is the
    underlying ``mensajes`` table – the values on
    ``batch`` are denormalised so the dashboard can render
    "X of Y delivered" without an aggregate query on every
    read. The recompute is a single ``GROUP BY status`` over
    the messages of one batch; the cost is one indexed read
    on the ``(batch_id, status)`` path the future worker
    will own.

    The function also flips :attr:`Batch.status` to
    :class:`BatchStatus.COMPLETED` (or :class:`BatchStatus.FAILED`
    when every item ended up ``failed``) so a polling
    dashboard can detect a finished campaign without
    inspecting the per-item counters.
    """
    stmt = (
        select(
            Message.status,
            func.count(Message.id),
            func.coalesce(func.sum(Message.cost_clp), 0),
            func.coalesce(func.sum(Message.fee_clp), 0),
        )
        .where(Message.batch_id == batch.id)
        .group_by(Message.status)
    )
    rows = (await session.execute(stmt)).all()

    pending = 0
    delivered = 0
    failed = 0
    total_cost = 0
    total_fee = 0
    for raw_status, raw_count, raw_cost, raw_fee in rows:
        # The ``MessageStatus`` column round-trips through
        # the ``_StringEnum`` decorator, so the row value
        # is either a :class:`MessageStatus` member or a
        # ``str`` (the SQLAlchemy core path). We accept both.
        count = int(raw_count or 0)
        total_cost += int(raw_cost or 0)
        total_fee += int(raw_fee or 0)
        if isinstance(raw_status, MessageStatus):
            status = raw_status
        else:
            try:
                status = MessageStatus(str(raw_status))
            except ValueError:
                status = MessageStatus.UNKNOWN
        if status == MessageStatus.DELIVERED:
            delivered += count
        elif status == MessageStatus.FAILED:
            failed += count
        else:
            # ``pending`` / ``queued`` / ``sent`` / ``unknown``
            # are all "in flight" from the batch's point of
            # view. The dashboard does not split them at the
            # batch level – the per-status breakdown is on
            # the per-message history endpoint.
            pending += count

    batch.pending_count = pending
    batch.delivered_count = delivered
    batch.failed_count = failed
    batch.total_cost_clp = total_cost
    batch.total_fee_clp = total_fee

    # Status transition: ``completed`` once no item is in
    # flight; ``failed`` if every item ended up ``failed``
    # (no partial success – useful for the dashboard's
    # "campaña fallida" filter).
    if pending == 0 and batch.total_count > 0:
        if delivered == 0 and failed > 0:
            batch.status = BatchStatus.FAILED
        else:
            batch.status = BatchStatus.COMPLETED
        # ``completed_at`` is set the first time the batch
        # reaches a terminal state. A second call (e.g. a
        # re-compute from a delivery-receipt webhook arriving
        # after the batch was already marked completed) leaves
        # the timestamp untouched so the value is "when did
        # the batch finish?", not "when was the last
        # counter recompute?".
        if batch.completed_at is None:
            from datetime import datetime as _dt

            batch.completed_at = _dt.now(tz=UTC)


# ---------------------------------------------------------------------------
# Per-channel rollup
# ---------------------------------------------------------------------------
#
# The dashboard's "Campañas" view renders the campaign's total
# cost ("CLP $X") plus a per-channel breakdown ("SMS: 70
# mensajes / CLP $2 450 · WhatsApp: 30 mensajes / CLP $2 550")
# so a customer can tell at a glance which channel drove the
# spend. The rollup is computed on demand from the underlying
# ``mensajes`` table (the per-batch cost / fee columns on the
# ``lotes`` row are only the totals) so the value is always
# in sync with the latest delivery-receipt update – no separate
# counter column is needed on the batch row.


async def _batch_channel_breakdown(
    session: AsyncSession,
    *,
    batch_id: str,
) -> tuple[BatchChannelSummary, ...]:
    """Return the per-channel rollup for a single batch.

    The query mirrors the per-status recompute in
    :func:`_recompute_batch_counters` but ``GROUP BY channel``
    instead of ``status``. The result is a stable
    ``(channel, count, …)`` projection the route layer
    projects straight onto the response.

    The function is intentionally narrow: a single
    ``GROUP BY channel, status`` returns one row per
    (channel, status) pair and the helper aggregates the
    rows into the :class:`BatchChannelSummary` shape the
    dashboard renders. The output is ordered by
    ``channel.value`` so the response is stable across
    calls (a future caller iterating the list does not
    have to re-sort on the client).
    """
    if not batch_id:
        return ()
    stmt = (
        select(
            Message.channel,
            Message.status,
            func.count(Message.id),
            func.coalesce(func.sum(Message.cost_clp), 0),
            func.coalesce(func.sum(Message.fee_clp), 0),
        )
        .where(Message.batch_id == batch_id)
        .group_by(Message.channel, Message.status)
    )
    rows = (await session.execute(stmt)).all()

    # Aggregate the per-(channel, status) rows into the
    # per-channel rollup. ``pending`` is the union of
    # ``pending`` / ``queued`` / ``sent`` / ``unknown``
    # – same definition :func:`_recompute_batch_counters`
    # uses for the batch-level rollup.
    by_channel: dict[Channel, dict[str, int]] = {}
    for raw_channel, raw_status, raw_count, raw_cost, raw_fee in rows:
        count = int(raw_count or 0)
        cost = int(raw_cost or 0)
        fee = int(raw_fee or 0)
        if isinstance(raw_channel, Channel):
            channel = raw_channel
        else:
            try:
                channel = Channel(str(raw_channel))
            except ValueError:
                # An unknown channel string is treated as a
                # caller bug (the column is an enum, so this
                # only happens on a manual DB edit) and is
                # silently dropped to avoid breaking the
                # response.
                continue
        if isinstance(raw_status, MessageStatus):
            status = raw_status
        else:
            try:
                status = MessageStatus(str(raw_status))
            except ValueError:
                status = MessageStatus.UNKNOWN
        bucket = by_channel.setdefault(
            channel,
            {
                "count": 0,
                "pending": 0,
                "delivered": 0,
                "failed": 0,
                "cost": 0,
                "fee": 0,
            },
        )
        bucket["count"] += count
        bucket["cost"] += cost
        bucket["fee"] += fee
        if status == MessageStatus.DELIVERED:
            bucket["delivered"] += count
        elif status == MessageStatus.FAILED:
            bucket["failed"] += count
        else:
            bucket["pending"] += count

    return tuple(
        BatchChannelSummary(
            channel=channel,
            count=bucket["count"],
            pending=bucket["pending"],
            delivered=bucket["delivered"],
            failed=bucket["failed"],
            total_cost_clp=bucket["cost"],
            total_fee_clp=bucket["fee"],
        )
        for channel, bucket in sorted(by_channel.items(), key=lambda item: item[0].value)
    )


async def _batch_channel_breakdowns(
    session: AsyncSession,
    *,
    batch_ids: Iterable[str],
) -> dict[str, tuple[BatchChannelSummary, ...]]:
    """Return the per-channel rollup for many batches in a single query.

    The dashboard's listing endpoint (``GET
    /v1/messages/batch``) renders a "Campañas" table where
    every row needs the per-channel breakdown. Doing one
    query per batch would be the textbook N+1 problem; this
    helper issues a single ``GROUP BY batch_id, channel,
    status`` and reassembles the per-batch slices in
    Python. The cost is one indexed read on
    ``mensajes.batch_id`` (the index the existing
    ``_recompute_batch_counters`` already relies on) so the
    listing endpoint stays single-round-trip.

    Batches that have no messages yet (a freshly-created
    batch that has not been flushed, or a batch whose items
    were all rolled back) are not in the result – the route
    layer is expected to render an empty ``channels``
    array for those, the same shape the
    :func:`_batch_channel_breakdown` helper returns for an
    empty input.
    """
    ids = [str(value) for value in batch_ids if value]
    if not ids:
        return {}
    stmt = (
        select(
            Message.batch_id,
            Message.channel,
            Message.status,
            func.count(Message.id),
            func.coalesce(func.sum(Message.cost_clp), 0),
            func.coalesce(func.sum(Message.fee_clp), 0),
        )
        .where(Message.batch_id.in_(ids))
        .group_by(Message.batch_id, Message.channel, Message.status)
    )
    rows = (await session.execute(stmt)).all()

    # ``accumulator`` is a ``{batch_id: {channel: bucket}}``
    # double dict so the loop below can stay branchless. The
    # outer dict is keyed by the batch id (the column we
    # ``GROUP BY``-ed) and the inner dict is keyed by the
    # :class:`Channel` (the per-batch rollup).
    accumulator: dict[str, dict[Channel, dict[str, int]]] = {}
    for raw_batch_id, raw_channel, raw_status, raw_count, raw_cost, raw_fee in rows:
        if raw_batch_id is None:
            # A message without a ``batch_id`` cannot be
            # part of any batch – this is the single-message
            # path (``POST /v1/messages``), which is not in
            # the listing. Silently drop so a stray row does
            # not break the response.
            continue
        batch_id = str(raw_batch_id)
        count = int(raw_count or 0)
        cost = int(raw_cost or 0)
        fee = int(raw_fee or 0)
        if isinstance(raw_channel, Channel):
            channel = raw_channel
        else:
            try:
                channel = Channel(str(raw_channel))
            except ValueError:
                continue
        if isinstance(raw_status, MessageStatus):
            status = raw_status
        else:
            try:
                status = MessageStatus(str(raw_status))
            except ValueError:
                status = MessageStatus.UNKNOWN
        per_batch = accumulator.setdefault(batch_id, {})
        bucket = per_batch.setdefault(
            channel,
            {
                "count": 0,
                "pending": 0,
                "delivered": 0,
                "failed": 0,
                "cost": 0,
                "fee": 0,
            },
        )
        bucket["count"] += count
        bucket["cost"] += cost
        bucket["fee"] += fee
        if status == MessageStatus.DELIVERED:
            bucket["delivered"] += count
        elif status == MessageStatus.FAILED:
            bucket["failed"] += count
        else:
            bucket["pending"] += count

    return {
        batch_id: tuple(
            BatchChannelSummary(
                channel=channel,
                count=bucket["count"],
                pending=bucket["pending"],
                delivered=bucket["delivered"],
                failed=bucket["failed"],
                total_cost_clp=bucket["cost"],
                total_fee_clp=bucket["fee"],
            )
            for channel, bucket in sorted(
                per_channel.items(), key=lambda item: item[0].value
            )
        )
        for batch_id, per_channel in accumulator.items()
    }


# ---------------------------------------------------------------------------
# Batch lookup
# ---------------------------------------------------------------------------


async def get_batch(
    session: AsyncSession,
    *,
    client: Client,
    batch_id: str,
    settings: Settings | None = None,
) -> BatchDetail:
    """Return a single :class:`Batch` row together with its
    per-channel rollup, recomputing the counters on the
    way out.

    The cross-tenant access guard reports an unknown id the
    same way :func:`get_message_status` does: a batch that
    belongs to a different client is reported as
    :class:`BatchNotFoundError` (the same response an
    unauthenticated caller would see) so the existence of
    another tenant's campaign is not leaked.

    The counters are recomputed (rather than read straight
    off the row) so a campaign that has been receiving
    delivery receipts asynchronously through the webhook
    loop still shows up-to-date numbers when the customer
    opens the dashboard.

    The return type is a :class:`BatchDetail` (a tuple of
    the batch row and its per-channel rollup) rather than
    a bare :class:`Batch` so the route layer does not have
    to issue a second query to fetch the per-channel
    breakdown the dashboard's "Campañas" view needs.
    """
    _ = settings  # kept for symmetry with the rest of the service
    if not isinstance(batch_id, str) or not batch_id:
        raise BatchNotFoundError("batch_not_found", "batch id is required")
    stmt = select(Batch).where(Batch.id == batch_id)
    result = await session.execute(stmt)
    batch = result.scalar_one_or_none()
    if batch is None or batch.client_id != client.id:
        raise BatchNotFoundError("batch_not_found", "batch does not exist")

    # A recompute is one indexed ``GROUP BY status`` over
    # the messages of one batch. The cost is bounded by the
    # hard cap on the batch (``_BATCH_HARD_LIMIT``) so even
    # a campaign at the limit is a sub-millisecond read.
    await _recompute_batch_counters(session, batch=batch)
    if batch.pending_count == 0 and batch.total_count > 0:
        # The row is dirty until we commit. The caller (the
        # route layer) will commit via the dependency; we
        # flush here so the in-memory copy is in sync with
        # the SQL we just ran.
        await session.commit()
    await session.refresh(batch)
    # The per-channel breakdown is computed once the
    # per-item status has settled (i.e. after the recompute
    # above) so the response carries the same numbers the
    # dashboard renders. The query is a single indexed
    # read on ``mensajes.batch_id``.
    channels = await _batch_channel_breakdown(session, batch_id=batch.id)
    return BatchDetail(batch=batch, channels=channels)


async def list_batches(
    session: AsyncSession,
    *,
    client: Client,
    status: object | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    settings: Settings | None = None,
) -> BatchListPage:
    """Return a paginated slice of the customer's batch history.

    Mirrors :func:`list_messages` (same ``limit`` / ``offset``
    semantics, same ``total`` / ``has_more`` projection) so
    the dashboard's "campañas" view can reuse the same
    pagination component the "historial" view already uses.

    The result is ordered by ``created_at`` descending so the
    most recent campaign is the first row of the response –
    the dashboard does not have to re-sort on the client.
    """
    _ = settings  # kept for symmetry with the rest of the service
    if not isinstance(client, Client):
        raise InvalidListFilterError("invalid_client", "client is required")
    status_filter = _normalise_batch_status_filter(status)
    limit = _coerce_int(limit, field="limit", minimum=1)
    offset = _coerce_int(offset, field="offset", minimum=0)
    if limit > _LIST_HARD_LIMIT:
        limit = _LIST_HARD_LIMIT

    where: list[ColumnElement[bool]] = [Batch.client_id == client.id]
    if status_filter is not None:
        where.append(Batch.status == status_filter)

    list_stmt = (
        select(Batch)
        .where(and_(*where))
        .order_by(Batch.created_at.desc(), Batch.id.desc())
        .limit(limit)
        .offset(offset)
    )
    count_stmt = select(func.count(Batch.id)).where(and_(*where))

    list_result = await session.execute(list_stmt)
    items = list(list_result.scalars().all())
    count_result = await session.execute(count_stmt)
    total = int(count_result.scalar_one() or 0)
    has_more = (offset + len(items)) < total

    # Per-channel rollup for the page: a single
    # ``GROUP BY batch_id, channel, status`` over the
    # returned batch ids. Issuing one round-trip per
    # batch would be the textbook N+1 trap, so we
    # batch the query here and return a ``batch_id ->
    # channels`` map the route layer iterates alongside
    # ``items``. Batches with no messages (a freshly
    # created batch that has not been flushed yet, or a
    # batch whose items were all rolled back) are not in
    # the map; the route layer is expected to render an
    # empty ``channels`` list for those.
    batch_ids = [batch.id for batch in items]
    channels_by_batch = await _batch_channel_breakdowns(
        session, batch_ids=batch_ids
    )

    return BatchListPage(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=has_more,
        channels_by_batch=channels_by_batch,
    )


def _normalise_batch_status_filter(value: object) -> BatchStatus | None:
    """Return the :class:`BatchStatus` matching ``value`` or ``None``.

    Same contract as :func:`_normalise_channel_filter` /
    :func:`_normalise_status_filter`: ``None`` / empty string
    means "no filter"; an unknown value raises
    :class:`InvalidListFilterError` so the route layer can
    surface a 422 instead of silently returning an empty
    list.
    """
    if value is None or value == "":
        return None
    if isinstance(value, BatchStatus):
        return value
    if isinstance(value, str):
        try:
            return BatchStatus(value)
        except ValueError as exc:
            raise InvalidListFilterError(
                "invalid_batch_status",
                f"unknown batch status filter: {value!r}",
            ) from exc
    raise InvalidListFilterError(
        "invalid_batch_status",
        "batch status filter must be a string",
    )


async def get_message_status(
    session: AsyncSession,
    *,
    client: Client,
    message_id: str,
    settings: Settings | None = None,
) -> Message:
    """Return the current :class:`Message` row, refreshing the
    provider status when the row is still in flight.

    A row that belongs to a different client is reported as
    :class:`MessageNotFoundError` (the same response an
    unauthenticated caller would see) so the existence of
    another tenant's message is not leaked.
    """
    cfg = settings or get_settings()
    if not isinstance(message_id, str) or not message_id:
        raise MessageNotFoundError("message_not_found", "message id is required")
    stmt = select(Message).where(Message.id == message_id)
    result = await session.execute(stmt)
    message = result.scalar_one_or_none()
    if message is None or message.client_id != client.id:
        raise MessageNotFoundError("message_not_found", "message does not exist")

    # No provider status to refresh if we never reached the
    # upstream or if the upstream already confirmed the
    # terminal state. This keeps the hot path cheap (one DB
    # read) for the common "I just got a delivery receipt"
    # case.
    if message.status in {MessageStatus.DELIVERED, MessageStatus.FAILED}:
        return message
    if not message.provider_msg_id:
        return message

    provider = get_provider(message.channel, settings=cfg)
    try:
        raw = await provider.get_status(message.provider_msg_id)
    except ProviderError:
        # If the upstream is down we return the cached row
        # rather than failing the whole call – the platform's
        # contract is "best-effort status refresh", not
        # "guaranteed up-to-date".
        return message
    new_status = _map_provider_status(raw)
    if new_status != message.status:
        message.status = new_status
        await session.commit()
        await session.refresh(message)
    return message


# ---------------------------------------------------------------------------
# History listing
# ---------------------------------------------------------------------------


def _normalise_channel_filter(value: object) -> Channel | None:
    """Return the :class:`Channel` matching ``value`` or ``None``.

    ``None`` means "no filter" (the SQL query omits the WHERE
    clause). The function accepts a string (the typical case
    from the API edge) or a :class:`Channel` instance (handy
    for unit tests that want to skip the str → enum dance).
    An unknown value is reported as
    :class:`InvalidListFilterError` so the caller surfaces a
    422 instead of silently returning an empty list.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Channel):
        return value
    if isinstance(value, str):
        try:
            return Channel(value)
        except ValueError as exc:
            raise InvalidListFilterError(
                "invalid_channel",
                f"unknown channel filter: {value!r}",
            ) from exc
    raise InvalidListFilterError(
        "invalid_channel",
        "channel filter must be a string",
    )


def _normalise_status_filter(value: object) -> MessageStatus | None:
    """Return the :class:`MessageStatus` matching ``value`` or ``None``.

    Same contract as :func:`_normalise_channel_filter`: a
    string-typed enum (``"sent"`` / ``"failed"`` / …) is
    converted, an unknown value raises
    :class:`InvalidListFilterError`. A non-string,
    non-enum value is rejected for symmetry.
    """
    if value is None or value == "":
        return None
    if isinstance(value, MessageStatus):
        return value
    if isinstance(value, str):
        try:
            return MessageStatus(value)
        except ValueError as exc:
            raise InvalidListFilterError(
                "invalid_status",
                f"unknown status filter: {value!r}",
            ) from exc
    raise InvalidListFilterError(
        "invalid_status",
        "status filter must be a string",
    )


def _coerce_int(value: object, *, field: str, minimum: int) -> int:
    """Return ``value`` clamped to ``[minimum, infinity)``.

    Used for ``limit`` / ``offset`` query parameters that the
    caller might submit as strings (the FastAPI ``Query``
    annotation does not coerce ``int`` when the input comes
    from a manually-built request) or that a dashboard
    could send as ``0`` / negative. The function rejects
    anything that is not an ``int`` so a typo (e.g. a stray
    string) does not silently degrade into a 200 with
    empty results.
    """
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int`` in Python; we
        # treat a ``True``/``False`` here as a caller bug and
        # surface it as 422 rather than coercing.
        raise InvalidListFilterError(
            f"invalid_{field}",
            f"{field} must be a positive integer",
        )
    coerced: int
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, str):
        try:
            coerced = int(value)
        except ValueError as exc:
            raise InvalidListFilterError(
                f"invalid_{field}",
                f"{field} must be a positive integer",
            ) from exc
    else:
        raise InvalidListFilterError(
            f"invalid_{field}",
            f"{field} must be a positive integer",
        )
    if coerced < minimum:
        raise InvalidListFilterError(
            f"invalid_{field}",
            f"{field} must be >= {minimum}",
        )
    return coerced


async def list_messages(
    session: AsyncSession,
    *,
    client: Client,
    channel: object | None = None,
    status: object | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    settings: Settings | None = None,
) -> MessageListPage:
    """Return a paginated slice of the customer's message history.

    The endpoint backs the dashboard's "Historial" view: a
    chronological list of every message the authenticated
    customer has dispatched, optionally narrowed down by
    channel, status, and a date range. The result is ordered
    by ``created_at`` descending so the most recent message
    is the first row of the response – the dashboard does
    not have to sort client-side.

    Filtering:

    - ``channel`` and ``status`` accept either a string
      (``"sms"`` / ``"sent"`` …) or the matching enum
      member. An unknown value is a 422.
    - ``since`` and ``until`` are inclusive lower / upper
      bounds on :attr:`Message.created_at`. They default to
      "no bound" (a NULL comparison is omitted from the
      WHERE clause) so the dashboard can fetch the full
      history with no parameters.
    - ``limit`` is clamped to :data:`_LIST_HARD_LIMIT` so a
      curious operator cannot ask the API for a million
      rows in one call.
    - ``offset`` is the standard SQL offset; the platform
      does not yet expose cursor-based pagination (the
      dashboard can switch to "cargar más" by bumping the
      offset by ``limit``).

    The function **does not** talk to the upstream provider:
    a history list is a database-only operation, and the
    delivery status has already been reconciled by the time
    the row is in the table (the worker / webhook loop keeps
    it fresh).
    """
    if not isinstance(client, Client):
        raise InvalidListFilterError("invalid_client", "client is required")
    # ``settings`` is accepted for symmetry with the other
    # service entry points (so a caller can pass a custom
    # ``Settings`` instance in tests) but the current
    # implementation does not read any value from it – the
    # list is a pure database query. Keeping the parameter
    # in the signature means a future change (e.g. a config
    # flag that caps the page size per customer) does not
    # have to break the contract.
    _ = settings
    where: list[ColumnElement[bool]]
    where, channel_filter, status_filter = _build_history_filters(
        client=client,
        channel=channel,
        status=status,
        since=since,
        until=until,
    )
    limit = _coerce_int(limit, field="limit", minimum=1)
    offset = _coerce_int(offset, field="offset", minimum=0)
    if limit > _LIST_HARD_LIMIT:
        limit = _LIST_HARD_LIMIT

    from sqlalchemy import and_, func

    list_stmt = (
        select(Message)
        .where(and_(*where))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
        .offset(offset)
    )
    count_stmt = select(func.count(Message.id)).where(and_(*where))

    list_result = await session.execute(list_stmt)
    items = list(list_result.scalars().all())

    count_result = await session.execute(count_stmt)
    total = int(count_result.scalar_one() or 0)

    # ``has_more`` is derived from the count (not by querying
    # the next page) so the dashboard can render the "cargar
    # más" button even when the current page fills the
    # window. The cost is one extra ``SELECT COUNT(*)`` per
    # call; on a composite (client_id, created_at) index
    # that is the same cost as the page query itself.
    has_more = (offset + len(items)) < total

    return MessageListPage(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


# ---------------------------------------------------------------------------
# History export (CSV)
# ---------------------------------------------------------------------------
#
# The "Historial y consumo" dashboard lets the customer download
# their history as a CSV file (PRD user story #18: "descargar un
# reporte mensual con detalle de mensajes, para auditoría"). The
# implementation is split in two: a thin database reader that
# returns every row matching the filter, and a pure function that
# projects those rows onto a CSV byte string. The route handler
# stitches the two together so the service layer is the only
# place that knows the export's hard cap and the column shape.


def _build_history_filters(
    *,
    client: Client,
    channel: object | None,
    status: object | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[list[ColumnElement[bool]], Channel | None, MessageStatus | None]:
    """Validate + normalise the history filters shared by the
    list and the export endpoints.

    Returns the SQLAlchemy ``WHERE`` clause plus the typed
    enum values so the caller can use them in the COUNT query
    without having to re-parse the raw input. The function
    raises :class:`InvalidListFilterError` on any malformed
    input (unknown channel, inverted date range, …) so both
    endpoints surface the same 422 contract.
    """
    if not isinstance(client, Client):
        raise InvalidListFilterError("invalid_client", "client is required")
    if since is not None and until is not None and since > until:
        raise InvalidListFilterError(
            "invalid_date_range",
            "since must be earlier than or equal to until",
        )
    channel_filter = _normalise_channel_filter(channel)
    status_filter = _normalise_status_filter(status)

    where: list[ColumnElement[bool]] = [Message.client_id == client.id]
    if channel_filter is not None:
        where.append(Message.channel == channel_filter)
    if status_filter is not None:
        where.append(Message.status == status_filter)
    if since is not None:
        where.append(Message.created_at >= since)
    if until is not None:
        where.append(Message.created_at <= until)
    return where, channel_filter, status_filter


async def iter_messages_for_export(
    session: AsyncSession,
    *,
    client: Client,
    channel: object | None = None,
    status: object | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    settings: Settings | None = None,
) -> MessageExport:
    """Return every message matching the filter, capped at
    :data:`_EXPORT_HARD_LIMIT`.

    The function is the backend for the
    ``GET /v1/messages/export`` CSV endpoint: it returns
    the full result set (newest first) so a flat-file export
    is a single round-trip. The ``truncated`` flag tells the
    caller whether the cap was reached; the route handler
    forwards it as a response header so a script can detect
    a partial export without re-running the count.

    The query mirrors :func:`list_messages` (same WHERE
    shape, same ordering) so the two endpoints never disagree
    on what "the history" means.
    """
    _ = settings  # kept for symmetry with the rest of the service
    where: list[ColumnElement[bool]]
    where, _, _ = _build_history_filters(
        client=client,
        channel=channel,
        status=status,
        since=since,
        until=until,
    )

    from sqlalchemy import and_, func

    # Fetch ``_EXPORT_HARD_LIMIT + 1`` rows so we can tell
    # whether the result was capped without a second query
    # against the table.
    list_stmt = (
        select(Message)
        .where(and_(*where))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(_EXPORT_HARD_LIMIT + 1)
    )
    count_stmt = select(func.count(Message.id)).where(and_(*where))

    list_result = await session.execute(list_stmt)
    raw_items = list(list_result.scalars().all())
    truncated = len(raw_items) > _EXPORT_HARD_LIMIT
    if truncated:
        raw_items = raw_items[:_EXPORT_HARD_LIMIT]

    count_result = await session.execute(count_stmt)
    total = int(count_result.scalar_one() or 0)

    return MessageExport(items=raw_items, total=total, truncated=truncated)


# CSV column order for the export. The order is part of the
# public contract: a script that ingests ``mensajes-YYYY-MM.csv``
# expects the same column layout across releases. Append new
# columns at the end so a parser written against the v1 layout
# keeps working.
_CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "created_at",
    "channel",
    "status",
    "to_number",
    "body",
    "provider",
    "provider_msg_id",
    "error_code",
    "error_message",
    "cost_clp",
    "fee_clp",
)


def _message_to_csv_row(message: Message) -> dict[str, str]:
    """Project a :class:`Message` row onto a CSV-friendly dict.

    Datetimes are serialised in the ISO-8601 form the rest of
    the platform uses (``2026-06-15T10:00:00+00:00``) so the
    CSV stays a flat file: a spreadsheet can sort / filter
    by the column without having to first parse a localised
    format. ``None`` values are written as empty strings
    (the standard csv module handles this), not the literal
    ``"None"`` that ``str(None)`` would produce.
    """
    created_at = message.created_at
    iso = created_at.isoformat() if created_at is not None else ""
    channel_value = (
        message.channel.value
        if isinstance(message.channel, Channel)
        else message.channel
    )
    status_value = (
        message.status.value
        if isinstance(message.status, MessageStatus)
        else message.status
    )
    return {
        "id": message.id,
        "created_at": iso,
        "channel": str(channel_value),
        "status": str(status_value),
        "to_number": message.to_number,
        "body": message.body,
        "provider": message.provider,
        "provider_msg_id": message.provider_msg_id or "",
        "error_code": message.error_code or "",
        "error_message": message.error_message or "",
        "cost_clp": str(message.cost_clp),
        "fee_clp": str(message.fee_clp),
    }


def render_messages_csv(messages: Iterable[Message]) -> str:
    """Render an iterable of :class:`Message` rows as a CSV string.

    The function is a pure helper (no database, no FastAPI
    dependency) so the unit tests can assert on the exact
    wire format without spinning up a session. The
    :class:`csv.DictWriter` is configured for ``utf-8`` with
    explicit quoting so an apostrophe in a message body does
    not break the row layout. The output uses ``\\r\\n`` line
    endings (the RFC-4180 default) so it loads cleanly into
    Excel on both Windows and macOS.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(_CSV_COLUMNS),
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
    )
    writer.writeheader()
    for message in messages:
        writer.writerow(_message_to_csv_row(message))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Daily usage aggregation
# ---------------------------------------------------------------------------
#
# The "Historial y consumo" dashboard renders a bar chart of
# daily message volume (PRD user story #8). The chart is a
# simple per-day count; a per-channel breakdown is included
# in the same response so the renderer can colour the stacked
# bars without a second round-trip.


# Default window for ``daily_message_counts`` when the caller
# does not pin a ``since`` value. 31 days covers the longest
# month in the year and the previous month when the dashboard
# is opened on the 1st.
_DAILY_DEFAULT_DAYS = 31

# Hard cap on the date range a single call can cover. A
# curious operator could otherwise ask the API for the entire
# history and force a full-table aggregation; the cap is the
# same number the chart's "todo el año" view would need.
_DAILY_HARD_LIMIT_DAYS = 366


def _daily_default_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return ``[since, until]`` for the default daily window.

    The window covers the trailing 31 days, ending at the
    current instant. Tests pass an explicit ``now`` so the
    result is deterministic; production relies on the
    ``datetime.now(tz=UTC)`` default.

    The function is a thin wrapper so the route layer and the
    tests share the same definition of "default window".
    """
    anchor = now or datetime.now(tz=UTC)
    start_day = anchor.date() - timedelta(days=_DAILY_DEFAULT_DAYS - 1)
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=UTC)
    return start, anchor


async def daily_message_counts(
    session: AsyncSession,
    *,
    client: Client,
    since: datetime | None = None,
    until: datetime | None = None,
    channel: object | None = None,
    settings: Settings | None = None,
) -> DailyUsagePage:
    """Aggregate the customer's messages per day, per channel.

    The function backs the dashboard's "gráfico de barras"
    (issue #6 acceptance criteria). It returns a
    :class:`DailyUsagePage` whose ``items`` field is a flat
    list of :class:`DailyMessageCount` rows; the route layer
    forwards the list to the wire verbatim. Days with no
    traffic are **not** filled in – the dashboard's chart
    component handles the zero-filling so a sparse history
    does not inflate the response.

    Filtering mirrors :func:`list_messages`:

    - ``since`` / ``until`` bound ``created_at`` (inclusive).
      An inverted range is a 422; the function does not
      silently return an empty list.
    - ``channel`` accepts either a :class:`Channel` enum
      member or the matching string. An unknown value is a
      422; the dashboard never sends one, but the validator
      guards against a future caller mistake.

    The function also returns the resolved window so the
    route layer can echo ``since`` / ``until`` in the
    response – the dashboard needs the value to draw the
    chart axis labels.

    Cross-tenant access is blocked by the ``client_id``
    WHERE clause – the function never returns another
    customer's rows.
    """
    if not isinstance(client, Client):
        raise InvalidListFilterError("invalid_client", "client is required")
    _ = settings  # kept for symmetry with the rest of the service
    if since is not None and until is not None and since > until:
        raise InvalidListFilterError(
            "invalid_date_range",
            "since must be earlier than or equal to until",
        )
    if since is None and until is None:
        since, until = _daily_default_range()
    elif since is None:
        assert until is not None  # narrow the union for mypy
        since = until - timedelta(days=_DAILY_DEFAULT_DAYS - 1)
    elif until is None:
        assert since is not None  # narrow the union for mypy
        until = since + timedelta(days=_DAILY_DEFAULT_DAYS - 1)
    # ``since`` and ``until`` are non-None at this point:
    # every branch above either leaves the argument alone
    # (already non-None) or assigns a fresh value. The
    # explicit assertion is a safety net so a future
    # refactor that re-introduces a ``None`` branch fails
    # loudly here rather than in the SQL builder.
    assert since is not None and until is not None
    if (until - since).days > _DAILY_HARD_LIMIT_DAYS:
        raise InvalidListFilterError(
            "invalid_date_range",
            f"date range cannot exceed {_DAILY_HARD_LIMIT_DAYS} days",
        )
    channel_filter = _normalise_channel_filter(channel)

    where: list[ColumnElement[bool]] = [
        Message.client_id == client.id,
        Message.created_at >= since,
        Message.created_at <= until,
    ]
    if channel_filter is not None:
        where.append(Message.channel == channel_filter)

    # ``func.date`` is the cross-dialect way to truncate a
    # ``TIMESTAMP WITH TIME ZONE`` to the calendar day in
    # UTC. SQLite (the test backend) and PostgreSQL (the
    # production backend) both understand the syntax; the
    # function returns a ``DATE`` value that SQLAlchemy
    # hands back as a :class:`datetime.date`.
    day_expr = func.date(Message.created_at).label("day")
    stmt = (
        select(
            day_expr,
            Message.channel.label("channel"),
            func.count(Message.id).label("count"),
        )
        .where(and_(*where))
        .group_by(day_expr, Message.channel)
        .order_by(day_expr.asc(), Message.channel.asc())
    )
    result = await session.execute(stmt)
    rows = list(result.all())
    # ``_mapping`` returns the row as a typed mapping; the
    # alternative ``row.day`` / ``row.channel`` syntax
    # collides with the built-in :class:`Row` methods
    # (``.count``) at the type-checker level, so we read
    # the labelled columns through the mapping interface.
    items = [
        DailyMessageCount(
            day=_coerce_day(mapping["day"]),
            channel=str(mapping["channel"]),
            count=int(mapping["count"]),
        )
        for mapping in (row._mapping for row in rows)
    ]
    return DailyUsagePage(items=items, since=since, until=until)


# ---------------------------------------------------------------------------
# Status summary
# ---------------------------------------------------------------------------
#
# The "Historial y consumo" dashboard renders a "desglose por
# estado" card (delivered / failed / pending / queued / sent /
# unknown) above the history table so a customer can see at a
# glance how their traffic is doing (PRD user story #13: "ver
# el historial de mensajes enviados con sus estados, para
# debugging"). The endpoint is a thin aggregation over the
# same table the daily chart uses, but it does not slice by
# day – the dashboard wants a single ``total`` per status, not
# a per-day series.


# Default window for ``message_status_summary`` when the
# caller does not pin a ``since`` value. 31 days matches
# the daily chart's default so the two widgets describe the
# same period and the numbers are directly comparable.
_SUMMARY_DEFAULT_DAYS = 31

# Hard cap on the date range a single call can cover.
# Same number as the daily endpoint for symmetry.
_SUMMARY_HARD_LIMIT_DAYS = 366


def _summary_default_range(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return ``[since, until]`` for the default summary window.

    The window covers the trailing 31 days, ending at the
    current instant. Mirrors :func:`_daily_default_range` so
    the two widgets describe the same period by default.
    """
    anchor = now or datetime.now(tz=UTC)
    start_day = anchor.date() - timedelta(days=_SUMMARY_DEFAULT_DAYS - 1)
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=UTC)
    return start, anchor


async def message_status_summary(
    session: AsyncSession,
    *,
    client: Client,
    channel: object | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    settings: Settings | None = None,
) -> MessageStatusSummary:
    """Aggregate the customer's messages by status for the period.

    The function backs the dashboard's "desglose por estado"
    card. It returns a :class:`MessageStatusSummary` whose
    ``items`` field is a flat list of
    :class:`MessageStatusCount` rows (one per
    :class:`MessageStatus` value, zero-filled for statuses
    with no traffic) plus the headline ``total`` counter,
    the resolved ``since`` / ``until`` window, the summed
    ``cost_clp`` / ``fee_clp`` amounts and the
    ``delivered`` / ``failed`` / ``pending`` counters the
    widget surfaces in the headline cards.

    Filtering mirrors :func:`daily_message_counts`:

    - ``since`` / ``until`` bound ``created_at`` (inclusive).
      An inverted range is a 422; the function does not
      silently return an empty list.
    - ``channel`` accepts either a :class:`Channel` enum
      member or the matching string. An unknown value is a
      422; the dashboard never sends one.

    Cross-tenant access is blocked by the ``client_id``
    WHERE clause – the function never returns another
    customer's rows.
    """
    _ = settings  # kept for symmetry with the rest of the service
    if not isinstance(client, Client):
        raise InvalidListFilterError("invalid_client", "client is required")
    if since is not None and until is not None and since > until:
        raise InvalidListFilterError(
            "invalid_date_range",
            "since must be earlier than or equal to until",
        )
    if since is None and until is None:
        since, until = _summary_default_range()
    elif since is None:
        assert until is not None  # narrow the union for mypy
        since = until - timedelta(days=_SUMMARY_DEFAULT_DAYS - 1)
    elif until is None:
        assert since is not None  # narrow the union for mypy
        until = since + timedelta(days=_SUMMARY_DEFAULT_DAYS - 1)
    # ``since`` and ``until`` are non-None at this point: every
    # branch above either leaves the argument alone (already
    # non-None) or assigns a fresh value. The explicit assertion
    # is a safety net so a future refactor that re-introduces a
    # ``None`` branch fails loudly here rather than in the SQL
    # builder.
    assert since is not None and until is not None
    if (until - since).days > _SUMMARY_HARD_LIMIT_DAYS:
        raise InvalidListFilterError(
            "invalid_date_range",
            f"date range cannot exceed {_SUMMARY_HARD_LIMIT_DAYS} days",
        )
    channel_filter = _normalise_channel_filter(channel)

    where: list[ColumnElement[bool]] = [
        Message.client_id == client.id,
        Message.created_at >= since,
        Message.created_at <= until,
    ]
    if channel_filter is not None:
        where.append(Message.channel == channel_filter)

    # Single round-trip aggregation: ``GROUP BY status`` plus
    # the sum of the cost / fee columns. The dashboard does
    # not need a per-channel breakdown here – the per-channel
    # number lives on the daily chart – so a single
    # ``GROUP BY`` is enough.
    stmt = (
        select(
            Message.status.label("status"),
            func.count(Message.id).label("count"),
            func.coalesce(func.sum(Message.cost_clp), 0).label("cost_clp"),
            func.coalesce(func.sum(Message.fee_clp), 0).label("fee_clp"),
        )
        .where(and_(*where))
        .group_by(Message.status)
    )
    result = await session.execute(stmt)
    rows = list(result.all())

    # Zero-fill every status so the dashboard can iterate over
    # the response without having to special-case a missing
    # row. The order matches the order the dashboard renders
    # the breakdown (delivered first, then sent, then
    # in-flight, then failed / unknown).
    counts: dict[MessageStatus, int] = {status: 0 for status in MessageStatus}
    cost_total = 0
    fee_total = 0
    for row in rows:
        mapping = row._mapping
        # The ``MessageStatus`` enum value is the column type,
        # so the mapping returns the enum member directly;
        # we re-validate through :class:`MessageStatus` to
        # cover a future migration that drops the enum
        # (the aggregator would still return strings).
        raw_status = mapping["status"]
        if isinstance(raw_status, MessageStatus):
            status = raw_status
        else:
            try:
                status = MessageStatus(str(raw_status))
            except ValueError:
                # An unrecognised status is a data-quality
                # bug, not a customer error: surface it as
                # ``unknown`` so the dashboard still gets a
                # number for it.
                status = MessageStatus.UNKNOWN
        counts[status] = int(mapping["count"])
        cost_total += int(mapping["cost_clp"])
        fee_total += int(mapping["fee_clp"])

    # Build the per-status item list. The ordering is the
    # platform's lifecycle order (delivered → sent → queued
    # → pending → failed → unknown) so the dashboard renders
    # the bars in a stable, intuitive sequence.
    ordered_statuses: tuple[MessageStatus, ...] = (
        MessageStatus.DELIVERED,
        MessageStatus.SENT,
        MessageStatus.QUEUED,
        MessageStatus.PENDING,
        MessageStatus.FAILED,
        MessageStatus.UNKNOWN,
    )
    items = [
        MessageStatusCount(status=status, count=counts[status])
        for status in ordered_statuses
    ]
    total = sum(counts.values())
    return MessageStatusSummary(
        items=items,
        total=total,
        delivered=counts[MessageStatus.DELIVERED],
        failed=counts[MessageStatus.FAILED],
        pending=counts[MessageStatus.PENDING],
        cost_clp=cost_total,
        fee_clp=fee_total,
        since=since,
        until=until,
    )


def _coerce_day(value: object) -> date:
    """Normalise a ``GROUP BY date(...)`` result to a :class:`date`.

    SQLite (the test backend) returns the truncated value as a
    string; PostgreSQL (the production backend) returns a real
    :class:`datetime.date`. The function is the single point of
    normalisation so the route layer can rely on a stable type
    regardless of the active database engine.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise InvalidListFilterError(
                "invalid_date_range",
                f"unparseable day value: {value!r}",
            ) from exc
    raise InvalidListFilterError(
        "invalid_date_range",
        f"unexpected day value: {value!r}",
    )


# ---------------------------------------------------------------------------
# Batch completion webhook (issue #9)
# ---------------------------------------------------------------------------
#
# The PRD's batch-completion notification is opt-in: a
# customer that wants a "your campaign finished" push
# registers a ``webhook_url`` on the ``POST /v1/messages/batch``
# request and the platform fires one signed POST once the
# batch reaches a terminal state (``completed`` or
# ``failed``).
#
# The notification piggybacks on the same
# :class:`~app.services.webhook_delivery.WebhookDeliveryClient`
# the per-message receipts use, so the retry / timeout knobs
# are owned by :class:`Settings` and a flaky customer
# endpoint cannot consume the worker's quota indefinitely.
# The signing key is the one-time ``webhook_secret`` the
# :func:`send_batch` call minted (or the customer-supplied
# value) so the receiver can verify the body out-of-band.


# Event name the platform advertises on the completion
# POST. Mirrors the same dotted-lowercase convention the
# per-message ``WebhookEvent`` values use so a single
# receiver-side switch statement can branch on the
# ``X-Mgw-Event`` header.
_BATCH_COMPLETED_EVENT = "batch.completed"

# Header names the delivery helper sets on the outbound
# completion POST. Mirrors the values
# :mod:`app.services.webhooks` exposes for the per-message
# receipts so a receiver that already speaks the per-message
# protocol does not have to add a second branch for the
# batch events.
_BATCH_SIGNATURE_HEADER = "X-Mgw-Signature"
_BATCH_EVENT_HEADER = "X-Mgw-Event"
_BATCH_DELIVERY_ID_HEADER = "X-Mgw-Delivery"


def _build_completion_payload(
    *,
    batch: Batch,
    summary: BatchSummary,
) -> dict[str, object]:
    """Project a finished :class:`Batch` row onto the JSON
    the delivery helper POSTs to the customer's endpoint.

    The shape is intentionally compact: only the fields a
    backend system needs to update its own record of the
    campaign (status, counters, totals). The full batch
    row stays on the platform side; the receiver can always
    poll ``GET /v1/messages/batch/{batch_id}`` for the
    canonical state.
    """
    channels = [
        {
            "channel": (
                channel.channel.value
                if hasattr(channel.channel, "value")
                else str(channel.channel)
            ),
            "count": channel.count,
            "pending": channel.pending,
            "delivered": channel.delivered,
            "failed": channel.failed,
            "succeeded": channel.succeeded,
            "total_cost_clp": channel.total_cost_clp,
            "total_fee_clp": channel.total_fee_clp,
            "total_amount_clp": channel.total_amount_clp,
        }
        for channel in summary.channels
    ]
    completed_at = batch.completed_at
    status_value = (
        batch.status.value
        if hasattr(batch.status, "value")
        else str(batch.status)
    )
    return {
        "id": batch.id,
        "client_id": batch.client_id,
        "name": batch.name,
        "status": status_value,
        "total": summary.total,
        "pending": summary.pending,
        "delivered": summary.delivered,
        "failed": summary.failed,
        "succeeded": summary.succeeded,
        "total_cost_clp": summary.total_cost_clp,
        "total_fee_clp": summary.total_fee_clp,
        "total_amount_clp": summary.total_amount_clp,
        "channels": channels,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
    }


async def fire_batch_completion_webhook(
    *,
    batch: Batch,
    summary: BatchSummary,
    settings: Settings | None = None,
    delivery_client: _WebhookDeliveryLike | None = None,
) -> WebhookDeliveryResult | None:
    """POST the batch-completion summary to the customer's
    ``webhook_url`` and return the per-call outcome.

    Returns ``None`` when the batch was not configured with
    a completion webhook (the common case for customers
    who only ever poll ``GET /v1/messages/batch/{id}``).
    The function never raises on a transport error – a
    failing customer endpoint must not crash the caller
    (the route layer wires this through
    :class:`fastapi.BackgroundTasks`, so a raised
    exception would only show up in the server log
    without any way for the customer to see it).

    The signing scheme mirrors the per-message
    :func:`app.services.webhooks.sign_payload` contract:
    HMAC-SHA256 over the JSON body, with the
    one-time ``webhook_secret`` the
    :func:`send_batch` call minted (or the
    customer-supplied value) as the key. The digest is
    carried in the ``X-Mgw-Signature`` header so the
    receiver can verify the body before parsing it.

    ``delivery_client`` is an injectable seam so unit
    tests can swap the in-memory fake
    :class:`FakeWebhookDeliveryClient` in without
    monkeypatching the production
    :class:`WebhookDeliveryClient`.
    """
    if not batch.webhook_url:
        return None
    cfg = settings or get_settings()
    # ``batch.webhook_secret`` was populated by
    # :func:`send_batch` (either from the caller or
    # minted as a one-time value). A ``None`` at this
    # point is a database corruption – the row claims
    # to have a webhook but the secret is missing. We
    # log the anomaly and skip the POST rather than
    # fire an unsigned request the receiver cannot
    # verify.
    if not batch.webhook_secret:
        logger = get_logger(__name__)
        logger.warning(
            "batch_webhook_missing_secret",
            extra={"batch_id": batch.id, "client_id": batch.client_id},
        )
        return None

    # Local import keeps the top-of-file import block
    # free of the ``httpx``-based delivery client and
    # avoids a circular dependency with the webhooks
    # module.
    import json as _json

    from app.services.webhook_delivery import (
        WebhookDeliveryClient,
    )
    from app.services.webhooks import sign_payload

    payload = _build_completion_payload(batch=batch, summary=summary)
    body = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    client = delivery_client or WebhookDeliveryClient(
        timeout_seconds=cfg.batch_webhook_timeout_seconds,
        max_attempts=cfg.batch_webhook_max_delivery_attempts,
    )
    signature = sign_payload(body=body, secret=batch.webhook_secret)
    delivery = await client.deliver(
        url=batch.webhook_url,
        body=body,
        headers={
            _BATCH_SIGNATURE_HEADER: signature,
            _BATCH_EVENT_HEADER: _BATCH_COMPLETED_EVENT,
            _BATCH_DELIVERY_ID_HEADER: batch.id,
            "Content-Type": "application/json",
        },
    )
    return delivery


__all__ = (
    "BatchListPage",
    "BatchNotFoundError",
    "BatchOutcome",
    "BatchRateLimitError",
    "BatchSummary",
    "BatchTooLargeError",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_LIST_LIMIT",
    "DailyMessageCount",
    "DailyUsagePage",
    "InvalidListFilterError",
    "InvalidMessageError",
    "MessageExport",
    "MessageListPage",
    "MessageNotFoundError",
    "MessageStatusCount",
    "MessageStatusSummary",
    "MessagingError",
    "SendOutcome",
    "compute_message_cost",
    "daily_message_counts",
    "fire_batch_completion_webhook",
    "get_batch",
    "get_message_status",
    "iter_messages_for_export",
    "list_batches",
    "list_messages",
    "message_status_summary",
    "render_messages_csv",
    "send_batch",
    "send_message",
)
