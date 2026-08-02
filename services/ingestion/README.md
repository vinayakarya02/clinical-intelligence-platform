# Ingestion Service — Document Intelligence Pipeline

Phase 1 of the Clinical Intelligence Platform. Ingests clinical documents (PDF, DOCX,
plain text), parses them with layout awareness and OCR fallback, normalises and sections
the text, extracts metadata, chunks the content, gates it on data quality, and persists
the result.

**Scope boundary:** this service stops at persisted chunks. Embedding generation, vector
search, knowledge-graph construction, retrieval, and conversational AI are Phases 2+
([implementation-roadmap.md](../../docs/roadmap/implementation-roadmap.md)).

## Pipeline

```
                       ┌──────────────────────────────────────────┐
  upload / CLI ──────► │ 1. validate    size, media type (sniffed  │
                       │                vs declared), filename     │
                       ├──────────────────────────────────────────┤
                       │ 2. deduplicate SHA-256 content hash,      │
                       │                scoped to (tenant, source) │
                       ├──────────────────────────────────────────┤
                       │ 3. persist raw  object storage, before    │
                       │                 parsing — see below       │
                       ├──────────────────────────────────────────┤
                       │ 4. parse        PDF / DOCX / text,        │
                       │                 per-page OCR fallback     │
                       ├──────────────────────────────────────────┤
                       │ 5. normalize    ligatures, de-hyphenation,│
                       │                 header/footer removal     │
                       ├──────────────────────────────────────────┤
                       │ 6. sections     clinical section detection│
                       ├──────────────────────────────────────────┤
                       │ 7. metadata     type, date, language, PHI │
                       ├──────────────────────────────────────────┤
                       │ 8. chunk        section-aware, table-safe │
                       ├──────────────────────────────────────────┤
                       │ 9. quality      score + PASS/WARN/FAIL    │
                       ├──────────────────────────────────────────┤
                       │ 10. persist     chunks, artifact, run,    │
                       │                 quality report, audit     │
                       └──────────────────────────────────────────┘
```

Three ordering decisions carry weight:

**Raw bytes are stored before parsing.** A document that crashes a parser is still durably
retained and reprocessable once the parser is fixed. The alternative loses the only copy at
exactly the moment it becomes interesting.

**Everything after the raw write is one transaction.** A failure leaves the document
marked `failed` with no half-written chunks, rather than a document that looks ingested
but has partial content in retrieval.

**Quality failure and pipeline failure are different outcomes.** A `FAIL` verdict
*quarantines* the document — stored, auditable, withheld from retrieval, reprocessable.
An exception marks it `failed`. Conflating them would either discard recoverable documents
or admit unreadable ones.

## Module map

| Module | Responsibility |
|---|---|
| `domain.py` | Frozen value objects passed between stages |
| `validation.py` | Upload validation, magic-byte sniffing, content hashing |
| `parsers/` | `DocumentParser` protocol, PDF/DOCX/text parsers, OCR engine abstraction |
| `processing/normalization.py` | Unicode, de-hyphenation, page-furniture removal, page offsets |
| `processing/sections.py` | Clinical section vocabulary and heading detection |
| `processing/metadata.py` | Type classification, dates, language, PHI indicators |
| `processing/tokenization.py` | `TokenEstimator` protocol, clinical sentence segmentation |
| `processing/chunking.py` | `Chunker` protocol, `StructuralSemanticChunker` |
| `processing/quality.py` | Weighted quality checks and verdict |
| `processor.py` | Pure orchestration of stages 4–9 (no I/O) |
| `pipeline.py` | Full ETL with storage, database, and audit I/O |
| `repositories/` | All database access, tenant-scoped |
| `api/` | FastAPI app, auth skeleton, middleware, routes |
| `cli.py` | Batch ingestion, health, config, migrations |

`processor.py` is deliberately I/O-free: it turns bytes into chunks and a verdict with no
database, storage, or network. That is what lets the majority of the pipeline be tested in
milliseconds against real clinical documents.

