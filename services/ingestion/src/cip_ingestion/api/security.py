"""Authentication skeleton.

Phase 1 ships the *verification and context-derivation* half of authentication, which is
the half the rest of the service depends on. Issuing tokens, federating with a tenant IdP,
and managing sessions belong to the Identity service.

Two verifiers implement one protocol:

* :class:`LocalHs256Verifier` — symmetric HS256 for local development and tests. Never
  permitted in a deployed environment; ``Settings`` rejects that configuration at startup.
* :class:`OidcVerifier` — the enterprise path, verifying RS256 against the tenant IdP's
  JWKS. The seam is defined and wired now so adopting it is configuration rather than a
  code change; JWKS retrieval and caching land with the Identity service.

The important output is :class:`~cip_core.tenancy.TenantContext`. Claims are translated
into it once, here, and every downstream layer receives the typed context instead of
re-parsing a token. That is what makes "no code path queries a store without a tenant"
(ADR-0003) checkable rather than aspirational.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

from jose import JWTError, jwt

from cip_core.config import AuthMode, AuthSettings
from cip_core.errors import AuthenticationError, ConfigurationError
from cip_core.logging import get_logger
from cip_core.tenancy import Role, TenantContext

__all__ = ["LocalHs256Verifier", "OidcVerifier", "TokenVerifier", "build_verifier"]

_log = get_logger(__name__)

#: Claim carrying the tenant UUID. Namespaced because ``tenant_id`` is a common claim name
#: and a collision with an IdP-provided claim would silently change tenant resolution.
_TENANT_CLAIM = "https://cip.example.com/tenant_id"
_ROLES_CLAIM = "https://cip.example.com/roles"
_SCOPES_CLAIM = "scope"


@runtime_checkable
class TokenVerifier(Protocol):
    """Verifies a bearer token and returns its claims."""

    @property
    def mode(self) -> str: ...

    def verify(self, token: str) -> dict[str, Any]:
        """Return verified claims, or raise :class:`AuthenticationError`."""
        ...


class LocalHs256Verifier:
    """Symmetric HS256 verification for local development and tests."""

    def __init__(self, settings: AuthSettings) -> None:
        secret = settings.jwt_secret.get_secret_value()
        if not secret:
            raise ConfigurationError("Local HS256 auth requires CIP_AUTH__JWT_SECRET")
        self._secret = secret
        self._settings = settings

    @property
    def mode(self) -> str:
        return str(AuthMode.LOCAL_HS256)

    def verify(self, token: str) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                self._secret,
                algorithms=[self._settings.jwt_algorithm],
                audience=self._settings.jwt_audience,
                issuer=self._settings.jwt_issuer,
                options={"leeway": self._settings.leeway_seconds},
            )
        except JWTError as exc:
            # The reason is logged but never returned: distinguishing "expired" from
            # "bad signature" to an unauthenticated caller is an oracle.
            _log.info("auth.token_rejected", reason=type(exc).__name__)
            raise AuthenticationError("Bearer token is invalid or expired") from exc


class OidcVerifier:
    """Asymmetric verification against a tenant IdP's JWKS.

    Deliberately incomplete in Phase 1: JWKS fetching, key caching, and rotation handling
    are the Identity service's responsibility. Raising a clear configuration error is
    correct behaviour here — ``Settings`` already requires ``oidc`` in deployed
    environments, so this failure is visible at startup of the first deployment rather
    than as a silent fallback to weaker verification.
    """

    def __init__(self, settings: AuthSettings) -> None:
        if not settings.jwks_url:
            raise ConfigurationError("OIDC auth requires CIP_AUTH__JWKS_URL")
        self._settings = settings

    @property
    def mode(self) -> str:
        return str(AuthMode.OIDC)

    def verify(self, token: str) -> dict[str, Any]:
        raise ConfigurationError(
            "OIDC token verification is provided by the Identity service, which is not "
            "part of Phase 1. Configure CIP_AUTH__MODE=local_hs256 for local development."
        )


def build_verifier(settings: AuthSettings) -> TokenVerifier:
    """Construct the verifier for the configured auth mode."""
    if settings.mode is AuthMode.LOCAL_HS256:
        return LocalHs256Verifier(settings)
    return OidcVerifier(settings)


def context_from_claims(claims: dict[str, Any], *, request_id: str | None = None) -> TenantContext:
    """Translate verified claims into a :class:`TenantContext`.

    Unknown roles are dropped rather than rejected: an IdP adding a role the platform does
    not model should not lock a user out, and an unmapped role grants nothing. Unknown
    *scopes* are preserved verbatim, since scope checks are exact-match and an unknown
    scope likewise grants nothing.
    """
    raw_tenant = claims.get(_TENANT_CLAIM)
    if not raw_tenant:
        raise AuthenticationError("Token is missing the tenant claim")
    try:
        tenant_id = uuid.UUID(str(raw_tenant))
    except ValueError as exc:
        raise AuthenticationError("Token tenant claim is not a valid identifier") from exc

    subject = claims.get("sub")
    if not subject:
        raise AuthenticationError("Token is missing the subject claim")

    roles: set[Role] = set()
    for raw_role in claims.get(_ROLES_CLAIM) or []:
        try:
            roles.add(Role(str(raw_role)))
        except ValueError:
            _log.debug("auth.unknown_role_ignored", role=str(raw_role))

    raw_scopes = claims.get(_SCOPES_CLAIM) or ""
    scopes = frozenset(raw_scopes.split()) if isinstance(raw_scopes, str) else frozenset(raw_scopes)

    return TenantContext(
        tenant_id=tenant_id,
        actor_id=str(subject),
        roles=frozenset(roles),
        scopes=scopes,
        request_id=request_id,
    )
