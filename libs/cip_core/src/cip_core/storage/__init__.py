"""Object storage for raw documents."""

from __future__ import annotations

from cip_core.config import StorageBackend, StorageSettings
from cip_core.storage.base import (
    ObjectStorage,
    StoredObject,
    build_object_key,
    validate_object_key,
)
from cip_core.storage.local import LocalFilesystemStorage
from cip_core.storage.s3 import S3Storage

__all__ = [
    "LocalFilesystemStorage",
    "ObjectStorage",
    "S3Storage",
    "StoredObject",
    "build_object_key",
    "create_storage",
    "validate_object_key",
]


def create_storage(settings: StorageSettings) -> ObjectStorage:
    """Build the configured storage backend.

    ``StorageSettings`` has already validated that an S3 bucket is present when the S3
    backend is selected, so this function does not re-check it.
    """
    if settings.backend is StorageBackend.LOCAL:
        return LocalFilesystemStorage(settings.local_root)
    return S3Storage(
        bucket=settings.s3_bucket or "",
        region=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
    )
