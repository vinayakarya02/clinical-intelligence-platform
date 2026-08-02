"""Configuration tests.

The environment-safety validation is the most important behaviour here: it is the control
that stops a deployed service booting with development defaults, so it gets a test per
rule rather than one aggregate assertion.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cip_core.config import (
    AuthSettings,
    Environment,
    IngestionSettings,
    PostgresSettings,
    Settings,
    StorageSettings,
)


def _prod_kwargs(**overrides: object) -> dict[str, object]:
    """A production configuration that passes validation, before overrides."""
    base: dict[str, object] = {
        "environment": Environment.PROD,
        "log_format": "json",
        "debug": False,
        "postgres": PostgresSettings(ssl_mode="verify-full"),
        "auth": AuthSettings(enabled=True, mode="oidc", jwks_url="https://idp.example.com/jwks"),
        "storage": StorageSettings(backend="s3", s3_bucket="cip-prod"),
    }
    base.update(overrides)
    return base


class TestEnvironment:
    def test_deployed_environments_are_flagged(self) -> None:
        assert Environment.PROD.is_deployed
        assert Environment.STAGING.is_deployed
        assert Environment.DEV.is_deployed

    def test_local_and_test_are_not_deployed(self) -> None:
        assert not Environment.LOCAL.is_deployed
        assert not Environment.TEST.is_deployed


class TestPostgresSettings:
    def test_dsn_percent_encodes_password(self) -> None:
        settings = PostgresSettings(
            user="cip", password="p@ss/w:rd?#", host="db", port=5432, database="cip"
        )
        dsn = settings.dsn()
        assert "p%40ss%2Fw%3Ard%3F%23" in dsn
        assert "p@ss/w:rd?#" not in dsn

    def test_dsn_uses_asyncpg_driver_by_default(self) -> None:
        assert PostgresSettings().dsn().startswith("postgresql+asyncpg://")

    def test_password_is_not_rendered_in_repr(self) -> None:
        settings = PostgresSettings(password="super-secret")
        assert "super-secret" not in repr(settings)


class TestStorageSettings:
    def test_s3_backend_requires_a_bucket(self) -> None:
        with pytest.raises(ValidationError, match="S3_BUCKET"):
            StorageSettings(backend="s3")

    def test_s3_backend_accepts_a_bucket(self) -> None:
        assert StorageSettings(backend="s3", s3_bucket="cip").s3_bucket == "cip"


class TestAuthSettings:
    def test_oidc_requires_a_jwks_url(self) -> None:
        with pytest.raises(ValidationError, match="JWKS_URL"):
            AuthSettings(enabled=True, mode="oidc")

    def test_disabled_auth_skips_mode_requirements(self) -> None:
        assert AuthSettings(enabled=False, mode="oidc").enabled is False


class TestIngestionSettings:
    def test_chunk_bounds_must_be_ordered(self) -> None:
        with pytest.raises(ValidationError):
            IngestionSettings(chunk_target_tokens=400, chunk_max_tokens=100)
        with pytest.raises(ValidationError):
            IngestionSettings(chunk_target_tokens=100, chunk_min_tokens=400)

    def test_overlap_ratio_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            IngestionSettings(chunk_overlap_ratio=0.9)


class TestEnvironmentSafetyValidation:
    def test_valid_production_configuration_is_accepted(self) -> None:
        assert Settings(**_prod_kwargs()).environment is Environment.PROD

    def test_debug_is_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="CIP_DEBUG"):
            Settings(**_prod_kwargs(debug=True))

    def test_console_logging_is_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="CIP_LOG_FORMAT"):
            Settings(**_prod_kwargs(log_format="console"))

    def test_weak_tls_is_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="SSL_MODE"):
            Settings(**_prod_kwargs(postgres=PostgresSettings(ssl_mode="prefer")))

    def test_disabled_auth_is_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="CIP_AUTH__ENABLED"):
            Settings(**_prod_kwargs(auth=AuthSettings(enabled=False)))

    def test_local_hs256_is_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="local_hs256"):
            Settings(
                **_prod_kwargs(auth=AuthSettings(enabled=True, mode="local_hs256", jwt_secret="x"))
            )

    def test_local_storage_is_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError, match="CIP_STORAGE__BACKEND"):
            Settings(**_prod_kwargs(storage=StorageSettings(backend="local")))

    def test_all_violations_are_reported_together(self) -> None:
        """One failed boot should reveal every problem, not just the first."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(
                environment=Environment.PROD,
                debug=True,
                log_format="console",
                storage=StorageSettings(backend="local"),
            )
        message = str(exc_info.value)
        assert "CIP_DEBUG" in message
        assert "CIP_LOG_FORMAT" in message
        assert "CIP_STORAGE__BACKEND" in message


class TestDevelopmentConvenience:
    def test_local_environment_generates_an_ephemeral_secret(self) -> None:
        """A fresh checkout must work without a .env, but never with a fixed default."""
        first = Settings(environment=Environment.LOCAL)
        second = Settings(environment=Environment.LOCAL)

        assert first.auth.jwt_secret.get_secret_value()
        assert (
            first.auth.jwt_secret.get_secret_value() != second.auth.jwt_secret.get_secret_value()
        ), "an ephemeral key must be random per process, not a shared constant"

    def test_configured_secret_is_preserved(self) -> None:
        settings = Settings(
            environment=Environment.LOCAL,
            auth=AuthSettings(enabled=True, mode="local_hs256", jwt_secret="explicit"),
        )
        assert settings.auth.jwt_secret.get_secret_value() == "explicit"


class TestDescribe:
    def test_describe_omits_secrets(self) -> None:
        settings = Settings(
            environment=Environment.LOCAL,
            postgres=PostgresSettings(password="do-not-leak"),
            auth=AuthSettings(enabled=True, mode="local_hs256", jwt_secret="also-secret"),
        )
        rendered = str(settings.describe())
        assert "do-not-leak" not in rendered
        assert "also-secret" not in rendered
        assert "postgres_host" in rendered
