"""Neo4j connectivity.

Phase 1 deliberately establishes connectivity, session management, and health-checking
only. Knowledge-graph construction — entity resolution, community detection, the schema
in docs/database/graph-schema.md — is Phase 2 scope
(docs/roadmap/implementation-roadmap.md).

Building the connection layer now rather than in Phase 2 is intentional: it means the
deployment topology, health endpoint, and configuration surface are complete and
exercised from the start, so Phase 2 adds graph *logic* rather than also discovering
connection/lifecycle problems.

The read/write session split below matters for Phase 2, where read replicas serve
traversal queries (docs/deployment/deployment-architecture.md §2). Establishing it now
avoids retrofitting every query later.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from cip_core.config import Neo4jSettings
from cip_core.errors import DependencyUnavailableError
from cip_core.logging import get_logger

__all__ = ["Neo4jManager"]

_log = get_logger(__name__)


class Neo4jManager:
    """Owns the async Neo4j driver lifecycle."""

    def __init__(self, settings: Neo4jSettings) -> None:
        self._settings = settings
        self._driver: AsyncDriver | None = None

    @property
    def is_connected(self) -> bool:
        return self._driver is not None

    @property
    def driver(self) -> AsyncDriver:
        if self._driver is None:
            raise RuntimeError("Neo4jManager.connect() has not been called")
        return self._driver

    async def connect(self) -> None:
        """Create the driver. Idempotent. Does not verify reachability."""
        if self._driver is not None:
            return
        self._driver = AsyncGraphDatabase.driver(
            self._settings.uri,
            auth=(self._settings.user, self._settings.password.get_secret_value()),
            connection_timeout=self._settings.connection_timeout_seconds,
        )
        _log.info("neo4j.connected", uri=self._settings.uri, database=self._settings.database)

    async def disconnect(self) -> None:
        if self._driver is None:
            return
        await self._driver.close()
        self._driver = None
        _log.info("neo4j.disconnected")

    @asynccontextmanager
    async def read_session(self) -> AsyncIterator[AsyncSession]:
        """Session routed to a read replica where the deployment provides one."""
        async with self.driver.session(
            database=self._settings.database, default_access_mode="READ"
        ) as session:
            yield session

    @asynccontextmanager
    async def write_session(self) -> AsyncIterator[AsyncSession]:
        """Session routed to the cluster leader."""
        async with self.driver.session(
            database=self._settings.database, default_access_mode="WRITE"
        ) as session:
            yield session

    async def health_check(self) -> dict[str, Any]:
        if self._driver is None:
            raise DependencyUnavailableError("Neo4j is not connected", dependency="neo4j")
        try:
            await self._driver.verify_connectivity()
            async with self.read_session() as session:
                result = await session.run("RETURN 1 AS ok")
                record = await result.single()
        except (Neo4jError, ServiceUnavailable, OSError) as exc:
            raise DependencyUnavailableError(
                f"Neo4j health check failed: {type(exc).__name__}", dependency="neo4j"
            ) from exc
        return {"status": "ok", "probe": int(record["ok"]) if record else 0}
