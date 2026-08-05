"""Object-storage abstraction for raw documents.

Two decisions worth stating up front, because they shape everything downstream:

**Keys are tenant-prefixed and content-addressed.** A key looks like
``tenants/<tenant_id>/documents/<yyyy>/<mm>/<sha256><ext>``. The tenant prefix is what
IAM policies scope against (ADR-0003: "per-tenant bucket prefix + IAM policy scoping, not
filename convention alone"), and content-addressing means re-uploading identical bytes
is a no-op rather than a second copy — the storage layer agrees with the duplicate
detection in the pipeline instead of contradicting it.

**Keys are constructed, never accepted.** :func:`build_object_key` is the only way to
produce one, and every backend validates keys against traversal before touching the
filesystem or bucket. A caller-supplied key would be an obvious path-traversal and
cross-tenant-read vector.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from cip_core.errors import ValidationFailedError

__all__ = [
    "ObjectStorage",
    "StoredObject",
    "build_object_key",
    "validate_object_key",
]

#: Conservative allowlist. Anything outside it — notably ``..`` segments, backslashes,
#: absolute paths, and control characters — is rejected rather than sanitised, because
#: silently rewriting a key would make two different inputs collide on one object.
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,511}$")

_EXTENSION_PATTERN = re.compile(r"^\.[A-Za-z0-9]{1,16}$")


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Result of a successful write."""

    key: str
    size_bytes: int
    content_type: str
    uri: str
    """Backend-qualified location (``file://`` or ``s3://``) recorded on the document row."""


def validate_object_key(key: str) -> str:
    """Return ``key`` if it is safe, otherwise raise :class:`ValidationFailedError`."""
    if not _KEY_PATTERN.match(key):
        raise ValidationFailedError("Object key contains illegal characters")
    if ".." in key or key.endswith("/") or "//" in key:
        raise ValidationFailedError("Object key contains illegal path segments")
    return key


def build_object_key(
    *,
    tenant_id: uuid.UUID,
    content_hash: str,
    extension: str = "",
    prefix: str = "documents",
    now: dt.datetime | None = None,
) -> str:
    """Build a tenant-prefixed, content-addressed object key.

    ``now`` is injectable so tests are not time-dependent; the date partition exists to
    keep any single storage prefix from accumulating unbounded objects, which matters for
    listing and lifecycle rules more than for reads.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise ValidationFailedError("content_hash must be a lowercase hex SHA-256 digest")
    if extension and not _EXTENSION_PATTERN.match(extension):
        raise ValidationFailedError("File extension is not a recognised form")

    stamp = now or dt.datetime.now(dt.UTC)
    key = (
        f"tenants/{tenant_id}/{prefix}/"
        f"{stamp.year:04d}/{stamp.month:02d}/{content_hash}{extension.lower()}"
    )
    return validate_object_key(key)


@runtime_checkable
class ObjectStorage(Protocol):
    """Raw-document storage backend.

    A ``Protocol`` rather than an ABC so tests can supply a lightweight in-memory double
    without inheriting production behaviour, and so a future backend (Azure Blob, GCS)
    needs no change here.
    """

    @property
    def backend_name(self) -> str: ...

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> StoredObject:
        """Write ``data`` at ``key``. Overwriting an identical key is a no-op by design."""
        ...

    async def get(self, key: str) -> bytes:
        """Read the object at ``key``. Raises :class:`~cip_core.errors.NotFoundError`."""
        ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> bool:
        """Delete ``key``. Returns False if it was already absent."""
        ...

    async def health_check(self) -> dict[str, object]: ...
