"""Service and pipeline versioning.

``PIPELINE_VERSION`` is recorded on every ingestion run. It exists so a corpus can be
*selectively* reprocessed: after a chunking or OCR change, "re-ingest everything produced
by pipeline < X" is an indexed query, whereas without it the only options are reprocessing
the entire corpus or guessing which documents are stale.

It is versioned independently of the service version because they change for different
reasons — an API fix ships a new service version but leaves every existing document's
derived artifacts valid, and must not trigger a corpus-wide reprocess.

Bump ``PIPELINE_VERSION`` when a change alters the *output* of processing: parser
behaviour, normalisation rules, section vocabulary, chunking strategy, or quality
thresholds.
"""

from __future__ import annotations

__all__ = ["PIPELINE_VERSION", "SERVICE_VERSION"]

SERVICE_VERSION = "0.1.0"

PIPELINE_VERSION = "1.0.0"
