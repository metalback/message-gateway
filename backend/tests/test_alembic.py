"""Tests pinning the Alembic configuration.

The migrations directory is part of the deployable image and
must keep a stable shape:

- ``alembic.ini`` declares the migrations folder and a non-empty
  ``sqlalchemy.url`` (a placeholder, since ``env.py`` overrides it).
- ``alembic/env.py`` points ``target_metadata`` at
  :data:`app.models.base.Base.metadata` and pulls the connection
  URL from :class:`app.config.Settings`.
- The ``versions`` directory ships at least one migration so the
  ``alembic`` CLI can be exercised end-to-end.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = BACKEND_DIR / "alembic"
VERSIONS_DIR = ALEMBIC_DIR / "versions"
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ENV_PY = ALEMBIC_DIR / "env.py"


def test_alembic_ini_exists() -> None:
    assert ALEMBIC_INI.is_file(), "alembic.ini must ship with the backend"


def test_alembic_directory_layout() -> None:
    """Alembic expects ``alembic/`` and ``alembic/versions/`` to
    exist. Missing directories break the CLI before the script
    even runs."""
    assert ALEMBIC_DIR.is_dir()
    assert VERSIONS_DIR.is_dir()


def test_alembic_ini_points_at_script_location() -> None:
    text = ALEMBIC_INI.read_text(encoding="utf-8")
    assert re.search(
        r"^script_location\s*=\s*%\(here\)s/alembic\s*$",
        text,
        re.MULTILINE,
    ), "alembic.ini must point script_location at the alembic/ dir"


def test_alembic_ini_declares_sqlalchemy_url() -> None:
    """Even though ``env.py`` overrides the URL at runtime, the
    parser requires the key to be present. Leaving it commented
    out makes ``alembic config`` raise a KeyError."""
    text = ALEMBIC_INI.read_text(encoding="utf-8")
    assert re.search(
        r"^sqlalchemy\.url\s*=\s*\S+",
        text,
        re.MULTILINE,
    ), "alembic.ini must declare sqlalchemy.url (placeholder is OK)"


def test_env_py_wires_app_metadata() -> None:
    text = ENV_PY.read_text(encoding="utf-8")
    assert "Base.metadata" in text, "env.py must point at app.models.base.Base.metadata"
    assert "get_settings" in text, "env.py must read the URL from app.config"


def test_versions_directory_has_at_least_one_migration() -> None:
    """The scaffold ships one empty migration so ``alembic`` can
    be exercised in CI. Adding the first domain migration is a
    follow-up task; the directory must not be empty though."""
    migrations = list(VERSIONS_DIR.glob("*.py"))
    migrations = [m for m in migrations if not m.name.startswith("__")]
    assert migrations, "alembic/versions/ must contain at least one migration"


def test_migration_files_have_revision_and_upgrade() -> None:
    """Every migration file must declare the standard contract:
    a ``revision`` variable and ``upgrade`` / ``downgrade`` callables."""
    migrations = [m for m in VERSIONS_DIR.glob("*.py") if not m.name.startswith("__")]
    assert migrations
    for path in migrations:
        text = path.read_text(encoding="utf-8")
        assert "revision:" in text, f"{path.name} must declare `revision`"
        assert "def upgrade" in text, f"{path.name} must define `upgrade`"
        assert "def downgrade" in text, f"{path.name} must define `downgrade`"
