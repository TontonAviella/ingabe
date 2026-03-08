"""Centralized application configuration using Pydantic Settings.

Replaces scattered ``os.getenv()`` / ``os.environ[]`` calls with a
single, validated, typed configuration object.

Usage::

    from src.config import settings

    bucket = settings.s3_bucket
    db_url = settings.postgres_dsn
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # ── PostgreSQL ──────────────────────────────────────────────────────
    postgres_host: str = Field(alias="POSTGRES_HOST")
    postgres_port: str = Field(default="5432", alias="POSTGRES_PORT")
    postgres_db: str = Field(alias="POSTGRES_DB")
    postgres_user: str = Field(alias="POSTGRES_USER")
    postgres_password: str = Field(alias="POSTGRES_PASSWORD")

    postgres_read_host: Optional[str] = Field(default=None, alias="POSTGRES_READ_HOST")
    postgres_read_port: Optional[str] = Field(default=None, alias="POSTGRES_READ_PORT")

    # ── S3 / MinIO ─────────────────────────────────────────────────────
    s3_access_key_id: str = Field(default="", alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str = Field(default="", alias="S3_SECRET_ACCESS_KEY")
    s3_endpoint_url: str = Field(default="", alias="S3_ENDPOINT_URL")
    s3_bucket: str = Field(default="mundi-uploads", alias="S3_BUCKET")
    s3_default_region: str = Field(default="us-east-1", alias="S3_DEFAULT_REGION")

    # ── Redis ──────────────────────────────────────────────────────────
    redis_host: str = Field(default="localhost", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")

    # ── OpenAI / LLM ──────────────────────────────────────────────────
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: Optional[str] = Field(default=None, alias="OPENAI_BASE_URL")

    # ── Application ────────────────────────────────────────────────────
    mundi_auth_mode: str = Field(default="view_only", alias="MUNDI_AUTH_MODE")
    website_domain: str = Field(default="http://localhost:5173", alias="WEBSITE_DOMAIN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # ── Sentinel Hub (Planet Labs integration) ──────────────────────────
    sh_client_id: str = Field(default="", alias="SH_CLIENT_ID")
    sh_client_secret: str = Field(default="", alias="SH_CLIENT_SECRET")

    # ── External services ──────────────────────────────────────────────
    qgis_processing_url: Optional[str] = Field(default=None, alias="QGIS_PROCESSING_URL")
    postgis_localhost_policy: str = Field(
        default="disallow", alias="POSTGIS_LOCALHOST_POLICY"
    )

    # ── Derived helpers ────────────────────────────────────────────────

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def postgres_read_dsn(self) -> Optional[str]:
        if not self.postgres_read_host:
            return None
        port = self.postgres_read_port or self.postgres_port
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_read_host}:{port}/{self.postgres_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


# Singleton — importable everywhere
settings = get_settings()
