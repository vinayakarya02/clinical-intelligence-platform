"""MongoDB connectivity.

MongoDB stores the *parsed document artifact* — the page/block/layout structure a parser
produces before normalisation and chunking. That payload is deeply nested, varies by
source format, and is written once and read whole, which is a poor fit for the relational
schema and a natural fit for a document store. See ADR-0005 for why this does not
contradict ADR-0004 (which evaluated Atlas for the *chunk/vector* tier and kept Postgres).

Tenant isolation here is a mandatory ``tenant_id`` filter injected by
:class:`MongoManager.tenant_collection`, mirroring the RLS pattern: repositories cannot
obtain an unscoped collection handle by accident.
"""

from __future__ import annotations

import uuid
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo.errors import PyMongoError

from cip_core.config import MongoSettings
from cip_core.errors import DependencyUnavailableError
from cip_core.logging import get_logger

__all__ = ["MongoManager", "TenantScopedCollection"]

_log = get_logger(__name__)


class TenantScopedCollection:
    """A collection handle that injects ``tenant_id`` into every filter and document.

    Wrapping rather than exposing the raw collection is deliberate: it makes "forgot the
    tenant filter" impossible for the operations the application actually uses, instead
    of relying on every call site to remember.
    """

    def __init__(self, collection: AsyncIOMotorCollection, tenant_id: uuid.UUID) -> None:
        self._collection = collection
        self._tenant_id = str(tenant_id)

    def _scoped(self, query: dict[str, Any] | None = None) -> dict[str, Any]:
        scoped = dict(query or {})
        scoped["tenant_id"] = self._tenant_id
        return scoped

    async def insert_one(self, document: dict[str, Any]) -> str:
        payload = dict(document)
        payload["tenant_id"] = self._tenant_id
        result = await self._collection.insert_one(payload)
        return str(result.inserted_id)

    async def replace_one(
        self, query: dict[str, Any], document: dict[str, Any], *, upsert: bool = False
    ) -> int:
        payload = dict(document)
        payload["tenant_id"] = self._tenant_id
        result = await self._collection.replace_one(self._scoped(query), payload, upsert=upsert)
        return int(result.modified_count)

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return await self._collection.find_one(self._scoped(query))

    async def count_documents(self, query: dict[str, Any] | None = None) -> int:
        return int(await self._collection.count_documents(self._scoped(query)))

    async def delete_many(self, query: dict[str, Any]) -> int:
        result = await self._collection.delete_many(self._scoped(query))
        return int(result.deleted_count)


class MongoManager:
    """Owns the Motor client lifecycle."""

    def __init__(self, settings: MongoSettings) -> None:
        self._settings = settings
        self._client: AsyncIOMotorClient | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def database(self) -> AsyncIOMotorDatabase:
        if self._client is None:
            raise RuntimeError("MongoManager.connect() has not been called")
        return self._client[self._settings.database]

    async def connect(self) -> None:
        """Create the client. Idempotent.

        Motor connects lazily, so this does not prove reachability — call
        :meth:`health_check` for that.
        """
        if self._client is not None:
            return
        self._client = AsyncIOMotorClient(
            self._settings.uri.get_secret_value(),
            serverSelectionTimeoutMS=self._settings.server_selection_timeout_ms,
            uuidRepresentation="standard",
        )
        _log.info("mongo.connected", database=self._settings.database)

    async def disconnect(self) -> None:
        if self._client is None:
            return
        self._client.close()
        self._client = None
        _log.info("mongo.disconnected")

    def tenant_collection(self, name: str, tenant_id: uuid.UUID) -> TenantScopedCollection:
        """Return a tenant-scoped handle on ``name``."""
        return TenantScopedCollection(self.database[name], tenant_id)

    async def ensure_indexes(self) -> None:
        """Create the indexes the ingestion pipeline depends on. Idempotent.

        The unique index on ``(tenant_id, document_id)`` is what makes re-running the
        pipeline for a document an upsert rather than a duplicate-artifact accumulation.
        """
        collection = self.database[self._settings.parsed_documents_collection]
        await collection.create_index([("tenant_id", 1), ("document_id", 1)], unique=True)
        await collection.create_index([("tenant_id", 1), ("content_hash", 1)])
        await collection.create_index([("tenant_id", 1), ("created_at", -1)])
        _log.info("mongo.indexes_ensured", collection=self._settings.parsed_documents_collection)

    async def health_check(self) -> dict[str, Any]:
        if self._client is None:
            raise DependencyUnavailableError("MongoDB is not connected", dependency="mongo")
        try:
            result = await self._client.admin.command("ping")
        except PyMongoError as exc:
            raise DependencyUnavailableError(
                f"MongoDB health check failed: {type(exc).__name__}", dependency="mongo"
            ) from exc
        return {"status": "ok", "ping": float(result.get("ok", 0.0))}
