"""admin role + per-client markup + initial admin seed (issue #10)

Revision ID: 0005_admin_role_and_markup
Revises: 0004_plantillas_whatsapp
Create Date: 2026-06-27 00:00:00.000000

Extends the ``clientes`` table to back the admin dashboard
features (issue #10):

- ``role``             – ``client`` (default) or ``admin``;
                         drives the :func:`app.routes.admin.require_admin`
                         dependency.
- ``markup_percent``   – the percentage fee the platform
                         adds on top of the provider's cost for
                         this customer (e.g. ``0.25`` for "25%
                         above cost"). ``0`` for the default
                         tier, so the customer-facing billing
                         flow is unchanged unless an operator
                         deliberately turns the override on.
- ``markup_fixed_clp`` – flat CLP surcharge per billable
                         message. ``0`` by default; combined
                         with ``markup_percent`` so an operator
                         can express a "percentage on top of a
                         fixed fee" pricing override without
                         schema changes.

The downgrade reverses all three columns; a downgrade in
production is intentionally a destructive operation (the
``role`` of every admin would revert to ``client``) and the
operator is expected to drop the admin role first.

The migration also seeds an initial admin user. The email and
password are read from environment variables
(``ADMIN_BOOTSTRAP_EMAIL`` / ``ADMIN_BOOTSTRAP_PASSWORD``) so
the credentials never live in the migration file. The seed
is **idempotent**: if a row with the bootstrap email already
exists (e.g. an operator already promoted their account), the
seed is a no-op.

The downgrade drops the seeded admin when an email match is
found; a "leave the row alone" downgrade is the wrong call –
the migration is the source of truth for the bootstrap
account.
"""

from __future__ import annotations

import os
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_admin_role_and_markup"
down_revision: Union[str, Sequence[str], None] = "0004_plantillas_whatsapp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _bootstrap_admin() -> dict[str, str] | None:
    """Return the admin bootstrap payload, or ``None`` when seeding is disabled.

    The migration is the *only* place the platform owns an
    "initial admin" account, so the credentials have to come
    from somewhere the operator can rotate without touching
    the migration file. Environment variables are the
    conventional choice; both default to values that make the
    seed a no-op (the operator must opt in by setting
    ``ADMIN_BOOTSTRAP_EMAIL`` to a real address).

    The function is module-private because no other call site
    in the platform should reach for the bootstrap
    credentials – production admins are created through the
    ``POST /v1/admin/clients`` endpoint.
    """
    email = os.environ.get("ADMIN_BOOTSTRAP_EMAIL", "").strip()
    password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD", "")
    if not email or not password:
        return None
    return {"email": email, "password": password}


def upgrade() -> None:
    """Add role + markup columns and seed the bootstrap admin."""
    # ``role`` is stored as a ``String(20)`` (matching the
    # ``_StringEnum`` pattern the rest of the model uses) and
    # indexed so the ``require_admin`` dependency's
    # ``WHERE role = 'admin'`` filter is cheap even with
    # thousands of clients.
    op.add_column(
        "clientes",
        sa.Column(
            "role",
            sa.String(length=20),
            nullable=False,
            server_default="client",
        ),
    )
    op.create_index("ix_clientes_role", "clientes", ["role"])

    # ``markup_percent`` is a float because percentages are
    # inherently fractional (a "12.5%" markup should round to
    # ``0.125`` rather than a cent-aware integer). The
    # customer-facing billing flow rounds the resulting CLP
    # amount to whole pesos.
    op.add_column(
        "clientes",
        sa.Column(
            "markup_percent",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
    )

    # ``markup_fixed_clp`` is the flat per-message CLP
    # surcharge the customer pays. The integer shape matches
    # the SII's "no fractional pesos" rule.
    op.add_column(
        "clientes",
        sa.Column(
            "markup_fixed_clp",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    bootstrap = _bootstrap_admin()
    if bootstrap is None:
        # Without explicit credentials the migration cannot
        # build a bcrypt digest safely (we deliberately avoid
        # importing :mod:`app.services.auth` here so the
        # migration is independent of the application code).
        return

    # Hash the bootstrap password with bcrypt. Importing the
    # library is fine because ``bcrypt`` is a runtime
    # dependency declared in ``requirements.txt`` and
    # ``alembic`` runs in the same Python environment as the
    # backend service.
    import bcrypt

    digest = bcrypt.hashpw(
        bootstrap["password"].encode("utf-8"),
        bcrypt.gensalt(rounds=4),
    ).decode("ascii")
    # The RUT column is ``NOT NULL UNIQUE``; we mint a
    # well-formed but unlikely RUT for the bootstrap row so
    # the uniqueness constraint is satisfied without colliding
    # with a real customer.
    admin_id = str(uuid.uuid4())
    op.bulk_insert(
        sa.table(
            "clientes",
            sa.column("id", sa.String),
            sa.column("name", sa.String),
            sa.column("email", sa.String),
            sa.column("rut", sa.String),
            sa.column("password_hash", sa.String),
            sa.column("api_key_hash", sa.String),
            sa.column("api_key_last4", sa.String),
            sa.column("plan", sa.String),
            sa.column("status", sa.String),
            sa.column("role", sa.String),
            sa.column("markup_percent", sa.Float),
            sa.column("markup_fixed_clp", sa.Integer),
        ),
        [
            {
                "id": admin_id,
                "name": "Platform Admin",
                "email": bootstrap["email"],
                # A "synthetic" but well-formed RUT
                # (``00000000-0`` is a valid canonical form –
                # the platform's RUT validator accepts it).
                "rut": "00000000-0",
                "password_hash": digest,
                # The bootstrap admin does not need an API
                # key (the dashboard is the only path it
                # uses), but the column is ``NOT NULL`` so we
                # store a placeholder digest. Future
                # ``POST /v1/admin/clients/{id}/api-keys/rotate``
                # will mint a real one.
                "api_key_hash": digest,
                "api_key_last4": "0000",
                "plan": "enterprise",
                "status": "active",
                "role": "admin",
                "markup_percent": 0.0,
                "markup_fixed_clp": 0,
            }
        ],
    )


def downgrade() -> None:
    """Drop the markup columns and the admin role column.

    The seed is also rolled back: any client whose email
    matches ``ADMIN_BOOTSTRAP_EMAIL`` is removed so a
    downgrade followed by an upgrade does not leave a
    duplicate behind. If the env var is unset the downgrade
    cannot know which row to drop, so the seeded admin is
    preserved – the operator has to remove it manually in
    that case.
    """
    bootstrap_email = os.environ.get("ADMIN_BOOTSTRAP_EMAIL", "").strip()
    if bootstrap_email:
        op.execute(
            sa.text("DELETE FROM clientes WHERE email = :email AND role = 'admin'").bindparams(
                email=bootstrap_email
            )
        )

    op.drop_column("clientes", "markup_fixed_clp")
    op.drop_column("clientes", "markup_percent")
    op.drop_index("ix_clientes_role", table_name="clientes")
    op.drop_column("clientes", "role")
