"""Typed application configuration.

Configuration is environment-first (12-factor): every setting has an ``CIP_``-prefixed
environment variable, nested settings use a ``__`` delimiter (``CIP_POSTGRES__HOST``).
``.env.example`` is the canonical template.

Two rules this module enforces rather than documents:

1. Secrets are never rendered. Password-bearing fields are ``SecretStr`` so an
   accidental ``repr``/log of a settings object cannot leak a credential.
2. Deployed environments cannot run with development defaults. ``Settings`` validates
   TLS mode, auth configuration, and debug flags against the environment and refuses to
   construct an unsafe production configuration (see :meth:`Settings._validate_environment`).
"""

from __future__ import annotations

import secrets
import sys
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "AuthMode",
    "AuthSettings",
    "Environment",
    "IngestionSettings",
    "LogFormat",
    "MongoSettings",
    "Neo4jSettings",
    "PostgresSettings",
    "Settings",
    "StorageBackend",
    "StorageSettings",
    "get_settings",
]


class Environment(StrEnum):
    """Deployment environment. Drives safety validation, not just labelling."""

    LOCAL = "local"
    TEST = "test"
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"

    @classmethod
    def _missing_(cls, value: object) -> Environment | None:
        """Accept the long spellings as well as the short ones.

        ``cip_core`` and ``cip_platform`` both read ``CIP_ENVIRONMENT`` and were written with
        different vocabularies — ``prod`` here, ``production`` there. Every deployment asset in
        the repository sets the long form, so the image, the compose stack, and the ConfigMap
        all set a value this enum rejected, and every containerised start failed at settings
        load. Nothing caught it because tests never set the variable.

        Both spellings are accepted rather than one being renamed: operators, runbooks, and
        existing manifests use either, and a config layer that rejects ``production`` for being
        spelled out adds no safety.
        """
        if not isinstance(value, str):
            return None
        return {
            "production": cls.PROD,
            "development": cls.DEV,
            "testing": cls.TEST,
        }.get(value.strip().lower())

    @property
    def is_deployed(self) -> bool:
        """True for environments that can hold real tenant data."""
        return self in {Environment.DEV, Environment.STAGING, Environment.PROD}


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


class AuthMode(StrEnum):
    LOCAL_HS256 = "local_hs256"
    OIDC = "oidc"


class StorageBackend(StrEnum):
    LOCAL = "local"
    S3 = "s3"


class PostgresSettings(BaseSettings):
    """Operational store connection settings (docs/database/postgres-schema.sql)."""

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = "cip"
    user: str = "cip"
    password: SecretStr = SecretStr("")
    ssl_mode: Literal["disable", "prefer", "require", "verify-ca", "verify-full"] = "prefer"
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=5, ge=0, le=100)
    command_timeout_seconds: float = Field(default=30.0, gt=0)
    statement_cache_size: int = Field(default=0, ge=0)
    """0 disables asyncpg's prepared-statement cache — required when connecting through a
    transaction-pooling proxy (PgBouncer), which the tenant-sharded pooler tier in
    ADR-0003 may sit behind. Safe default; raise it only for direct connections."""

    def dsn(self, *, driver: str = "postgresql+asyncpg") -> str:
        """Build a SQLAlchemy URL.

        The password is percent-encoded because asyncpg's URL parser is strict about
        reserved characters, and generated production passwords routinely contain them.
        """
        from urllib.parse import quote

        password = quote(self.password.get_secret_value(), safe="")
        user = quote(self.user, safe="")
        return f"{driver}://{user}:{password}@{self.host}:{self.port}/{self.database}"


class MongoSettings(BaseSettings):
    """Parsed-document artifact store (see docs/design/adr-0005-phase1-implementation.md)."""

    uri: SecretStr = SecretStr("mongodb://localhost:27017")
    database: str = "cip"
    server_selection_timeout_ms: int = Field(default=5000, ge=100)
    parsed_documents_collection: str = "parsed_documents"


class Neo4jSettings(BaseSettings):
    """Graph store settings.

    Phase 1 establishes connectivity and health-checking only; graph construction is
    Phase 2 (docs/roadmap/implementation-roadmap.md).
    """

    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: SecretStr = SecretStr("")
    database: str = "neo4j"
    connection_timeout_seconds: float = Field(default=10.0, gt=0)


class StorageSettings(BaseSettings):
    """Raw-document object storage."""

    backend: StorageBackend = StorageBackend.LOCAL
    local_root: Path = Path("./var/storage")
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_endpoint_url: str | None = None

    @model_validator(mode="after")
    def _require_bucket_for_s3(self) -> StorageSettings:
        if self.backend is StorageBackend.S3 and not self.s3_bucket:
            raise ValueError("CIP_STORAGE__S3_BUCKET is required when backend is 's3'")
        return self


class AuthSettings(BaseSettings):
    """Authentication skeleton settings (docs/architecture/06-security-compliance.md §3)."""

    enabled: bool = True
    mode: AuthMode = AuthMode.LOCAL_HS256
    jwt_secret: SecretStr = SecretStr("")
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "cip-local"
    jwt_audience: str = "cip-api"
    jwks_url: str | None = None
    leeway_seconds: int = Field(default=30, ge=0, le=300)

    @model_validator(mode="after")
    def _validate_mode_requirements(self) -> AuthSettings:
        # The HS256 secret is validated by `Settings`, not here: whether a missing secret
        # is a fatal error or an ephemeral-key development convenience depends on the
        # environment, which this nested model cannot see.
        if self.enabled and self.mode is AuthMode.OIDC and not self.jwks_url:
            raise ValueError("CIP_AUTH__JWKS_URL is required when auth mode is 'oidc'")
        return self


