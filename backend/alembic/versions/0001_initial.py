"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-26 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    The scaffold ships no business tables yet; the message / client
    / template / billing tables land in follow-up tasks. We keep an
    empty upgrade so the ``alembic`` CLI can be exercised end-to-end
    in CI without autogen errors.
    """


def downgrade() -> None:
    """Downgrade schema."""
    pass
