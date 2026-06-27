"""lotes table + mensajes.batch_id – batch messaging API (issue #9)

Revision ID: 0005_lotes_mensajes
Revises: 0004_plantillas_whatsapp
Create Date: 2026-06-27 00:00:00.000000

Creates the ``lotes`` table that backs the batch-sending surface
exposed through ``POST /v1/messages/batch`` (issue #9) and adds
the optional ``batch_id`` foreign key on ``mensajes`` so every
message submitted as part of a batch can be grouped under it.

Shape of the new ``lotes`` table (mirrors
:class:`app.models.batch.Batch`):

- ``id``               – UUIDv4 primary key, generated client-side.
- ``client_id``        – FK to ``clientes.id`` (no cascade: a
                         suspended customer keeps its batch history
                         available for audit).
- ``name``             – optional human-readable label
                         (e.g. "Black Friday 2026"). Indexed
                         because the dashboard filters by
                         label on the "Campañas" view.
- ``total_count``      – number of items the caller submitted.
                         Frozen at creation time so the dashboard
                         can render "X of Y" without re-deriving
                         the denominator.
- ``pending_count``    – items still in ``pending`` / ``queued`` /
                         ``sent`` state.
- ``delivered_count``  – items in ``delivered`` state.
- ``failed_count``     – items in ``failed`` state.
- ``status``           – ``processing`` / ``completed`` /
                         ``failed`` (string so a future state
                         like ``"cancelled"`` can land without
                         rewriting the column type).
- ``created_at`` / ``updated_at`` – server-side timestamps.
- ``completed_at``     – set the first time ``status`` flips to
                         ``completed``.

The ``mensajes.batch_id`` column is ``NULLABLE`` so the single-
message path (``POST /v1/messages``) keeps working unchanged; the
batch path always sets it. The column is indexed because the
counter-recompute query that runs after every batch send groups
the ``mensajes`` table by ``batch_id``.

The downgrade drops the ``lotes`` table and the ``batch_id``
column on ``mensajes``. There is no data to preserve: a fresh
``alembic upgrade head`` against a clean database will recreate
both identically.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_lotes_mensajes"
# The webhooks / templates / billing migrations were each
# declared as parallel branches off ``0003_mensajes`` (because
# they were worked on in parallel branches). Issue #9 is the
# first migration that builds on top of every previous one, so
# the ``down_revision`` is the last migration declared in
# practice (alembic accepts the higher alphabetical / numeric
# id; the same convention ``0004_plantillas_whatsapp`` uses).
down_revision: Union[str, Sequence[str], None] = "0004_plantillas_whatsapp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``lotes`` table and add ``batch_id`` to ``mensajes``."""
    op.create_table(
        "lotes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(length=36),
            sa.ForeignKey("clientes.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="processing",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Single-column indexes: the queries the batch routes run
    # most often are "list batches for client X" and "filter by
    # status". The composite index below covers the dashboard's
    # "list my recent batches" query so the listing endpoint
    # never has to fall back to a full table scan.
    op.create_index("ix_lotes_client_id", "lotes", ["client_id"])
    op.create_index("ix_lotes_status", "lotes", ["status"])
    op.create_index("ix_lotes_client_created", "lotes", ["client_id", "created_at"])

    # Add the nullable ``batch_id`` foreign key on ``mensajes``.
    # The single-message path (``POST /v1/messages``) leaves it
    # ``NULL``; every item submitted through the batch endpoint
    # sets it. Indexed because the counter-recompute query
    # groups the ``mensajes`` table by ``batch_id``.
    op.add_column(
        "mensajes",
        sa.Column(
            "batch_id",
            sa.String(length=36),
            sa.ForeignKey("lotes.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_mensajes_batch_id", "mensajes", ["batch_id"])


def downgrade() -> None:
    """Drop the ``lotes`` table and the ``mensajes.batch_id`` column."""
    op.drop_index("ix_mensajes_batch_id", table_name="mensajes")
    op.drop_column("mensajes", "batch_id")
    op.drop_index("ix_lotes_client_created", table_name="lotes")
    op.drop_index("ix_lotes_status", table_name="lotes")
    op.drop_index("ix_lotes_client_id", table_name="lotes")
    op.drop_table("lotes")
