"""Audit-log persistence with hash chaining.

The chain is per tenant, so one tenant's write volume cannot invalidate another's chain
and verification can run per tenant without reading the whole table.

Correctness depends on serialising appends within a tenant: two concurrent writers that
both read the same ``prev_hash`` would produce a fork that verification reports as
tampering. The row lock in :meth:`AuditRepository.append` is what prevents that, and it is
the reason appends take the caller's transaction rather than opening their own.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cip_core.audit import GENESIS_HASH, AuditRecord, compute_row_hash
from cip_core.logging import get_logger
from cip_core.models import AuditLog

__all__ = ["AuditRepository"]

_log = get_logger(__name__)


class AuditRepository:
    """Appends tamper-evident audit entries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, record: AuditRecord) -> AuditLog:
        """Append ``record`` to its tenant's chain.

        Takes ``FOR UPDATE`` on the tenant's most recent row so concurrent appends
        serialise. Under PostgreSQL this blocks the second writer only until the first
        commits, which is acceptable: audit appends are small, and a correct chain is
        worth more than concurrent audit throughput.
        """
        stmt = (
            select(AuditLog)
            .where(AuditLog.tenant_id == record.tenant_id)
            .order_by(AuditLog.audit_id.desc())
            .limit(1)
            .with_for_update()
        )
        previous = (await self._session.execute(stmt)).scalar_one_or_none()
        prev_hash = previous.row_hash if previous is not None else GENESIS_HASH

        row = AuditLog(
            tenant_id=record.tenant_id,
            actor_user_id=record.actor_user_id,
            actor_service=record.actor_service,
            action=record.action,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            phi_accessed=record.phi_accessed,
            request_context=record.request_context,
            prev_hash=prev_hash,
            row_hash=compute_row_hash(record, prev_hash),
            occurred_at=record.occurred_at,
        )
        self._session.add(row)
        await self._session.flush()
        _log.debug(
            "audit.appended",
            action=record.action,
            resource_type=record.resource_type,
            phi_accessed=record.phi_accessed,
        )
        return row

    async def load_chain(self, tenant_id: uuid.UUID, *, limit: int = 1000) -> list[AuditLog]:
        """Load a tenant's chain in append order, for verification.

        Ordered by ``audit_id`` — the database-assigned sequence — not by
        ``occurred_at``, so the read order always matches the order the rows were
        chained in. See the ``audit_id`` note on the model.
        """
        stmt = (
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.audit_id.asc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())
