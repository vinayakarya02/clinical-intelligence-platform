"""Object-storage tests.

Key construction and path containment carry the security weight here: a caller-influenced
key is a cross-tenant read, and an escaping path is arbitrary filesystem access.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest

from cip_core.errors import NotFoundError, ValidationFailedError
from cip_core.storage import build_object_key, validate_object_key
from cip_core.storage.local import LocalFilesystemStorage

_HASH = "a" * 64


class TestObjectKeyConstruction:
    def test_key_is_tenant_prefixed_and_content_addressed(self, tenant_id: uuid.UUID) -> None:
        key = build_object_key(
            tenant_id=tenant_id,
            content_hash=_HASH,
            extension=".pdf",
            now=dt.datetime(2026, 3, 14, tzinfo=dt.UTC),
        )
        assert key == f"tenants/{tenant_id}/documents/2026/03/{_HASH}.pdf"

    def test_identical_content_yields_an_identical_key(self, tenant_id: uuid.UUID) -> None:
        now = dt.datetime(2026, 3, 14, tzinfo=dt.UTC)
        first = build_object_key(tenant_id=tenant_id, content_hash=_HASH, now=now)
        second = build_object_key(tenant_id=tenant_id, content_hash=_HASH, now=now)
        assert first == second

    def test_different_tenants_never_share_a_key(
        self, tenant_id: uuid.UUID, other_tenant_id: uuid.UUID
    ) -> None:
        now = dt.datetime(2026, 3, 14, tzinfo=dt.UTC)
        assert build_object_key(
            tenant_id=tenant_id, content_hash=_HASH, now=now
        ) != build_object_key(tenant_id=other_tenant_id, content_hash=_HASH, now=now)

    @pytest.mark.parametrize("bad_hash", ["", "xyz", "A" * 64, "a" * 63, "a" * 65])
    def test_malformed_hashes_are_rejected(self, tenant_id: uuid.UUID, bad_hash: str) -> None:
        with pytest.raises(ValidationFailedError):
            build_object_key(tenant_id=tenant_id, content_hash=bad_hash)

    @pytest.mark.parametrize("extension", ["pdf", ".p df", ".../etc", ".{}"])
    def test_malformed_extensions_are_rejected(self, tenant_id: uuid.UUID, extension: str) -> None:
        with pytest.raises(ValidationFailedError):
            build_object_key(tenant_id=tenant_id, content_hash=_HASH, extension=extension)


class TestKeyValidation:
    @pytest.mark.parametrize(
        "key",
        [
            "../etc/passwd",
            "tenants/../../secret",
            "tenants//double",
            "tenants/x/",
            "/absolute/path",
            "tenants/x\\windows",
            "",
        ],
    )
    def test_traversal_and_malformed_keys_are_rejected(self, key: str) -> None:
        with pytest.raises(ValidationFailedError):
            validate_object_key(key)

    def test_well_formed_key_is_accepted(self) -> None:
        assert validate_object_key("tenants/abc/documents/2026/03/file.pdf")


class TestLocalFilesystemStorage:
    async def test_round_trip(self, tmp_path: Path) -> None:
        storage = LocalFilesystemStorage(tmp_path)
        key = "tenants/t/documents/2026/03/doc.txt"

        stored = await storage.put(key, b"clinical content", content_type="text/plain")
        assert stored.size_bytes == len(b"clinical content")
        assert stored.key == key
        assert await storage.get(key) == b"clinical content"
        assert await storage.exists(key)

    async def test_get_missing_object_raises_not_found(self, tmp_path: Path) -> None:
        storage = LocalFilesystemStorage(tmp_path)
        with pytest.raises(NotFoundError):
            await storage.get("tenants/t/documents/2026/03/absent.txt")

    async def test_delete_reports_whether_anything_was_removed(self, tmp_path: Path) -> None:
        storage = LocalFilesystemStorage(tmp_path)
        key = "tenants/t/documents/2026/03/doc.txt"
        await storage.put(key, b"x")
        assert await storage.delete(key) is True
        assert await storage.delete(key) is False

    async def test_overwrite_is_atomic_and_leaves_no_partial_file(self, tmp_path: Path) -> None:
        storage = LocalFilesystemStorage(tmp_path)
        key = "tenants/t/documents/2026/03/doc.txt"
        await storage.put(key, b"first")
        await storage.put(key, b"second-and-longer")

        assert await storage.get(key) == b"second-and-longer"
        assert not list(tmp_path.rglob("*.partial")), "temporary write files must not survive"

    async def test_path_traversal_cannot_escape_the_root(self, tmp_path: Path) -> None:
        storage = LocalFilesystemStorage(tmp_path / "root")
        with pytest.raises(ValidationFailedError):
            await storage.put("../escaped.txt", b"nope")

    async def test_health_check_proves_the_root_is_writable(self, tmp_path: Path) -> None:
        storage = LocalFilesystemStorage(tmp_path)
        result = await storage.health_check()
        assert result["status"] == "ok"
        assert not list(tmp_path.glob(".cip-health")), "probe file must be cleaned up"
