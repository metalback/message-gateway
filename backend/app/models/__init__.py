"""SQLAlchemy ORM models.

One model per file under ``app/models/`` (e.g. ``client.py``,
``message.py``). Database tables follow the schema described in
``PRD.md`` §"Modelo de datos".

The base class lives in ``app.models.base`` and is the single
source of truth for the declarative ``metadata`` Alembic reads
when generating migrations.
"""
