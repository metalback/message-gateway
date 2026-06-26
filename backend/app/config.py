"""Application configuration loaded from environment variables.

Centralised here so the rest of the codebase can import a typed
`Settings` object instead of reading `os.environ` everywhere. Tests can
override the values via `monkeypatch.setenv` or by constructing a
`Settings()` instance directly.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the FastAPI backend."""

    # --- General ---------------------------------------------------------
    env: str = Field(default="development", alias="BACKEND_ENV")
    log_level: str = Field(default="info", alias="LOG_LEVEL")
    secret_key: str = Field(default="dev-secret-change-me", alias="SECRET_KEY")

    # --- CORS ------------------------------------------------------------
    # Comma separated list. "*" is allowed only in development.
    cors_allow_origins: str = Field(default="http://localhost:4200", alias="CORS_ALLOW_ORIGINS")

    # --- PostgreSQL ------------------------------------------------------
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_user: str = Field(default="msg_gateway", alias="POSTGRES_USER")
    postgres_password: str = Field(default="msg_gateway", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="msg_gateway", alias="POSTGRES_DB")

    @property
    def database_url(self) -> str:
        """Build an async SQLAlchemy URL from the discrete PG settings."""
        return (
            "postgresql+asyncpg://"
            f"{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # --- Redis -----------------------------------------------------------
    redis_host: str = Field(default="localhost", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_db: int = Field(default=0, alias="REDIS_DB")

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # --- Pydantic config ------------------------------------------------
    # `populate_by_name=True` lets tests instantiate `Settings(field="x")`
    # using the pythonic name even though we expose UPPER_SNAKE env vars.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached `Settings` instance.

    `lru_cache` keeps the settings object a process-level singleton while
    still allowing tests to clear the cache via `get_settings.cache_clear()`.
    """
    return Settings()
