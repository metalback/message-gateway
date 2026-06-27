"""webhooks table – delivery-receipt subscriptions (issue #5)

Revision ID: 0004_webhooks
Revises: 0003_planes_facturas_pagos
Create Date: 2026-06-27 00:00:00.000000

Creates the ``webhooks`` table that backs the delivery-receipt
subscription feature (issue #5). The shape mirrors the model
declared in ``app/models/webhook.py``:

- ``id``        – UUIDv4 primary key, generated client-side.
- ``client_id`` – FK to ``clientes.id`` (no cascade: a
                  suspended client keeps its history).
- ``url``       – destination URL (https-only, validated at the
                  service layer). ``String(500)`` to match the
                  same ceiling the platform uses for
                  ``pagos.flow_redirect_url``.
- ``events``    – comma-separated list of event names
                  (``message.sent``, ``message.delivered``,
                  ``message.failed``). ``String(500)`` so a
                  future event-name growth is bounded but not
                  capped at three values.
- ``secret``    – HMAC-SHA256 key the platform uses to sign
                  every outbound receipt. ``String(128)`` (64
                  hex chars for a 32-byte secret + slack for a
                  future algorithm change).
- ``active``    – boolean soft-delete flag; flipped to
                  ``False`` to stop deliveries without losing
                  the row.
- ``created_at`` / ``updated_at`` – server-side timestamps.

The downgrade drops the whole table. There is no data to
preserve: a fresh ``alembic upgrade head`` against a clean
database will recreate it identically.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_webhooks"
down_revision: Union[str, Sequence[str], None] = "0003_planes_facturas_pagos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``webhooks`` table and its indexes."""
    op.create_table(
        "webhooks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(length=36),
            sa.ForeignKey("clientes.id"),
            nullable=False,
        ),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("events", sa.String(length=500), nullable=False),
        sa.Column("secret", sa.String(length=128), nullable=False),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Single-column index: ``client_id`` covers the "list
    # subscriptions for client X" query the GET endpoint
    # runs on every page load.
    op.create_index("ix_webhooks_client_id", "webhooks", ["client_id"])
    # Composite index that backs the delivery-receipt
    # fan-out query the worker runs on every message status
    # change: ``WHERE client_id = ? AND active = true``.
    op.create_index("ix_webhooks_client_active", "webhooks", ["client_id", "active"])


def downgrade() -> None:
    """Drop the ``webhooks`` table and its indexes."""
    op.drop_index("ix_webhooks_client_active", table_name="webhooks")
    op.drop_index("ix_webhooks_client_id", table_name="webhooks")
    op.drop_table("webhooks")
