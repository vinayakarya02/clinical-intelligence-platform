"""Authentication and authorisation.

API keys and JWT today, OIDC-ready by construction: :class:`TokenVerifier` is the protocol an
OIDC verifier implements, so adding one is a new class rather than a change to every route.

Three properties are enforced here because each closes a failure that is invisible at the call
site:

**API keys are stored hashed, and looked up by prefix.** A stolen database of raw keys is a
stolen fleet of credentials. Hashing alone would make lookup an O(n) scan over every key in the
system, so each key carries a short public prefix that is indexed, and only the candidates
sharing that prefix are compared — in constant time, because a timing difference on secret
comparison is a slow but real oracle.

**The tenant comes from the credential, never from the request.** A request body that can name
its own tenant is a request body that can name someone else's.

**Scopes are derived from roles, and a route declares the scope it needs.** Neither the role
set nor the route can drift into granting something nobody intended, because the mapping is one
table that a test asserts on.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from cip_core.errors import CipError
from cip_core.logging import get_logger

__all__ = [
    "ApiKeyRecord",
    "ApiKeyStore",
    "AuthenticationError",
    "AuthorizationError",
    "Principal",
    "Role",
    "Scope",
    "TokenVerifier",
    "issue_api_key",
    "scopes_for_roles",
]

_log = get_logger(__name__)

#: Characters of the key that are public and indexed. Long enough to make a prefix collision
#: rare, short enough to leak nothing useful if logged.
_PREFIX_LENGTH = 12

#: Bytes of entropy in the secret half. 32 bytes is 256 bits.
_SECRET_BYTES = 32


class AuthenticationError(CipError):
    """The caller could not be identified."""

    status = 401
    problem_type = "authentication-failed"
    title = "Authentication failed"


class AuthorizationError(CipError):
    """The caller was identified but may not do this."""

    status = 403
    problem_type = "authorization-failed"
    title = "Not permitted"


class Scope(StrEnum):
    """A single permission a route can require."""

    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_WRITE = "documents:write"
    PATIENTS_READ = "patients:read"
    REFERENCE_READ = "reference:read"
    ANALYTICS_READ = "analytics:read"
    COPILOT_ASK = "copilot:ask"
    ADMIN = "admin"


class Role(StrEnum):
    """A named bundle of scopes.

    Roles are what an administrator assigns; scopes are what code checks. Keeping them
    separate means a permission change is one edit to :data:`_ROLE_SCOPES` rather than a
    migration over every principal.
    """

    CLINICIAN = "clinician"
    RESEARCHER = "researcher"
    SERVICE = "service"
    TENANT_ADMIN = "tenant_admin"
    READ_ONLY = "read_only"


#: Role → scopes. The single place a permission is granted.
_ROLE_SCOPES: dict[Role, frozenset[Scope]] = {
    Role.CLINICIAN: frozenset(
        {
            Scope.DOCUMENTS_READ,
            Scope.PATIENTS_READ,
            Scope.REFERENCE_READ,
            Scope.COPILOT_ASK,
        }
    ),
    # No PATIENTS_READ: research runs on de-identified aggregates, and a researcher who can
    # read an identified record has defeated the de-identification.
    Role.RESEARCHER: frozenset(
        {Scope.DOCUMENTS_READ, Scope.REFERENCE_READ, Scope.ANALYTICS_READ, Scope.COPILOT_ASK}
    ),
    Role.SERVICE: frozenset(
        {
            Scope.DOCUMENTS_READ,
            Scope.DOCUMENTS_WRITE,
            Scope.PATIENTS_READ,
            Scope.REFERENCE_READ,
        }
    ),
    Role.TENANT_ADMIN: frozenset(
        {
            Scope.DOCUMENTS_READ,
            Scope.DOCUMENTS_WRITE,
            Scope.PATIENTS_READ,
            Scope.REFERENCE_READ,
            Scope.ANALYTICS_READ,
            Scope.COPILOT_ASK,
            Scope.ADMIN,
        }
    ),
    Role.READ_ONLY: frozenset({Scope.DOCUMENTS_READ, Scope.REFERENCE_READ}),
}


def scopes_for_roles(roles: frozenset[Role]) -> frozenset[Scope]:
    """Union of the scopes these roles grant."""
    granted: set[Scope] = set()
    for role in roles:
        granted |= _ROLE_SCOPES[role]
    return frozenset(granted)


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making a request, and what they may do.

    ``tenant_id`` is derived from the credential and is the only tenant the request may touch.
    Nothing downstream reads a tenant from the request body.
    """

    principal_id: str
    tenant_id: uuid.UUID
    roles: frozenset[Role]
    scopes: frozenset[Scope]
    kind: str = "api_key"
    display_name: str = ""
    expires_at: dt.datetime | None = None

    def require(self, scope: Scope) -> None:
        """Raise unless this principal holds ``scope``."""
        if scope not in self.scopes:
            raise AuthorizationError(f"This principal lacks the '{scope}' scope")

    def require_tenant(self, tenant_id: uuid.UUID) -> None:
        """Raise unless ``tenant_id`` is this principal's own."""
        if tenant_id != self.tenant_id:
            raise AuthorizationError("Resource belongs to a different tenant")

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and dt.datetime.now(dt.UTC) >= self.expires_at


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    """A stored API key. Holds the hash, never the secret."""

    key_id: str
    prefix: str
    secret_hash: str
    tenant_id: uuid.UUID
    roles: frozenset[Role]
    display_name: str = ""
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    expires_at: dt.datetime | None = None
    revoked: bool = False

    @property
    def is_usable(self) -> bool:
        if self.revoked:
            return False
        return self.expires_at is None or dt.datetime.now(dt.UTC) < self.expires_at


