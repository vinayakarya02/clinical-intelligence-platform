"""Local filesystem storage backend.

Development and test only — ``Settings`` refuses this backend in deployed environments
because a pod-local filesystem is neither durable nor encrypted at rest.

Blocking file I/O is dispatched to a worker thread so the async event loop is never
stalled by a slow disk. That matters even locally: the ingestion API awaits a write on
the request path, and a blocking write would serialise every concurrent upload.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from cip_core.errors import NotFoundError, ValidationFailedError
from cip_core.logging import get_logger
from cip_core.storage.base import StoredObject, validate_object_key

__all__ = ["LocalFilesystemStorage"]

_log = get_logger(__name__)


class LocalFilesystemStorage:
    """Stores objects under a root directory, one file per key."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def backend_name(self) -> str:
        return "local"

    @property
    def root(self) -> Path:
        return self._root

    def _resolve(self, key: str) -> Path:
        """Map a validated key to an absolute path inside the root.

        The containment re-check after resolution is not redundant with key validation:
        it also catches a symlink inside the root pointing outside it, which key
        validation cannot see.
        """
        validate_object_key(key)
        candidate = (self._root / key).resolve()
        if not candidate.is_relative_to(self._root):
            raise ValidationFailedError("Resolved object path escapes the storage root")
        return candidate

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> StoredObject:
        path = self._resolve(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary sibling and replace, so a crash mid-write cannot leave
            # a truncated object that later reads would treat as a complete document.
            temp = path.with_suffix(path.suffix + ".partial")
            temp.write_bytes(data)
            temp.replace(path)

        await asyncio.to_thread(_write)
        _log.debug("storage.put", backend=self.backend_name, key=key, size_bytes=len(data))
        return StoredObject(
            key=key,
            size_bytes=len(data),
            content_type=content_type,
            uri=path.as_uri(),
        )

    async def get(self, key: str) -> bytes:
        path = self._resolve(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise NotFoundError(f"Object not found: {key}") from exc

    async def exists(self, key: str) -> bool:
        path = self._resolve(key)
        return await asyncio.to_thread(path.is_file)

    async def delete(self, key: str) -> bool:
        path = self._resolve(key)

        def _delete() -> bool:
            if not path.is_file():
                return False
            path.unlink()
            return True

        return await asyncio.to_thread(_delete)

    async def health_check(self) -> dict[str, Any]:
        """Prove the root is writable, not merely present.

        A read-only mount is a common failure that an existence check would miss.
        """
        probe = self._root / ".cip-health"

        def _probe() -> None:
            probe.write_bytes(b"ok")
            probe.unlink()

        await asyncio.to_thread(_probe)
        return {"status": "ok", "backend": self.backend_name, "root": str(self._root)}