## Running it

```bash
make install        # venv + dependencies
make services-up    # Postgres, MongoDB, Neo4j via docker compose
cp .env.example .env
make migrate        # apply the schema
make api            # http://localhost:8000/docs
```

Without `make` (Windows, or a minimal environment):

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m cip_ingestion.cli db upgrade
.venv/Scripts/python.exe -m uvicorn cip_ingestion.api.app:create_app --factory --port 8000
```

### CLI

```bash
cip ingest ./corpus --recursive --tenant-id <uuid> --source-system epic
cip ingest report.pdf --tenant-id <uuid> --source-system epic --document-type lab_report
cip health --json
cip config
cip db upgrade
```

Exit codes: `0` success, `1` partial failure (some documents failed), `2` usage error,
`3` dependency unavailable. A batch that ingests 99 of 100 documents exits `1` — it
neither passes silently nor looks like a total failure. Duplicates are not failures, so a
resumed batch over a partially-ingested directory exits `0`.

### API

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/documents` | Multipart upload. `201` ingested, `202` quarantined, `409` duplicate |
| `GET` | `/v1/documents` | List, filterable by ingestion status |
| `GET` | `/v1/documents/{id}` | Detail with chunk metadata, runs, quality report |
| `DELETE` | `/v1/documents/{id}` | Soft delete + purge scheduling |
| `GET` | `/health/live` | Liveness — touches no dependency |
| `GET` | `/health/ready` | Readiness — checks Postgres, Mongo, storage, Neo4j |

Errors are RFC 7807 problem documents (`application/problem+json`). No response returns
chunk text: chunk content is PHI, and this API reports *status*, it does not serve
documents.

## Testing

```bash
make test              # unit + API; no external services required
make test-integration  # requires live PostgreSQL (CIP_RUN_INTEGRATION=1)
make check             # lint + type-check + test
```

The suite runs persistence against real in-memory SQLite rather than mocks, so repository
code, transaction boundaries, constraint violations, and rollback behaviour are exercised
as real SQL. What SQLite structurally cannot cover — Row-Level Security policies, JSONB
operators — is covered by `tests/integration/`, which needs a real PostgreSQL server.

Tests requiring a Tesseract binary are marked `ocr` and skip automatically when it is
absent; OCR *routing* is still tested via a deterministic stub engine.

## Configuration

Environment-first, `CIP_`-prefixed, `__` for nesting (`CIP_POSTGRES__HOST`). See
[`.env.example`](../../.env.example).

`Settings` refuses to construct an unsafe deployed configuration — TLS below
`verify-full`, `debug=true`, console logging, disabled auth, local storage, or the
development-only HS256 auth mode all fail at startup rather than in a later audit. Local
and test environments get an ephemeral random JWT key when none is configured, so a fresh
checkout works without a `.env` while never shipping a fixed default secret.

## Known Phase 1 limitations

These are scope boundaries, not defects:

- **No ontology coding.** SNOMED/ICD/LOINC/RxNorm concept resolution needs licensed
  terminology data ([ontology-licensing.md](../../docs/legal/ontology-licensing.md)).
  Sections and metadata ship; concept coding does not — see
  [ADR-0005](../../docs/design/adr-0005-phase1-service-decomposition.md).
- **`documents.patient_id` has no foreign key.** The `patients` table arrives with the
  extraction workstream; enforcing the constraint against a non-existent table would
  reject every patient-linked upload.
- **OIDC verification is a seam, not an implementation.** Token *verification* ships;
  JWKS retrieval and IdP federation belong to the Identity service. `local_hs256` is
  development-only and rejected in deployed environments.
- **Chunking is structural, not embedding-based** — deliberate, with the interface for the
  Phase 0 algorithm already in place. See
  [ADR-0006](../../docs/design/adr-0006-phase1-chunking-strategy.md).
- **Neo4j is connected but unused.** Phase 1 establishes connectivity and health-checking
  so Phase 2 adds graph *logic* rather than also discovering connection problems.
