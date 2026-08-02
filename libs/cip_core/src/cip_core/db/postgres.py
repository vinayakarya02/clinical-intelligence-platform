"""PostgreSQL connectivity.

The important detail here is :meth:`PostgresManager.tenant_session`. ADR-0003 makes
Row-Level Security the enforced floor for tenant isolation, and RLS policies in
docs/database/postgres-schema.sql read ``current_setting('app.tenant_id')``. That
setting has to be applied inside the same transaction as the query, or the policy
evaluates against an empty setting and the query silently returns nothing (or, worse,
everything, if a policy were ever written permissively).

So the session helper sets it transactionally and the repositories only ever accept a
session produced by it. Getting a session without a tenant is possible but explicit
(:meth:`session`), and is used only for platform-level tables that carry no PHI.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cip_core.config import PostgresSettings
from cip_core.errors import DependencyUnavailableError
from cip_core.logging import get_logger
from cip_core.tenancy import TenantContext

__all__ = ["PostgresManager"]

_log = get_logger(__name__)

# asyncpg takes `ssl` rather than libpq's `sslmode`; map the configured mode onto the
# connect argument. 'prefer' is intentionally treated as "try TLS, tolerate plaintext",
# which asyncpg expresses as ssl=False + server negotiation, so it is only used locally —
# Settings refuses anything but 'verify-full' in deployed environments.
_SSL_MODE_TO_ASYNCPG: dict[str, Any] = {
    "disable": False,
    "prefer": None,
    "require": True,
    "verify-ca": "verify-ca",
    "verify-full": "verify-full",
}


class PostgresManager:
    """Owns the async engine and session factory for the operational store."""

    def __init__(self, settings: PostgresSettings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @classmethod
    def from_engine(
        cls, engine: AsyncEngine, settings: PostgresSettings | None = None
    ) -> PostgresManager:
        """Wrap an externally-created engine.

        The seam that lets the test suite exercise the *real* session and transaction
        logic — including tenant scoping and rollback behaviour — against an in-process
        database, instead of asserting against a mock that cannot disagree with it.
        """
        manager = cls(settings or PostgresSettings())
        manager._engine = engine
        manager._session_factory = async_sessionmaker(
            bind=engine, expire_on_commit=False, autoflush=False
        )
        return manager

    @property
    def _supports_rls_session_var(self) -> bool:
        """Whether the bound database understands ``set_config``.

        ``set_config`` is PostgreSQL-specific. Guarding on the dialect keeps the manager
        usable against another database in development without every transaction failing
        on an unknown function — the RLS policies themselves are PostgreSQL-only anyway,
        and repository-level tenant filtering still applies.
        """
        return self._engine is not None and self._engine.dialect.name == "postgresql"

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("PostgresManager.connect() has not been called")
        return self._engine

    @property
    def is_connected(self) -> bool:
        return self._engine is not None

    async def connect(self) -> None:
        """Create the engine and session factory. Idempotent."""
        if self._engine is not None:
            return

        connect_args: dict[str, Any] = {
            "command_timeout": self._settings.command_timeout_seconds,
            # Disabling the prepared-statement cache keeps the engine compatible with a
            # transaction-pooling proxy in front of Postgres (ADR-0003's pooler tier).
            "statement_cache_size": self._settings.statement_cache_size,
        }
        ssl_value = _SSL_MODE_TO_ASYNCPG[self._settings.ssl_mode]
        if ssl_value is not None:
            connect_args["ssl"] = ssl_value

        self._engine = create_async_engine(
            self._settings.dsn(),
            pool_size=self._settings.pool_size,
            max_overflow=self._settings.max_overflow,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        self._session_factory = async_sessionmaker(
            bind=self._engine, expire_on_commit=False, autoflush=False
        )
        _log.info(
            "postgres.connected",
            host=self._settings.host,
            database=self._settings.database,
            ssl_mode=self._settings.ssl_mode,
        )

    async def disconnect(self) -> None:
        if self._engine is None:
            return
        await self._engine.dispose()
        self._engine = None
        self._session_factory = None
        _log.info("postgres.disconnected")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session with no tenant scoping.

        Only for platform-level tables (``platform.tenants``, migrations) that hold no
        PHI. Tenant-scoped work must use :meth:`tenant_session`.
        """
        if self._session_factory is None:
            raise RuntimeError("PostgresManager.connect() has not been called")
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def tenant_session(self, context: TenantContext) -> AsyncIterator[AsyncSession]:
        """Yield a session with the RLS tenant variable applied for the transaction.

        ``set_config(..., true)`` scopes the setting to the transaction, so a pooled
        connection returned to the pool cannot leak one request's tenant into the next.
        """
        if self._session_factory is None:
            raise RuntimeError("PostgresManager.connect() has not been called")
        async with self._session_factory() as session:
            try:
                if self._supports_rls_session_var:
                    await session.execute(
                        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                        {"tenant_id": str(context.tenant_id)},
                    )
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def health_check(self) -> dict[str, Any]:
        """Return a health payload; raise :class:`DependencyUnavailableError` if down.

        The probe is ``SELECT 1`` — a portable round-trip that proves the connection pool
        can hand out a working connection, which is the only thing readiness needs to
        know. Server version is reported as supplementary detail and its absence never
        fails the check: a probe that depends on dialect-specific SQL reports a healthy
        database as down the moment it runs anywhere unexpected.
        """
        if self._engine is None:
            raise DependencyUnavailableError("PostgreSQL is not connected", dependency="postgres")
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                version: str | None = None
                if self._engine.dialect.name == "postgresql":
                    result = await conn.execute(text("SELECT version()"))
                    version = str(result.scalar_one()).split(",")[0]
        except SQLAlchemyError as exc:
            raise DependencyUnavailableError(
                f"PostgreSQL health check failed: {type(exc).__name__}", dependency="postgres"
            ) from exc

        payload: dict[str, Any] = {"status": "ok", "dialect": self._engine.dialect.name}
        if version is not None:
            payload["version"] = version
        return payload

    async def current_tenant_setting(self, session: AsyncSession) -> uuid.UUID | None:
        """Read back the RLS tenant setting. Used by tests and diagnostics."""
        result = await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
        raw = result.scalar_one_or_none()
        return uuid.UUID(raw) if raw else None
