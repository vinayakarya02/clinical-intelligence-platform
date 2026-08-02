"""Parsed-document artifact persistence (MongoDB).

Stores the full parser output — pages, blocks, layout kinds, per-page OCR confidence —
keyed by document. This is the artifact that makes reprocessing cheap: re-chunking after
a strategy change reads the stored parse instead of re-running OCR over a scanned
document, which is the dominant cost in the pipeline by a wide margin.

Writes are upserts on ``(tenant_id, document_id)``, so re-running the pipeline replaces
the artifact rather than accumulating one per run. Run *history* lives in
``ingestion_runs``; this collection holds current state only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from cip_core.db.mongo import MongoManager
from cip_core.logging import get_logger
from cip_core.tenancy import TenantContext
from cip_ingestion.domain import ParsedDocument

__all__ = ["ParsedArtifactRepository"]

_log = get_logger(__name__)


def _serialize(parsed: ParsedDocument) -> dict[str, Any]:
    """Convert a :class:`ParsedDocument` into a Mongo-storable document."""
    return {
        "parser_name": parsed.parser_name,
        "media_type": parsed.media_type,
        "page_count": parsed.page_count,
        "char_count": parsed.char_count,
        "ocr_page_count": parsed.ocr_page_count,
        "mean_ocr_confidence": parsed.mean_ocr_confidence,
        "properties": parsed.properties,
        "warnings": list(parsed.warnings),
        "pages": [
            {
                "page_number": page.page_number,
                "ocr_applied": page.ocr_applied,
                "ocr_confidence": page.ocr_confidence,
                "char_count": page.char_count,
                "blocks": [
                    {
                        "text": block.text,
                        "kind": str(block.kind),
                        "order": block.order,
                        "confidence": block.confidence,
                    }
                    for block in page.blocks
                ],
            }
            for page in parsed.pages
        ],
    }


class ParsedArtifactRepository:
    """Reads and writes parsed-document artifacts."""

    def __init__(self, mongo: MongoManager, *, collection_name: str = "parsed_documents") -> None:
        self._mongo = mongo
        self._collection_name = collection_name

    async def save(
        self,
        parsed: ParsedDocument,
        *,
        document_id: uuid.UUID,
        content_hash: str,
        context: TenantContext,
    ) -> None:
        collection = self._mongo.tenant_collection(self._collection_name, context.tenant_id)
        payload = {
            "document_id": str(document_id),
            "content_hash": content_hash,
            "created_at": dt.datetime.now(dt.UTC),
            **_serialize(parsed),
        }
        await collection.replace_one({"document_id": str(document_id)}, payload, upsert=True)
        _log.info(
            "artifact.persisted",
            document_id=str(document_id),
            parser=parsed.parser_name,
            page_count=parsed.page_count,
        )

    async def get(self, document_id: uuid.UUID, *, context: TenantContext) -> dict[str, Any] | None:
        collection = self._mongo.tenant_collection(self._collection_name, context.tenant_id)
        return await collection.find_one({"document_id": str(document_id)})

    async def delete(self, document_id: uuid.UUID, *, context: TenantContext) -> int:
        collection = self._mongo.tenant_collection(self._collection_name, context.tenant_id)
        return await collection.delete_many({"document_id": str(document_id)})
