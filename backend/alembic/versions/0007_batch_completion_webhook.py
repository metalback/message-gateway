"""lotes webhook_url / webhook_secret – batch completion webhook (issue #9)

Revision ID: 0007_batch_completion_webhook
Revises: 0006_batch_cost_rollup
Create Date: 2026-06-27 00:00:00.000000

Closes out the last open acceptance criterion from issue #9
("Webhook de batch completion funciona") by giving the
``lotes`` table the two columns the platform needs to
fire a one-shot completion notification once a batch
reaches a terminal state.

Shape of the new columns:

- ``webhook_url``   – optional ``https://`` endpoint the
                       platform POSTs a JSON summary to when
                       the batch transitions to ``completed``
                       (or ``failed``). ``NULL`` means
                       "no webhook configured", which is the
                       default for the legacy code path that
                       just polls through
                       ``GET /v1/messages/batch/{id}``.
- ``webhook_secret`` – optional HMAC-SHA256 secret the
                       platform uses to sign the outbound
                       POST. When the caller omits the value
                       on ``POST /v1/messages/batch`` the
                       service layer generates a one-time
                       secret (32 bytes of CSPRNG entropy,
                       hex-encoded) and returns it in the
                       response – the same flow the API-key
                       / webhook-secret onboarding uses. The
                       column is the canonical record of the
                       secret so a future ``POST /batch``
                       retry can re-fire the webhook without
                       a second secret minting round.

Both columns are nullable. The completion webhook is
opt-in: a batch the customer submitted before this
migration will simply not have a ``webhook_url`` and the
service layer treats that as "skip the completion POST".

The downgrade drops the two columns; a downgrade is
intentionally destructive (a freshly-upgraded deployment
can no longer deliver the completion webhook, but the
per-message webhooks are unaffected).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_batch_completion_webhook"
# Builds on the cost-rollup migration (0006) – the most
# recent in the linear history this branch follows.
down_revision: Union[str, Sequence[str], None] = "0006_batch_cost_rollup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the batch-completion-webhook columns to ``lotes``."""
    op.add_column(
        "lotes",
        sa.Column("webhook_url", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "lotes",
        sa.Column("webhook_secret", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    """Drop the batch-completion-webhook columns from ``lotes``."""
    op.drop_column("lotes", "webhook_secret")
    op.drop_column("lotes", "webhook_url")