class IngestionSettings(BaseSettings):
    """Document-intelligence pipeline tuning.

    Chunk sizing defaults follow docs/architecture/02-rag-hybrid-retrieval.md §1.2
    (256-512 tokens, 10-15% overlap).
    """

    max_upload_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    allowed_media_types: tuple[str, ...] = (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
    )

    chunk_target_tokens: int = Field(default=384, ge=32, le=4096)
    chunk_min_tokens: int = Field(default=64, ge=1, le=4096)
    chunk_max_tokens: int = Field(default=512, ge=32, le=8192)
    chunk_overlap_ratio: float = Field(default=0.12, ge=0.0, lt=0.5)

    ocr_enabled: bool = True
    ocr_dpi: int = Field(default=300, ge=72, le=1200)
    ocr_language: str = "eng"
    ocr_min_text_chars_per_page: int = Field(default=48, ge=0)
    """A PDF page yielding fewer extractable characters than this is treated as scanned
    and routed to OCR. Tuned low deliberately: a false positive costs OCR time, a false
    negative silently drops a page's clinical content from retrieval."""

    quality_min_score: float = Field(default=0.60, ge=0.0, le=1.0)

    @field_validator("chunk_max_tokens")
    @classmethod
    def _max_ge_target(cls, value: int, info: ValidationInfo) -> int:
        target = info.data.get("chunk_target_tokens")
        if target is not None and value < target:
            raise ValueError("chunk_max_tokens must be >= chunk_target_tokens")
        return value

    @field_validator("chunk_min_tokens")
    @classmethod
    def _min_le_target(cls, value: int, info: ValidationInfo) -> int:
        target = info.data.get("chunk_target_tokens")
        if target is not None and value > target:
            raise ValueError("chunk_min_tokens must be <= chunk_target_tokens")
        return value


class Settings(BaseSettings):
    """Root application settings."""

    model_config = SettingsConfigDict(
        env_prefix="CIP_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Environment.LOCAL
    service_name: str = "cip-ingestion"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: LogFormat = LogFormat.CONSOLE
    debug: bool = False

    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    mongo: MongoSettings = Field(default_factory=MongoSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)

    @model_validator(mode="after")
    def _validate_environment(self) -> Settings:
        """Refuse configurations that are unsafe for the declared environment.

        These are hard failures at startup rather than warnings: a deployed service that
        boots with TLS disabled or authentication off is a PHI incident, and the cheapest
        place to catch it is process start, not a later audit.
        """
        if not self.environment.is_deployed:
            # Local and test runs get an ephemeral HS256 key when none is configured, so a
            # fresh checkout works without a `.env`. The key is random per process, so
            # tokens do not survive a restart — which is the correct property for a
            # development shortcut, and why this is never reachable in a deployed
            # environment (the `local_hs256` mode itself is rejected below).
            if self.auth.enabled and not self.auth.jwt_secret.get_secret_value():
                object.__setattr__(self.auth, "jwt_secret", SecretStr(secrets.token_urlsafe(48)))
            return self

        problems: list[str] = []
        if self.debug:
            problems.append("CIP_DEBUG must be false in deployed environments")
        if self.log_format is not LogFormat.JSON:
            problems.append("CIP_LOG_FORMAT must be 'json' in deployed environments")
        if self.postgres.ssl_mode != "verify-full":
            problems.append(
                "CIP_POSTGRES__SSL_MODE must be 'verify-full' in deployed environments "
                "(docs/architecture/06-security-compliance.md §9)"
            )
        if not self.auth.enabled:
            problems.append("CIP_AUTH__ENABLED must be true in deployed environments")
        if self.auth.enabled and self.auth.mode is AuthMode.LOCAL_HS256:
            problems.append(
                "CIP_AUTH__MODE 'local_hs256' is a development-only shortcut; deployed "
                "environments must federate with the tenant IdP ('oidc')"
            )
        if self.storage.backend is StorageBackend.LOCAL:
            problems.append(
                "CIP_STORAGE__BACKEND 'local' is development-only; deployed environments "
                "must use durable object storage"
            )

        if problems:
            raise ValueError(
                f"Unsafe configuration for environment '{self.environment}':\n  - "
                + "\n  - ".join(problems)
            )
        return self

    def describe(self) -> dict[str, Any]:
        """Secret-free settings summary, safe to emit at startup."""
        return {
            "environment": str(self.environment),
            "service_name": self.service_name,
            "log_level": self.log_level,
            "log_format": str(self.log_format),
            "debug": self.debug,
            "postgres_host": self.postgres.host,
            "postgres_database": self.postgres.database,
            "postgres_ssl_mode": self.postgres.ssl_mode,
            "mongo_database": self.mongo.database,
            "neo4j_uri": self.neo4j.uri,
            "storage_backend": str(self.storage.backend),
            "auth_enabled": self.auth.enabled,
            "auth_mode": str(self.auth.mode),
            "python": sys.version.split()[0],
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached because settings are immutable for a process lifetime and constructing them
    reads the environment and ``.env``. Tests that need different settings should build
    ``Settings(...)`` directly or call ``get_settings.cache_clear()``.
    """
    return Settings()
