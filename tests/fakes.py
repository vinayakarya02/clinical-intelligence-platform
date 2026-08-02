"""In-process test doubles.

These implement the same protocols as the production collaborators, so the code under
test takes its real path. They are doubles for things that are genuinely external
processes (MongoDB, the Tesseract binary), not for the application's own logic — that is
always exercised for real.
"""

from __future__ import annotations

import uuid
from typing import Any

from cip_core.errors import DependencyUnavailableError
from cip_core.storage.base import StoredObject, validate_object_key
from cip_ingestion.parsers.ocr import OcrPageResult

__all__ = [
    "FailingStorage",
    "FakeMongoManager",
    "InMemoryStorage",
    "StubOcrEngine",
]


class _FakeCollection:
    """Tenant-scoped collection backed by a dict, mirroring ``TenantScopedCollection``."""

    def __init__(self, store: dict[tuple[str, str], dict[str, Any]], tenant_id: uuid.UUID) -> None:
        self._store = store
        self._tenant_id = str(tenant_id)

    async def insert_one(self, document: dict[str, Any]) -> str:
        key = (self._tenant_id, str(document.get("document_id")))
        self._store[key] = {**document, "tenant_id": self._tenant_id}
        return str(uuid.uuid4())

    async def replace_one(
        self, query: dict[str, Any], document: dict[str, Any], *, upsert: bool = False
    ) -> int:
        key = (self._tenant_id, str(query.get("document_id")))
        existed = key in self._store
        if existed or upsert:
            self._store[key] = {**document, "tenant_id": self._tenant_id}
        return 1 if existed else 0

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return self._store.get((self._tenant_id, str(query.get("document_id"))))

    async def count_documents(self, query: dict[str, Any] | None = None) -> int:
        return sum(1 for tenant, _ in self._store if tenant == self._tenant_id)

    async def delete_many(self, query: dict[str, Any]) -> int:
        key = (self._tenant_id, str(query.get("document_id")))
        return 1 if self._store.pop(key, None) is not None else 0


class FakeMongoManager:
    """Substitute for :class:`~cip_core.db.mongo.MongoManager`.

    Keys documents by ``(tenant_id, document_id)`` exactly as the real unique index does,
    so a test that accidentally crosses tenants fails here the same way it would in
    production.
    """

    def __init__(self, *, healthy: bool = True) -> None:
        self._store: dict[tuple[str, str], dict[str, Any]] = {}
        self._healthy = healthy
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    def tenant_collection(self, name: str, tenant_id: uuid.UUID) -> _FakeCollection:
        return _FakeCollection(self._store, tenant_id)

    async def ensure_indexes(self) -> None:
        return None

    async def health_check(self) -> dict[str, Any]:
        if not self._healthy:
            raise DependencyUnavailableError("fake mongo is down", dependency="mongo")
        return {"status": "ok", "ping": 1.0}

    @property
    def documents(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Direct access for assertions."""
        return self._store


class InMemoryStorage:
    """Object storage backed by a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls = 0

    @property
    def backend_name(self) -> str:
        return "memory"

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> StoredObject:
        validate_object_key(key)
        self.put_calls += 1
        self.objects[key] = data
        return StoredObject(
            key=key, size_bytes=len(data), content_type=content_type, uri=f"memory://{key}"
        )

    async def get(self, key: str) -> bytes:
        from cip_core.errors import NotFoundError

        try:
            return self.objects[key]
        except KeyError as exc:
            raise NotFoundError(f"Object not found: {key}") from exc

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def delete(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None

    async def health_check(self) -> dict[str, Any]:
        return {"status": "ok", "backend": self.backend_name}


class FailingStorage(InMemoryStorage):
    """Storage whose writes always fail, for error-path tests."""

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> StoredObject:
        raise OSError("simulated storage failure")

    async def health_check(self) -> dict[str, Any]:
        raise DependencyUnavailableError("simulated storage outage", dependency="storage")


class StubOcrEngine:
    """Deterministic OCR engine.

    Returns fixed text so OCR-routing behaviour can be asserted without depending on a
    Tesseract installation or on real recognition accuracy.
    """

    def __init__(
        self,
        *,
        available: bool = True,
        text: str = "OCR RECOVERED TEXT\nScanned page content.",
        confidence: float | None = 0.91,
    ) -> None:
        self._available = available
        self._text = text
        self._confidence = confidence
        self.calls = 0

    @property
    def name(self) -> str:
        return "stub"

    def is_available(self) -> bool:
        return self._available

    def recognize_page(self, image: Any, *, language: str) -> OcrPageResult:
        self.calls += 1
        return OcrPageResult(text=self._text, confidence=self._confidence, engine=self.name)
