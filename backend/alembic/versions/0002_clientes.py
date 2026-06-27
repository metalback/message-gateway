"""clientes table – auth & registration

Revision ID: 0002_clientes
Revises: 0001_initial
Create Date: 2026-06-26 00:00:00.000000

Creates the ``clientes`` table that backs the auth / registration
feature (issue #3). The shape mirrors the model declared in
``app/models/client.py``:

- ``id``            – UUIDv4 primary key, generated client-side.
- ``name``          – display label (person or company name).
- ``email``         – unique, used as the dashboard login id.
- ``rut``           – Chilean tax id, normalised to ``body-dv``.
- ``password_hash`` – bcrypt digest of the dashboard password.
- ``api_key_hash``  – bcrypt digest of the API key (the plain key
                      is never stored).
- ``api_key_last4`` – last 4 chars of the plain API key, kept so
                      the dashboard can render an identifier
                      without ever being able to reconstruct the
                      full secret.
- ``plan`` / ``status`` – commercial lifecycle enums stored as
                           ``String`` for forward-compatibility.
- ``created_at`` / ``updated_at`` – server-side timestamps.

The downgrade drops the whole table. There is no data to
preserve: a fresh ``alembic upgrade head`` against a clean
database will recreate it identically.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_clientes"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``clientes`` table."""
    op.create_table(
        "clientes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("rut", sa.String(length=12), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("api_key_hash", sa.String(length=255), nullable=False),
        sa.Column("api_key_last4", sa.String(length=4), nullable=False),
        sa.Column("plan", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        # Uniqueness constraints – email and RUT are the two
        # natural identifiers a customer might use to look up
        # their own account, and the dashboard enforces "one
        # account per email / RUT" at registration time.
        sa.UniqueConstraint("email", name="uq_clientes_email"),
        sa.UniqueConstraint("rut", name="uq_clientes_rut"),
    )
    op.create_index("ix_clientes_email", "clientes", ["email"], unique=True)
    op.create_index("ix_clientes_rut", "clientes", ["rut"], unique=True)
    # ``api_key_hash`` is the lookup key for the API key auth
    # dependency, so it must be indexed even though it is not a
    # uniqueness target (two clients would never share a hash
    # in practice, but bcrypt's salt would make the equality
    # check a happy accident rather than a contract).
    op.create_index("ix_clientes_api_key_hash", "clientes", ["api_key_hash"])


def downgrade() -> None:
    """Drop the ``clientes`` table."""
    op.drop_index("ix_clientes_api_key_hash", table_name="clientes")
    op.drop_index("ix_clientes_rut", table_name="clientes")
    op.drop_index("ix_clientes_email", table_name="clientes")
    op.drop_table("clientes")
