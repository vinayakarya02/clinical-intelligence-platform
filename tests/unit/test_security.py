"""Authentication skeleton tests."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from jose import jwt

from cip_core.config import AuthMode, AuthSettings
from cip_core.errors import AuthenticationError, ConfigurationError
from cip_core.tenancy import Role
from cip_ingestion.api.security import (
    LocalHs256Verifier,
    OidcVerifier,
    build_verifier,
    context_from_claims,
)

_TENANT_CLAIM = "https://cip.example.com/tenant_id"
_ROLES_CLAIM = "https://cip.example.com/roles"
_SECRET = "test-secret-value"


@pytest.fixture
def auth_settings() -> AuthSettings:
    return AuthSettings(
        enabled=True,
        mode=AuthMode.LOCAL_HS256,
        jwt_secret=_SECRET,
        jwt_issuer="cip-local",
        jwt_audience="cip-api",
    )


def _token(
    *,
    tenant_id: uuid.UUID | str | None = None,
    subject: str | None = "user-1",
    roles: list[str] | None = None,
    scope: str = "documents:read documents:write",
    secret: str = _SECRET,
    issuer: str = "cip-local",
    audience: str = "cip-api",
    expires_in: int = 300,
) -> str:
    now = dt.datetime.now(dt.UTC)
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + dt.timedelta(seconds=expires_in),
        "scope": scope,
    }
    if subject is not None:
        claims["sub"] = subject
    if tenant_id is not None:
        claims[_TENANT_CLAIM] = str(tenant_id)
    if roles is not None:
        claims[_ROLES_CLAIM] = roles
    return jwt.encode(claims, secret, algorithm="HS256")


class TestVerifierConstruction:
    def test_builds_a_local_verifier(self, auth_settings: AuthSettings) -> None:
        assert build_verifier(auth_settings).mode == str(AuthMode.LOCAL_HS256)

    def test_builds_an_oidc_verifier(self) -> None:
        settings = AuthSettings(
            enabled=True, mode=AuthMode.OIDC, jwks_url="https://idp.example.com/jwks"
        )
        assert build_verifier(settings).mode == str(AuthMode.OIDC)

    def test_local_verifier_requires_a_secret(self) -> None:
        settings = AuthSettings.model_construct(
            enabled=True, mode=AuthMode.LOCAL_HS256, jwt_secret=__import__("pydantic").SecretStr("")
        )
        with pytest.raises(ConfigurationError, match="JWT_SECRET"):
            LocalHs256Verifier(settings)

    def test_oidc_verification_is_not_implemented_in_phase_1(self) -> None:
        """A clear configuration error beats silently falling back to weaker verification."""
        verifier = OidcVerifier(
            AuthSettings(enabled=True, mode=AuthMode.OIDC, jwks_url="https://idp/jwks")
        )
        with pytest.raises(ConfigurationError, match="Identity service"):
            verifier.verify("any-token")


class TestLocalHs256Verifier:
    def test_accepts_a_valid_token(self, auth_settings: AuthSettings, tenant_id: uuid.UUID) -> None:
        claims = LocalHs256Verifier(auth_settings).verify(_token(tenant_id=tenant_id))
        assert claims["sub"] == "user-1"

    @pytest.mark.parametrize(
        "token_kwargs",
        [
            {"secret": "wrong-secret"},
            {"issuer": "someone-else"},
            {"audience": "another-api"},
            {"expires_in": -60},
        ],
        ids=["bad-signature", "bad-issuer", "bad-audience", "expired"],
    )
    def test_rejects_invalid_tokens(
        self, auth_settings: AuthSettings, tenant_id: uuid.UUID, token_kwargs: dict
    ) -> None:
        verifier = LocalHs256Verifier(auth_settings)
        with pytest.raises(AuthenticationError):
            verifier.verify(_token(tenant_id=tenant_id, **token_kwargs))

    def test_rejects_a_malformed_token(self, auth_settings: AuthSettings) -> None:
        with pytest.raises(AuthenticationError):
            LocalHs256Verifier(auth_settings).verify("not.a.jwt")

    def test_rejection_reason_is_not_disclosed(
        self, auth_settings: AuthSettings, tenant_id: uuid.UUID
    ) -> None:
        """Distinguishing 'expired' from 'bad signature' to a caller is an oracle."""
        verifier = LocalHs256Verifier(auth_settings)
        messages = set()
        for kwargs in ({"secret": "wrong"}, {"expires_in": -60}):
            with pytest.raises(AuthenticationError) as exc_info:
                verifier.verify(_token(tenant_id=tenant_id, **kwargs))
            messages.add(exc_info.value.detail)
        assert len(messages) == 1


class TestContextFromClaims:
    def test_derives_a_tenant_context(self, tenant_id: uuid.UUID) -> None:
        context = context_from_claims(
            {
                _TENANT_CLAIM: str(tenant_id),
                "sub": "user-1",
                _ROLES_CLAIM: ["clinician"],
                "scope": "documents:read documents:write",
            },
            request_id="req-1",
        )
        assert context.tenant_id == tenant_id
        assert context.actor_id == "user-1"
        assert Role.CLINICIAN in context.roles
        assert "documents:write" in context.scopes
        assert context.request_id == "req-1"

    def test_requires_a_tenant_claim(self) -> None:
        with pytest.raises(AuthenticationError, match="tenant claim"):
            context_from_claims({"sub": "user-1"})

    def test_requires_a_subject_claim(self, tenant_id: uuid.UUID) -> None:
        with pytest.raises(AuthenticationError, match="subject claim"):
            context_from_claims({_TENANT_CLAIM: str(tenant_id)})

    def test_rejects_a_malformed_tenant_claim(self) -> None:
        with pytest.raises(AuthenticationError, match="not a valid identifier"):
            context_from_claims({_TENANT_CLAIM: "not-a-uuid", "sub": "user-1"})

    def test_unknown_roles_are_ignored_not_fatal(self, tenant_id: uuid.UUID) -> None:
        """An IdP adding a role the platform does not model must not lock a user out."""
        context = context_from_claims(
            {
                _TENANT_CLAIM: str(tenant_id),
                "sub": "user-1",
                _ROLES_CLAIM: ["clinician", "chief_wizard"],
            }
        )
        assert context.roles == frozenset({Role.CLINICIAN})

    def test_missing_scopes_yield_no_permissions(self, tenant_id: uuid.UUID) -> None:
        context = context_from_claims({_TENANT_CLAIM: str(tenant_id), "sub": "user-1"})
        assert context.scopes == frozenset()

    def test_scope_list_form_is_accepted(self, tenant_id: uuid.UUID) -> None:
        context = context_from_claims(
            {_TENANT_CLAIM: str(tenant_id), "sub": "user-1", "scope": ["documents:read"]}
        )
        assert "documents:read" in context.scopes
