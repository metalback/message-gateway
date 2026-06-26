"""SQLAlchemy declarative base.

Centralising the base class keeps Alembic's autogenerate
working: every ORM model must inherit from :class:`Base` so its
table is registered in the same ``MetaData`` Alembic reads.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for every ORM model in the project."""
