"""``cip_ingestion`` — the Phase 1 Document Intelligence service.

Ingests clinical documents and turns them into validated, quality-gated, chunked, and
persisted retrieval units.

Scope boundary: Phase 1 stops at persisted chunks. Embedding generation, vector indexing,
knowledge-graph construction, retrieval, and conversational AI are Phases 2+ and are
deliberately absent — see docs/roadmap/implementation-roadmap.md. The seams where they
attach (``SyncStateRepository`` queuing index work, the ``Chunker`` protocol, the
``TokenEstimator`` protocol) exist now so those phases extend the pipeline rather than
restructure it.
"""

from cip_ingestion.pipeline import IngestionPipeline, IngestionRequest, IngestionResult
from cip_ingestion.processor import DocumentProcessor, ProcessingResult
from cip_ingestion.version import PIPELINE_VERSION, SERVICE_VERSION

__all__ = [
    "PIPELINE_VERSION",
    "SERVICE_VERSION",
    "DocumentProcessor",
    "IngestionPipeline",
    "IngestionRequest",
    "IngestionResult",
    "ProcessingResult",
]
