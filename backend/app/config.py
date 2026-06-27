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

    # --- Auth & registration --------------------------------------------
    # The cost factor used by :mod:`app.services.auth` whenever it
    # hashes a password or an API key. 12 is the OWASP-recommended
    # minimum for 2024+; lower in dev / test to keep unit suites
    # fast (a round=12 hash is ~250 ms on commodity hardware).
    bcrypt_rounds: int = Field(default=12, alias="BCRYPT_ROUNDS", ge=4, le=15)

    # HMAC secret used to sign the dashboard session JWT. Kept
    # independent from `secret_key` so a JWT rotation does not
    # cascade into rotating the rest of the platform's secrets.
    jwt_secret: str = Field(default="dev-jwt-change-me", alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    # Dashboard session lifetime in minutes.
    jwt_ttl_minutes: int = Field(default=60, alias="JWT_TTL_MINUTES", ge=1, le=60 * 24 * 7)

    # Public prefix attached to every API key the platform mints.
    # The default matches the documented integration contract; the
    # field is exposed so a future "staging" environment can ship
    # `mgw_test_…` keys without code changes.
    api_key_prefix: str = Field(default="mgw_live_", alias="API_KEY_PREFIX")

    # --- Provider integrations ------------------------------------------
    # WhatsApp / Meta Cloud API. The platform owns the WABA and
    # every client sends through the same ``phone_number_id``;
    # ``access_token`` is the long-lived bearer the Cloud API
    # expects on every call. ``api_base`` / ``api_version`` are
    # exposed so a future deployment can pin a different Graph
    # version (or a sandbox host) without code changes.
    meta_whatsapp_access_token: str = Field(
        default="dev-meta-token", alias="META_WHATSAPP_ACCESS_TOKEN"
    )
    meta_whatsapp_phone_number_id: str = Field(
        default="dev-phone-id", alias="META_WHATSAPP_PHONE_NUMBER_ID"
    )
    meta_whatsapp_api_base: str = Field(
        default="https://graph.facebook.com", alias="META_WHATSAPP_API_BASE"
    )
    meta_whatsapp_api_version: str = Field(default="v22.0", alias="META_WHATSAPP_API_VERSION")

    # Local Chilean SMS aggregator. ``api_url`` is the operator's
    # REST endpoint (varies by carrier), ``sender_id`` is the
    # alphanumeric "from" string the aggregator prints on the
    # recipient's handset.
    sms_aggregator_api_url: str = Field(
        default="https://sms.aggregator.cl", alias="SMS_AGGREGATOR_API_URL"
    )
    sms_aggregator_api_key: str = Field(
        default="dev-sms-aggregator-key", alias="SMS_AGGREGATOR_API_KEY"
    )
    sms_aggregator_sender_id: str = Field(default="MSGGTWY", alias="SMS_AGGREGATOR_SENDER_ID")

    # Default timeout (in seconds) applied to every provider HTTP
    # call. Kept low so a misconfigured upstream fails the request
    # fast instead of stalling on a TCP handshake. The Meta Cloud
    # API recommends a ceiling around 10 seconds; the same value
    # works for the SMS aggregator.
    provider_timeout_seconds: float = Field(
        default=10.0, alias="PROVIDER_TIMEOUT_SECONDS", ge=1.0, le=60.0
    )

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
