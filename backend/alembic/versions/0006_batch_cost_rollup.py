"""lotes total_cost_clp / total_fee_clp – per-batch cost rollup (issue #9)

Revision ID: 0006_batch_cost_rollup
Revises: 0005_lotes_mensajes
Create Date: 2026-06-27 00:00:00.000000

Adds the per-batch cost / fee rollup columns the dashboard's
"Campañas" view needs to surface the campaign's total cost
without re-aggregating the underlying ``mensajes`` table on
every read:

- ``total_cost_clp`` – aggregated upstream cost (CLP cents)
                       across every message of the batch.
                       Mirrors ``Message.cost_clp`` and is
                       recomputed by
                       :func:`app.services.messaging._recompute_batch_counters`.
- ``total_fee_clp``  – aggregated platform markup (CLP cents)
                       across every message of the batch.
                       Mirrors ``Message.fee_clp``; ``cost + fee``
                       is the customer-facing total the dashboard
                       renders as "costo total de la campaña".

Both columns default to ``0`` and are non-nullable so the
pre-existing recompute / listing queries do not have to
special-case the missing-row case. The values are kept in sync
by the service layer; the migration only adds the schema, no
backfill is needed for a fresh deployment (existing rows
inherit the ``0`` default).

The downgrade drops both columns; a downgrade in production is
intentionally destructive (the campaign-level cost totals are
no longer surfaced, but the per-message rows still carry the
canonical cost / fee columns).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_batch_cost_rollup"
# Builds on the migration that introduced the ``lotes`` table
# (issue #9). The previous batch-related migration
# (``0005_lotes_mensajes``) is the most recent in the linear
# history this branch follows; an admin-dashboard branch
# (``0005_admin_role_and_markup``) ran in parallel off
# ``0004_plantillas_whatsapp`` and is unrelated to the batch
# rollup, so the down_revision is the batch one.
down_revision: Union[str, Sequence[str], None] = "0005_lotes_mensajes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the per-batch cost / fee rollup columns to ``lotes``."""
    op.add_column(
        "lotes",
        sa.Column(
            "total_cost_clp",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "lotes",
        sa.Column(
            "total_fee_clp",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    """Drop the per-batch cost / fee rollup columns from ``lotes``."""
    op.drop_column("lotes", "total_fee_clp")
    op.drop_column("lotes", "total_cost_clp")
