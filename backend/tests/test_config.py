"""Unit tests for `app.config.Settings`.

We focus on the derived URLs because those are the bits the rest of
the codebase actually consumes, plus the env-var override behaviour
to make sure the docker-compose / .env wiring works.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


def test_database_url_uses_asyncpg_driver() -> None:
    settings = Settings(
        postgres_user="alice",
        postgres_password="s3cret",
        postgres_host="db.internal",
        postgres_port=5433,
        postgres_db="messaging",
    )
    assert settings.database_url == "postgresql+asyncpg://alice:s3cret@db.internal:5433/messaging"


def test_redis_url_includes_db_index() -> None:
    settings = Settings(redis_host="cache", redis_port=6380, redis_db=2)
    assert settings.redis_url == "redis://cache:6380/2"


def test_defaults_are_development_safe() -> None:
    settings = Settings()
    assert settings.env == "development"
    assert settings.postgres_db == "msg_gateway"
    assert settings.redis_db == 0
    # The default secret is intentionally weak; the deployment story
    # is that it must be overridden via env in any non-dev environment.
    assert settings.secret_key != ""


def test_env_vars_override_field_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `Settings` class uses UPPER_SNAKE env aliases (see `alias=`
    in `config.py`); make sure every alias actually wins over the
    declared default."""
    monkeypatch.setenv("BACKEND_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("SECRET_KEY", "rotate-me-please")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com,https://admin.example.com")
    monkeypatch.setenv("POSTGRES_HOST", "pg.internal")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("REDIS_HOST", "redis.internal")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("REDIS_DB", "7")

    settings = Settings()

    assert settings.env == "production"
    assert settings.log_level == "warning"
    assert settings.secret_key == "rotate-me-please"
    assert settings.cors_allow_origins == ("https://app.example.com,https://admin.example.com")
    assert settings.postgres_host == "pg.internal"
    assert settings.postgres_port == 5433
    assert settings.redis_host == "redis.internal"
    assert settings.redis_port == 6380
    assert settings.redis_db == 7


def test_settings_load_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`pydantic-settings` honours `env_file` declared on the model
    config. We write a temporary `.env` to confirm the parser is wired
    up and that values propagate to the derived URLs.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_USER=file_user\n"
        "POSTGRES_PASSWORD=file_pw\n"
        "POSTGRES_HOST=pg.from.file\n"
        "POSTGRES_DB=file_db\n"
        "REDIS_HOST=redis.from.file\n",
        encoding="utf-8",
    )

    # Clear the `lru_cache` so the test sees the freshly-constructed
    # settings object. This is also the pattern consumers use in CI.
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        monkeypatch.setenv("ENV_FILE", str(env_file))
        settings = Settings(_env_file=str(env_file))

        assert settings.postgres_user == "file_user"
        assert settings.postgres_password == "file_pw"
        assert settings.postgres_host == "pg.from.file"
        assert settings.postgres_db == "file_db"
        assert settings.redis_host == "redis.from.file"
        assert (
            settings.database_url
            == "postgresql+asyncpg://file_user:file_pw@pg.from.file:5432/file_db"
        )
        assert settings.redis_url == "redis://redis.from.file:6379/0"
    finally:
        get_settings.cache_clear()