def _hash_secret(secret: str, *, pepper: str) -> str:
    """Hash a key secret.

    A single SHA-256 with a pepper rather than a slow KDF: an API key is 256 bits of random,
    so there is no dictionary to attack and the brute-force cost is already prohibitive. A
    slow KDF here would only add latency to every request. This reasoning does **not** carry
    over to user passwords, which are low-entropy and need Argon2 or equivalent.
    """
    return hashlib.sha256(f"{pepper}:{secret}".encode()).hexdigest()


def issue_api_key(
    *,
    tenant_id: uuid.UUID,
    roles: frozenset[Role],
    pepper: str,
    display_name: str = "",
    expires_at: dt.datetime | None = None,
) -> tuple[str, ApiKeyRecord]:
    """Mint a key. Returns ``(secret_to_show_once, record_to_store)``.

    The caller is expected to show the secret once and never again; only the record is
    persisted. That split is why this returns a tuple rather than storing anything itself.
    """
    if not roles:
        raise ValueError("An API key must have at least one role")
    prefix = secrets.token_hex(_PREFIX_LENGTH // 2)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    presented = f"cip_{prefix}_{secret}"
    record = ApiKeyRecord(
        key_id=str(uuid.uuid4()),
        prefix=prefix,
        secret_hash=_hash_secret(secret, pepper=pepper),
        tenant_id=tenant_id,
        roles=roles,
        display_name=display_name,
        expires_at=expires_at,
    )
    return presented, record


class ApiKeyStore:
    """Verifies presented keys against stored records.

    In-memory. A production store is a database table with the same shape; the verification
    logic is what matters and it lives here rather than in a query.
    """

    def __init__(self, *, pepper: str) -> None:
        if not pepper:
            raise ValueError("An API key pepper is required")
        self._pepper = pepper
        self._by_prefix: dict[str, list[ApiKeyRecord]] = {}

    def add(self, record: ApiKeyRecord) -> None:
        self._by_prefix.setdefault(record.prefix, []).append(record)

    def revoke(self, key_id: str) -> bool:
        from dataclasses import replace

        for prefix, records in self._by_prefix.items():
            for index, record in enumerate(records):
                if record.key_id == key_id:
                    self._by_prefix[prefix][index] = replace(record, revoked=True)
                    return True
        return False

    def authenticate(self, presented: str) -> Principal:
        """Identify the caller from a presented key.

        Every failure raises the same error with the same message. Distinguishing "no such
        key" from "wrong secret" from "expired" would tell an attacker which half of a guess
        was right.
        """
        # maxsplit=2, because `token_urlsafe` emits `-` and `_`: an unbounded split shatters
        # the secret at its own underscores and rejects a valid key. That failure is
        # *nondeterministic* — it depends on the random bytes — so roughly one minted key in
        # three would be permanently unusable with no pattern to it.
        parts = presented.split("_", 2)
        if len(parts) != 3 or parts[0] != "cip" or not parts[1] or not parts[2]:
            raise AuthenticationError("Invalid API key")

        _, prefix, secret = parts
        candidates = self._by_prefix.get(prefix, [])
        expected = _hash_secret(secret, pepper=self._pepper)

        for record in candidates:
            # Constant-time comparison: a byte-by-byte early exit is a slow but real oracle
            # for recovering the stored hash.
            if not hmac.compare_digest(record.secret_hash, expected):
                continue
            if not record.is_usable:
                raise AuthenticationError("Invalid API key")
            return Principal(
                principal_id=record.key_id,
                tenant_id=record.tenant_id,
                roles=record.roles,
                scopes=scopes_for_roles(record.roles),
                kind="api_key",
                display_name=record.display_name,
                expires_at=record.expires_at,
            )

        raise AuthenticationError("Invalid API key")


@runtime_checkable
class TokenVerifier(Protocol):
    """Verifies a bearer token and returns the principal it identifies.

    The seam an OIDC verifier implements. A JWT verifier that checks issuer, audience,
    expiry, and signature satisfies this; so does one that calls a provider's introspection
    endpoint.
    """

    def verify(self, token: str) -> Principal: ...


class StaticClaimsVerifier:
    """Maps pre-verified claims to a principal.

    Signature verification belongs to a library, not to this codebase — writing one is how
    ``alg: none`` bugs happen. This takes claims a verified token yielded and does the part
    that is genuinely ours: turning them into a tenant, roles, and scopes, and refusing
    anything malformed.
    """

    def __init__(self, *, required_issuer: str, required_audience: str) -> None:
        self._issuer = required_issuer
        self._audience = required_audience

    def principal_from_claims(self, claims: dict[str, Any]) -> Principal:
        if claims.get("iss") != self._issuer:
            raise AuthenticationError("Token issuer is not accepted")
        if self._audience not in _as_list(claims.get("aud")):
            raise AuthenticationError("Token audience is not accepted")

        raw_tenant = claims.get("tenant_id")
        if not raw_tenant:
            # No tenant means no isolation. Refusing is the only safe response; defaulting to
            # one would silently grant access to whichever tenant that was.
            raise AuthenticationError("Token carries no tenant")
        try:
            tenant_id = uuid.UUID(str(raw_tenant))
        except ValueError as exc:
            raise AuthenticationError("Token tenant is malformed") from exc

        roles = frozenset(Role(role) for role in _as_list(claims.get("roles")) if role in set(Role))
        if not roles:
            raise AuthenticationError("Token grants no recognised role")

        expires = claims.get("exp")
        expires_at = dt.datetime.fromtimestamp(float(expires), dt.UTC) if expires else None
        principal = Principal(
            principal_id=str(claims.get("sub", "unknown")),
            tenant_id=tenant_id,
            roles=roles,
            scopes=scopes_for_roles(roles),
            kind="jwt",
            expires_at=expires_at,
        )
        if principal.is_expired:
            raise AuthenticationError("Token has expired")
        return principal


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]
