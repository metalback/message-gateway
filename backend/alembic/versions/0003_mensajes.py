"""mensajes table – message-sending history

Revision ID: 0003_mensajes
Revises: 0002_clientes
Create Date: 2026-06-27 00:00:00.000000

Creates the ``mensajes`` table that backs the message-sending
feature (issue #4). The shape mirrors the model declared in
``app/models/message.py``:

- ``id``              – UUIDv4 primary key, generated client-side.
- ``client_id``       – FK to ``clientes.id`` (no cascade: a
                        suspended client keeps its history).
- ``provider``        – short name of the upstream provider that
                        accepted the message
                        (``meta_whatsapp`` / ``sms_aggregator`` …).
- ``channel``         – ``sms`` / ``whatsapp`` stored as a string
                        so a future release can introduce a new
                        channel without rewriting the column type.
- ``to_number``       – destination in the canonical ``+56…`` form.
- ``body``            – plain text payload.
- ``status``          – lifecycle state, stored as a string for
                        forward-compatibility.
- ``provider_msg_id`` – upstream identifier (set after the
                        provider accepts the message).
- ``error_code`` / ``error_message`` – populated when the
                        provider rejects the message.
- ``cost_clp`` / ``fee_clp`` – per-message billing breakdown in
                              CLP cents (``integer`` so the
                              database never has to deal with
                              floating-point currency).
- ``created_at`` / ``updated_at`` – server-side timestamps.

The downgrade drops the whole table. There is no data to
preserve: a fresh ``alembic upgrade head`` against a clean
database will recreate it identically.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_mensajes"
down_revision: Union[str, Sequence[str], None] = "0002_clientes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``mensajes`` table and its indexes."""
    op.create_table(
        "mensajes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(length=36),
            sa.ForeignKey("clientes.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("to_number", sa.String(length=20), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("provider_msg_id", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("cost_clp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fee_clp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Single-column indexes: the queries the message-sending
    # routes run most often are "list messages for client X",
    # "list messages by status" and "look up a provider message
    # id". The composite index below covers the "list my
    # recent messages" query.
    op.create_index("ix_mensajes_client_id", "mensajes", ["client_id"])
    op.create_index("ix_mensajes_channel", "mensajes", ["channel"])
    op.create_index("ix_mensajes_status", "mensajes", ["status"])
    op.create_index("ix_mensajes_to_number", "mensajes", ["to_number"])
    op.create_index(
        "ix_mensajes_provider_msg_id", "mensajes", ["provider_msg_id"], unique=False
    )
    op.create_index(
        "ix_mensajes_client_created",
        "mensajes",
        ["client_id", "created_at"],
    )


def downgrade() -> None:
    """Drop the ``mensajes`` table and its indexes."""
    op.drop_index("ix_mensajes_client_created", table_name="mensajes")
    op.drop_index("ix_mensajes_provider_msg_id", table_name="mensajes")
    op.drop_index("ix_mensajes_to_number", table_name="mensajes")
    op.drop_index("ix_mensajes_status", table_name="mensajes")
    op.drop_index("ix_mensajes_channel", table_name="mensajes")
    op.drop_index("ix_mensajes_client_id", table_name="mensajes")
    op.drop_table("mensajes")
