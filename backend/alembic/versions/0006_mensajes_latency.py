"""mensajes.latency_ms — per-provider dispatch latency (issue #10)

Revision ID: 0006_mensajes_latency
Revises: 0005_admin_role_and_markup
Create Date: 2026-06-27 00:00:00.000000

Adds the ``latency_ms`` column to the ``mensajes`` table so the
admin dashboard's "latencia promedio por provider" tile can
report the average round-trip time of a provider dispatch.

The migration is a single, additive ``op.add_column`` call so a
fresh ``alembic upgrade head`` against an empty database is a
no-op as far as the column is concerned (the platform will
start populating it as messages are sent). The downgrade
reverses the change without data preservation – latency is a
diagnostic metric and an operator losing it on a downgrade
is acceptable (the rest of the admin breakdown is still
populated from the cost / status columns).

Design notes:

- ``latency_ms`` is a ``Float`` because the wall-clock
  duration of an outbound HTTP call is inherently fractional
  (a "150 ms" average becomes ``150.0``, a "250.5 ms"
  average becomes ``250.5``). The frontend renders the
  value with one decimal of precision so the wire shape stays
  human-readable.
- The column is ``nullable=True`` because rows that were
  inserted before the column landed do not have a
  measurement – the per-provider average query treats
  ``NULL`` as "skip this row" via ``AVG(latency_ms)``'s
  standard SQL semantics.
- The column is not indexed on its own: the admin
  breakdown query groups by ``(provider, channel)`` and the
  existing composite indexes on ``mensajes`` are enough to
  keep the aggregation cheap. A dedicated index would only
  help a "find rows with the slowest latency" query, which
  is not in the current feature set.
- The default is ``NULL`` (no default at the SQL level) so
  older rows are not silently back-filled with a fake
  ``0.0`` value that would skew the average.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_mensajes_latency"
down_revision: Union[str, Sequence[str], None] = "0005_admin_role_and_markup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the nullable ``latency_ms`` column to ``mensajes``."""
    op.add_column(
        "mensajes",
        sa.Column("latency_ms", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    """Drop the ``latency_ms`` column.

    No data preservation: latency is a diagnostic metric and
    a downgrade that re-runs the column through migration
    history will start re-populating it from the new code
    path.
    """
    op.drop_column("mensajes", "latency_ms")
