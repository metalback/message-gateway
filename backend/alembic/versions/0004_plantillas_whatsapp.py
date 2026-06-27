"""plantillas_whatsapp table – WhatsApp template CRUD (issue #8)

Revision ID: 0004_plantillas_whatsapp
Revises: 0003_mensajes
Create Date: 2026-06-27 00:00:00.000000

Creates the ``plantillas_whatsapp`` table that backs the
WhatsApp-template CRUD surface (issue #8). The shape mirrors
the model declared in ``app/models/whatsapp_template.py``:

- ``id``               – UUIDv4 primary key, generated client-side.
- ``client_id``        – FK to ``clientes.id`` (no cascade: a
                         suspended customer keeps its template
                         history for audit).
- ``name``             – Meta-side template name (lowercase,
                         alphanumerics + underscore). Indexed
                         because the "list my templates" query
                         orders by ``(client_id, name)``.
- ``language``         – BCP-47 language tag (``"es_CL"`` …).
- ``category``         – ``utility`` / ``marketing`` /
                         ``authentication``. Stored as a string
                         to mirror the ``_StringEnum`` pattern
                         used in the other model files.
- ``status``           – lifecycle state (``draft`` /
                         ``pending`` / ``approved`` /
                         ``rejected``). Indexed because the
                         "list my pending templates" query the
                         dashboard uses after a bulk submission
                         filters on this column.
- ``meta_template_id`` – the ID Meta returns after the
                         template is submitted. ``None`` while
                         the row is in ``draft``. Indexed
                         because the future "send WhatsApp
                         template" flow looks the row up by
                         Meta's id.
- ``components``       – JSON-serialised list of Meta
                         components (header / body / footer /
                         buttons). ``Text`` so a
                         button-heavy template fits without
                         a column-type migration.
- ``rejection_reason`` – the reason Meta gave on rejection.
- ``description``      – free-form note the customer attaches
                         to the row.
- ``created_at`` / ``updated_at`` – server-side timestamps.
- ``submitted_at``     – when the platform forwarded the
                         template to Meta. ``None`` for
                         ``draft`` rows.

The downgrade drops the whole table. There is no data to
preserve: a fresh ``alembic upgrade head`` against a clean
database will recreate it identically.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_plantillas_whatsapp"
down_revision: Union[str, Sequence[str], None] = "0003_mensajes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``plantillas_whatsapp`` table and its indexes."""
    op.create_table(
        "plantillas_whatsapp",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(length=36),
            sa.ForeignKey("clientes.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="utility"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("meta_template_id", sa.String(length=128), nullable=True),
        sa.Column("rejection_reason", sa.String(length=1000), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("components", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Single-column indexes: the queries the templates routes
    # run most often are "list templates for client X",
    # "filter by status", "find a template by Meta id" and
    # "find a template by name". The composite indexes below
    # cover the dashboard's "list my recent templates" and
    # the unique-constraint lookups.
    op.create_index("ix_plantillas_whatsapp_client_id", "plantillas_whatsapp", ["client_id"])
    op.create_index("ix_plantillas_whatsapp_name", "plantillas_whatsapp", ["name"])
    op.create_index("ix_plantillas_whatsapp_status", "plantillas_whatsapp", ["status"])
    op.create_index(
        "ix_plantillas_whatsapp_meta_template_id",
        "plantillas_whatsapp",
        ["meta_template_id"],
    )
    op.create_index(
        "ix_plantillas_whatsapp_client_created",
        "plantillas_whatsapp",
        ["client_id", "created_at"],
    )
    # Meta rejects duplicate ``(name, language)`` pairs per
    # WABA; we mirror the constraint at the platform level so
    # a typo does not silently become a second uneditable
    # row before the upstream rejects the submission.
    op.create_index(
        "uq_plantillas_whatsapp_client_name_language",
        "plantillas_whatsapp",
        ["client_id", "name", "language"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the ``plantillas_whatsapp`` table."""
    op.drop_index(
        "uq_plantillas_whatsapp_client_name_language", table_name="plantillas_whatsapp"
    )
    op.drop_index("ix_plantillas_whatsapp_client_created", table_name="plantillas_whatsapp")
    op.drop_index("ix_plantillas_whatsapp_meta_template_id", table_name="plantillas_whatsapp")
    op.drop_index("ix_plantillas_whatsapp_status", table_name="plantillas_whatsapp")
    op.drop_index("ix_plantillas_whatsapp_name", table_name="plantillas_whatsapp")
    op.drop_index("ix_plantillas_whatsapp_client_id", table_name="plantillas_whatsapp")
    op.drop_table("plantillas_whatsapp")
