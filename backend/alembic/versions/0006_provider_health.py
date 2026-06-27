"""provider_config + routing_log – automatic provider failover (issue #11)

Revision ID: 0006_provider_health
Revises: 0005_lotes_mensajes
Create Date: 2026-06-27 00:00:00.000000

Adds the two tables that back the "Fallback Automático de
Proveedores" feature (issue #11):

- ``provider_config`` – persisted configuration and
  runtime health snapshot for each provider the
  platform knows about. The periodic health-check
  worker writes ``health_status``,
  ``last_health_check``,
  ``consecutive_failures`` /
  ``consecutive_successes`` and ``last_latency_ms``
  on every probe. The admin dashboard reads the
  same columns to render the "estado de
  proveedores" traffic-light card.

- ``routing_log``     – one row per provider attempt
  the messaging service made while dispatching a
  message. The chain can produce ``N`` rows per
  message (one per upstream that was tried); the
  admin dashboard aggregates them for the "intentos
  por proveedor" chart and groups by ``message_id``
  to render the per-message trace.

The migration also declares the indexes the
admin / dashboard queries actually use:

- ``ix_provider_config_active_channel`` – the
  registry's "list active providers for channel X"
  query.
- ``ix_routing_log_provider_attempted_at``  – the
  dashboard's "latencia promedio por proveedor en
  las últimas 24h" query.
- ``ix_routing_log_message_id`` /
  ``ix_routing_log_provider_attempted`` /
  ``ix_routing_log_attempted_at`` – already
  declared on the column ``index=True`` flags, but
  repeated here for clarity so a reviewer can read
  the migration top-to-bottom without cross-
  referencing the model.

The downgrade drops both tables. There is no data
to preserve: a fresh ``alembic upgrade head`` against
a clean database will recreate both identically.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_provider_health"
# Chain off the most-recent migration in the branch
# that already carries the message-sending surface
# (``0005_lotes_mensajes``) so a deployment that
# applied every previous migration in order lands
# on this one as the next step. The other 0005
# branch (``0005_admin_role_and_markup``) is a
# sibling; Alembic resolves the chain by the
# declared ``down_revision`` so a deployment that
# took the admin path first will still find this
# migration through the same downstream link.
down_revision: Union[str, Sequence[str], None] = "0005_lotes_mensajes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``provider_config`` and ``routing_log`` tables."""
    # ----------------------------------------------------------------
    # provider_config
    # ----------------------------------------------------------------
    # Mirrors :class:`app.models.provider_config.ProviderConfig`. The
    # ``name`` column is unique so two rows for the same upstream
    # cannot race the health-check worker; the ``health_status``
    # default is ``"unknown"`` so a never-probed provider does not
    # silently count as healthy on the dashboard.
    op.create_table(
        "provider_config",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "channel",
            sa.String(length=20),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column(
            "health_status",
            sa.String(length=20),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "consecutive_successes",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_latency_ms", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Single-column indexes that the ORM ``index=True`` flags
    # imply. Declared explicitly here so the migration is
    # readable without cross-referencing the model.
    op.create_index("ix_provider_config_channel", "provider_config", ["channel"])
    op.create_index(
        "ix_provider_config_last_health_check",
        "provider_config",
        ["last_health_check"],
    )
    # Composite index on ``(active, channel)`` so the
    # registry's "list active providers for channel X" query
    # is a single lookup.
    op.create_index(
        "ix_provider_config_active_channel",
        "provider_config",
        ["active", "channel"],
    )

    # ----------------------------------------------------------------
    # routing_log
    # ----------------------------------------------------------------
    # Mirrors :class:`app.models.routing_log.RoutingLog`. The
    # ``message_id`` FK is declared nullable because the
    # health-check worker inserts a probe row without an
    # associated message.
    op.create_table(
        "routing_log",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "message_id",
            sa.String(length=36),
            sa.ForeignKey("mensajes.id"),
            nullable=True,
        ),
        sa.Column("provider_attempted", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Single-column indexes for the per-message trace and
    # the per-provider aggregate.
    op.create_index("ix_routing_log_message_id", "routing_log", ["message_id"])
    op.create_index(
        "ix_routing_log_provider_attempted",
        "routing_log",
        ["provider_attempted"],
    )
    op.create_index("ix_routing_log_attempted_at", "routing_log", ["attempted_at"])
    # Composite index on ``(provider_attempted, attempted_at)``
    # so the dashboard's "latencia promedio por proveedor en
    # las últimas 24h" query is a single lookup without a
    # full table scan.
    op.create_index(
        "ix_routing_log_provider_attempted_at",
        "routing_log",
        ["provider_attempted", "attempted_at"],
    )


def downgrade() -> None:
    """Drop the ``provider_config`` and ``routing_log`` tables."""
    # Drop the routing_log table first because the only
    # foreign key on it (``message_id`` -> ``mensajes.id``)
    # would otherwise block a downstream migration that
    # drops ``mensajes``. (No such migration is in the
    # current plan, but the order matches the FK
    # dependency.)
    op.drop_index("ix_routing_log_provider_attempted_at", table_name="routing_log")
    op.drop_index("ix_routing_log_attempted_at", table_name="routing_log")
    op.drop_index("ix_routing_log_provider_attempted", table_name="routing_log")
    op.drop_index("ix_routing_log_message_id", table_name="routing_log")
    op.drop_table("routing_log")

    op.drop_index("ix_provider_config_active_channel", table_name="provider_config")
    op.drop_index("ix_provider_config_last_health_check", table_name="provider_config")
    op.drop_index("ix_provider_config_channel", table_name="provider_config")
    op.drop_table("provider_config")
