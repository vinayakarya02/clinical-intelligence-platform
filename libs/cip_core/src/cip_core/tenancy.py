"""Tenant and actor context.

ADR-0003 requires that every store-level query carry a ``(tenant_id, actor_scopes)``
context, with no code path exempted. :class:`TenantContext` is that object, and it is a
required argument on every repository method in this codebase rather than an ambient
global — a missing tenant scope is then a type error at the call site instead of a
runtime PHI leak.

The ambient contextvar below exists only for logging and for the Postgres RLS session
variable. Application code reads the tenant from the explicitly-passed context.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum

from cip_core.errors import AuthorizationError

__all__ = ["Role", "TenantContext", "current_tenant_context", "set_current_tenant_context"]


class Role(StrEnum):
    """Coarse RBAC roles (docs/architecture/06-security-compliance.md §3).

    ``EMERGENCY_ACCESS`` backs the break-glass flow; it is never granted implicitly.
    """

    ADMIN = "admin"
    CLINICIAN = "clinician"
    ANALYST = "analyst"
    PHARMACOVIGILANCE_REVIEWER = "pharmacovigilance_reviewer"
    VIEWER = "viewer"
    EMERGENCY_ACCESS = "emergency_access"
    SERVICE = "service"
    """Machine actor for pipeline/CLI operations that run without an interactive user."""


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Authenticated caller identity, scoped to one tenant.

    Frozen so a downstream layer cannot widen its own scope after an authorization check
    has already passed.
    """

    tenant_id: uuid.UUID
    actor_id: str
    roles: frozenset[Role] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)
    request_id: str | None = None

    @classmethod
    def for_service(
        cls, tenant_id: uuid.UUID, *, actor_id: str = "system", request_id: str | None = None
    ) -> TenantContext:
        """Build a machine context for CLI/pipeline execution.

        Deliberately does not grant ``ADMIN``: batch ingestion needs write access to its
        own tenant, not administrative authority over it.
        """
        return cls(
            tenant_id=tenant_id,
            actor_id=actor_id,
            roles=frozenset({Role.SERVICE}),
            scopes=frozenset({"documents:write", "documents:read"}),
            request_id=request_id,
        )

    @property
    def is_admin(self) -> bool:
        return Role.ADMIN in self.roles

    def has_any_role(self, *roles: Role) -> bool:
        return bool(self.roles.intersection(roles))

    def require_scope(self, scope: str) -> None:
        """Raise :class:`AuthorizationError` unless the caller holds ``scope``.

        Admins bypass scope checks; every other role must hold the scope explicitly.
        """
        if self.is_admin or scope in self.scopes:
            return
        raise AuthorizationError(f"Caller lacks required scope '{scope}'")

    def require_tenant(self, tenant_id: uuid.UUID) -> None:
        """Raise unless ``tenant_id`` matches the caller's tenant.

        The last line of defence for the post-retrieval re-check in
        docs/architecture/02-rag-hybrid-retrieval.md §2.2.
        """
        if tenant_id != self.tenant_id:
            raise AuthorizationError("Resource belongs to a different tenant")


_current_context: ContextVar[TenantContext | None] = ContextVar("cip_tenant_context", default=None)


def set_current_tenant_context(context: TenantContext | None) -> None:
    """Bind the ambient tenant context (used for logging and the RLS session variable)."""
    _current_context.set(context)


def current_tenant_context() -> TenantContext | None:
    """Return the ambient tenant context, if one is bound."""
    return _current_context.get()
