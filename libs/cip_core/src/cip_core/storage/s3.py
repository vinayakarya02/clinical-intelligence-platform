"""S3-compatible storage backend.

``boto3`` is an optional dependency (``pip install '.[s3]'``) and is imported lazily, so
a local or on-prem deployment that never uses S3 does not carry the dependency. The
import error is translated into a configuration error with an actionable message rather
than surfacing as a bare ``ModuleNotFoundError`` at first upload.

boto3 is synchronous; calls are dispatched to worker threads. An async-native client
(aioboto3) was considered and rejected for Phase 1: the ingestion path makes one object
write per document, so thread-pool overhead is irrelevant next to parsing cost, and
boto3 is the better-supported client for the retry/credential behaviour that actually
matters in production.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from cip_core.errors import ConfigurationError, DependencyUnavailableError, NotFoundError
from cip_core.logging import get_logger
from cip_core.storage.base import StoredObject, validate_object_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-only import from the optional boto3 stubs package; absent unless the `s3`
    # extra and its stubs are installed, which is why it is suppressed rather than
    # required.
    from mypy_boto3_s3.client import S3Client  # pyright: ignore[reportMissingImports]
else:
    S3Client = Any

__all__ = ["S3Storage"]

_log = get_logger(__name__)


class S3Storage:
    """Stores objects in an S3 (or S3-compatible) bucket."""

    def __init__(
        self,
        bucket: str,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        client: S3Client | None = None,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url
        self._client = client or self._build_client()

    def _build_client(self) -> S3Client:
        try:
            # Optional dependency: installed via the `s3` extra. The ModuleNotFoundError
            # below is the supported path when it is absent.
            import boto3  # pyright: ignore[reportMissingImports]
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on install extras
            raise ConfigurationError(
                "S3 storage backend requires the 's3' extra: pip install '.[s3]'"
            ) from exc
        return boto3.client("s3", region_name=self._region, endpoint_url=self._endpoint_url)

    @property
    def backend_name(self) -> str:
        return "s3"

    @property
    def bucket(self) -> str:
        return self._bucket

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> StoredObject:
        validate_object_key(key)

        def _put() -> None:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                # Server-side encryption is mandatory for PHI at rest
                # (docs/architecture/06-security-compliance.md §9). Set per object rather
                # than relying on a bucket default so a misconfigured bucket cannot
                # silently store plaintext.
                ServerSideEncryption="AES256",
            )

        await asyncio.to_thread(_put)
        _log.debug("storage.put", backend=self.backend_name, key=key, size_bytes=len(data))
        return StoredObject(
            key=key,
            size_bytes=len(data),
            content_type=content_type,
            uri=f"s3://{self._bucket}/{key}",
        )

    async def get(self, key: str) -> bytes:
        validate_object_key(key)

        def _get() -> bytes:
            try:
                response = self._client.get_object(Bucket=self._bucket, Key=key)
            except self._client.exceptions.NoSuchKey as exc:
                raise NotFoundError(f"Object not found: {key}") from exc
            body: bytes = response["Body"].read()
            return body

        return await asyncio.to_thread(_get)

    async def exists(self, key: str) -> bool:
        validate_object_key(key)

        def _head() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
            except Exception:
                return False
            return True

        return await asyncio.to_thread(_head)

    async def delete(self, key: str) -> bool:
        validate_object_key(key)
        if not await self.exists(key):
            return False

        def _delete() -> None:
            self._client.delete_object(Bucket=self._bucket, Key=key)

        await asyncio.to_thread(_delete)
        return True

    async def health_check(self) -> dict[str, Any]:
        def _head_bucket() -> None:
            self._client.head_bucket(Bucket=self._bucket)

        try:
            await asyncio.to_thread(_head_bucket)
        except Exception as exc:
            raise DependencyUnavailableError(
                f"S3 health check failed: {type(exc).__name__}", dependency="storage"
            ) from exc
        return {"status": "ok", "backend": self.backend_name, "bucket": self._bucket}
