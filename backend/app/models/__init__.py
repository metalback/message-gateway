"""SQLAlchemy ORM models.

One model per file under ``app/models/`` (e.g. ``client.py``,
``message.py``). Database tables follow the schema described in
``PRD.md`` §"Modelo de datos".

The base class lives in ``app.models.base`` and is the single
source of truth for the declarative ``metadata`` Alembic reads
when generating migrations.

Model modules are imported here for their side effects: every
``Base`` subclass must register itself in
``app.models.base.Base.metadata`` so the autogenerate pass in
:mod:`app.alembic.env` can pick the table up. Keeping the
imports in a single place also gives contributors a single
location to discover the full set of tables in the database.
"""

from app.models.base import Base
from app.models.client import Client, ClientPlan, ClientStatus

__all__ = ("Base", "Client", "ClientPlan", "ClientStatus")
