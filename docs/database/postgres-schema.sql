-- =============================================================================
-- Clinical Intelligence Platform — Operational Store Schema (PostgreSQL)
-- Phase 0 design artifact. Not yet applied to any environment.
-- Revised after the Phase 0 architecture review — see docs/design/phase-0-architecture-review.md
-- (findings D1-D17). Superseded issues from the original draft are not repeated
-- here as comments; see the review doc for the before/after rationale.
--
-- Isolation model: see docs/design/adr-0003-multi-tenancy-model.md
--   - Large/regulated tenants: this schema is deployed once PER TENANT SCHEMA
--     (e.g. `tenant_acmehealth.patients`), via the same DDL below.
--   - Smaller/shared tenants: deployed once in a shared schema with Row-Level
--     Security enforcing tenant_id scoping (policies at the bottom of this file).
--   - Connection pooling for schema-per-tenant mode uses a tenant-sharded pooler
--     tier (N PgBouncer instances, each session-pooling a fixed shard of tenant
--     schemas) rather than a single transaction-pooled cluster — see
--     ADR-0003 Consequences for the sizing rationale. This is a real constraint
--     on how many tenant schemas one Postgres instance can practically serve;
--     see docs/nfr.md for the scale ceiling this implies.
--
-- Encryption: every connection MUST use `sslmode=verify-full` (enforced at the
-- pooler/proxy, not just documented here). Disk-level encryption uses the
-- cloud provider's KMS-managed keys (see docs/architecture/06-security-compliance.md
-- §9). Columns flagged "PHI, column-encrypted" below additionally use
-- application-layer envelope encryption (not pgcrypto pgp_sym alone — key
-- rotation is managed by the platform KMS integration, a Phase 1 concern)
-- because they are the fields most likely to appear in an unfiltered SELECT *
-- during debugging and warrant a second layer beyond disk/TLS encryption.
--
-- Backup/DR: RPO/RTO targets and backup cadence are defined in
-- docs/operations/sla-dr.md — not repeated here to avoid drift between two
-- copies of the same numbers.
--
-- Requires: PostgreSQL 15+, pgvector extension (embedding storage), pgcrypto
-- (UUID generation and column-level encryption primitives).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- -----------------------------------------------------------------------------
-- Platform-level tables (live in a dedicated `platform` schema, never per-tenant)
-- -----------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS platform;

CREATE TABLE platform.tenants (
    tenant_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    slug                TEXT NOT NULL UNIQUE,
    tenant_type         TEXT NOT NULL CHECK (tenant_type IN ('hospital', 'pharma', 'analytics_org', 'trial')),
    isolation_mode      TEXT NOT NULL CHECK (isolation_mode IN ('schema_per_tenant', 'database_per_tenant', 'shared_rls')),
    schema_name         TEXT,                         -- populated when isolation_mode = schema_per_tenant
    data_residency      TEXT NOT NULL DEFAULT 'us',    -- ISO region code driving deployment placement
    deidentification_default TEXT NOT NULL DEFAULT 'safe_harbor'
        CHECK (deidentification_default IN ('safe_harbor', 'expert_determination', 'none_baa_covered')),
    baa_signed_at       TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('trial', 'active', 'suspended', 'offboarding', 'offboarded')),
    -- offboarding lifecycle: see docs/operations/tenant-lifecycle.md. 'offboarding' is a
    -- time-boxed transitional state during which data export + verified deletion runs;
    -- 'offboarded' means deletion is complete and purge_after has been actioned platform-wide.
    offboarding_started_at TIMESTAMPTZ,
    data_purge_completed_at TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE platform.users (
    user_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES platform.tenants(tenant_id),
    external_idp_subject TEXT NOT NULL,                -- OIDC/SAML subject claim
    email               TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, external_idp_subject)
);
CREATE INDEX idx_users_tenant ON platform.users (tenant_id);

CREATE TABLE platform.roles (
    role_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES platform.tenants(tenant_id),
    name                TEXT NOT NULL CHECK (name IN
        ('admin', 'clinician', 'analyst', 'pharmacovigilance_reviewer', 'viewer', 'emergency_access')),
    -- 'emergency_access' backs the break-glass flow — see docs/api/openapi.yaml
    -- POST /access/break-glass and docs/architecture/06-security-compliance.md §3.
    scope_json          JSONB NOT NULL DEFAULT '{}',   -- ABAC scope: department, patient_panel, max_identification_level
    UNIQUE (tenant_id, name)
);
CREATE INDEX idx_roles_tenant ON platform.roles (tenant_id);

CREATE TABLE platform.user_roles (
    user_id             UUID NOT NULL REFERENCES platform.users(user_id),
    role_id             UUID NOT NULL REFERENCES platform.roles(role_id),
    PRIMARY KEY (user_id, role_id)
);
CREATE INDEX idx_user_roles_role ON platform.user_roles (role_id);

-- Immutable, tamper-evident audit log — docs/architecture/06-security-compliance.md §5.
-- Append-only; no UPDATE/DELETE grants issued to any application role. The
-- migration/superuser role that CAN bypass RLS/grants is never used by a
-- request-serving service (see the note at the bottom of this file) and its
-- use is itself logged out-of-band to the WORM mirror described in
-- docs/operations/sla-dr.md §3 — the hash chain below detects tampering by
-- that role too, since it can alter rows but cannot retroactively recompute
-- every subsequent row's hash without detection.
--
-- Partitioned by month on occurred_at: a 6-year-retention, every-request-logged
-- table is multi-billion-row within 1-2 years for any real hospital tenant;
-- monthly partitions keep autovacuum, index maintenance, and the hot/cold
-- tiering in docs/operations/sla-dr.md §3 tractable. Partitions are created by
-- a scheduled job one month ahead of need; partitions older than the hot
-- window (90 days) are moved to the cold tier per docs/operations/sla-dr.md.
CREATE TABLE platform.audit_log (
    -- Monotonic sequence, deliberately NOT a UUID: the hash chain is an ordered
    -- structure, so it needs a total order the database assigns. Chain reads order by
    -- audit_id alone; occurred_at is caller-supplied, ties, and can invert under clock
    -- skew, so ordering by it can make an intact chain fail verification.
    audit_id             BIGINT GENERATED ALWAYS AS IDENTITY,
    tenant_id             UUID NOT NULL REFERENCES platform.tenants(tenant_id),
    actor_user_id          UUID REFERENCES platform.users(user_id),
    actor_service           TEXT,                      -- populated for service-to-service actions
    action                 TEXT NOT NULL,               -- e.g. 'retrieval.query', 'chat.generate', 'document.access', 'access.break_glass'
    resource_type            TEXT NOT NULL,               -- e.g. 'document', 'patient', 'graph_entity'
    resource_id              TEXT,
    phi_accessed              BOOLEAN NOT NULL DEFAULT false,
    request_context_json       JSONB,                       -- retrieval scope, query, model used, citations returned
    prev_hash                 TEXT,                         -- SHA-256 of the previous row's row_hash, per (tenant_id) chain
    row_hash                  TEXT NOT NULL,                -- SHA-256(prev_hash || canonicalized row content) — computed at write time by the Audit Service, verifiable independently at any time
    occurred_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (audit_id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE TABLE platform.audit_log_default PARTITION OF platform.audit_log DEFAULT;
CREATE INDEX idx_audit_log_tenant_time ON platform.audit_log (tenant_id, occurred_at DESC);
CREATE INDEX idx_audit_log_actor ON platform.audit_log (actor_user_id);

-- Embedding model registry — decouples the vector schema from any one model
-- (docs/architecture/02-rag-hybrid-retrieval.md §1.3). `dimensions` determines
-- which of the per-dimension chunk_embeddings_* tables (below) a model's
-- vectors are written to — see that section for why a single fixed-width
-- vector column cannot hold every model's output.
CREATE TABLE platform.embedding_models (
    embedding_model_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider             TEXT NOT NULL,
    model_name           TEXT NOT NULL,
    dimensions            INTEGER NOT NULL CHECK (dimensions IN (1024, 1536, 3072)),  -- extend this list + add a matching chunk_embeddings_<dims> table when a new dimensionality is adopted
    is_default            BOOLEAN NOT NULL DEFAULT false,
    retired_at             TIMESTAMPTZ,                 -- set when a model is superseded; retrieval stops querying its embeddings, backfill/re-embed job (docs/architecture/02-rag-hybrid-retrieval.md §1.3) tracks migration progress separately
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, model_name)
);

-- -----------------------------------------------------------------------------
-- Tenant-scoped clinical & document tables
-- (Deployed per-tenant-schema for isolation_mode = schema_per_tenant; deployed
--  once in `shared` schema with RLS policies below otherwise. Every table below
--  carries tenant_id AND a leading tenant_id index even in schema-per-tenant
--  mode, per ADR-0003's defense-in-depth principle — isolation must not depend
--  on which deployment mode a given tenant happens to be in.)
-- -----------------------------------------------------------------------------

CREATE TABLE patients (
    patient_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            UUID NOT NULL,
    mrn                  TEXT,                           -- PHI, column-encrypted. Medical record number (source-system scoped, not globally unique)
    source_system         TEXT NOT NULL,
    fhir_resource_id      TEXT,                           -- FHIR Patient.id if sourced via FHIR feed
    birth_year            INTEGER,                        -- year only by default; full DOB requires expert_determination scope
    sex                  TEXT,
    deidentification_level TEXT NOT NULL DEFAULT 'safe_harbor'
        CHECK (deidentification_level IN ('identified', 'expert_determination', 'safe_harbor')),
    deleted_at             TIMESTAMPTZ,                    -- soft delete; see docs/operations/tenant-lifecycle.md for the purge workflow this feeds
    purge_after             DATE,                           -- set on deletion request or contractual retention expiry; a scheduled job hard-deletes rows where purge_after < now() and deleted_at IS NOT NULL
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_patients_tenant ON patients (tenant_id) WHERE deleted_at IS NULL;

CREATE TABLE encounters (
    encounter_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL,
    patient_id            UUID NOT NULL REFERENCES patients(patient_id),
    fhir_resource_id       TEXT,
    encounter_type          TEXT,
    facility               TEXT,
    provider_display        TEXT,
    started_at             TIMESTAMPTZ,
    ended_at               TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_encounters_tenant_patient ON encounters (tenant_id, patient_id);

-- Normalized clinical facts — output of the Extraction & Coding Service.
-- Ontology coding: SNOMED CT / ICD-10-11 / LOINC / RxNorm / HPO, reconciled via UMLS CUI.
-- (docs/architecture/02-rag-hybrid-retrieval.md §1.4). umls_cui is nullable —
-- entities that don't resolve to a UMLS concept are still written here with
-- umls_cui = NULL and a corresponding LocalConcept node in the graph
-- (docs/database/graph-schema.md §1); they are not dropped.
CREATE TABLE conditions (
    condition_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              UUID NOT NULL,
    patient_id              UUID NOT NULL REFERENCES patients(patient_id),
    encounter_id             UUID REFERENCES encounters(encounter_id),
    snomed_concept_id         TEXT,
    icd10_code               TEXT,
    umls_cui                 TEXT,
    display_text              TEXT NOT NULL,
    clinical_status            TEXT,
    onset_date                DATE,
    abatement_date              DATE,                      -- resolution/end date, for point-in-time queries — see graph-schema.md §1 bi-temporal note
    asserted_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),  -- transaction time: when the platform recorded this fact, distinct from onset_date (clinical/valid time)
    source_document_id         UUID,                     -- FK to documents, added after documents table below
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_conditions_tenant_patient ON conditions (tenant_id, patient_id);
CREATE INDEX idx_conditions_umls ON conditions (umls_cui);
CREATE INDEX idx_conditions_encounter ON conditions (encounter_id);

CREATE TABLE medications (
    medication_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL,
    patient_id                 UUID NOT NULL REFERENCES patients(patient_id),
    encounter_id                UUID REFERENCES encounters(encounter_id),
    rxnorm_concept_id            TEXT,
    ndc_code                    TEXT,
    umls_cui                    TEXT,
    display_text                 TEXT NOT NULL,
    dosage                      TEXT,
    status                     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'discontinued', 'completed', 'entered-in-error')),
    started_at                  DATE,
    ended_at                    DATE,
    asserted_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_document_id           UUID,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_medications_tenant_patient ON medications (tenant_id, patient_id);
CREATE INDEX idx_medications_umls ON medications (umls_cui);
CREATE INDEX idx_medications_encounter ON medications (encounter_id);
-- When two source documents assert conflicting status for what is otherwise
-- the same medication course, BOTH rows are kept (never overwritten in place)
-- and reconciled by the same provenance-weighted, temporal-precedence policy
-- used in the graph (docs/architecture/03-knowledge-graph.md §1) — this table
-- is a fact log, not a mutable current-state cache.

CREATE TABLE observations (
    observation_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                  UUID NOT NULL,
    patient_id                   UUID NOT NULL REFERENCES patients(patient_id),
    encounter_id                  UUID REFERENCES encounters(encounter_id),
    loinc_code                    TEXT,
    display_text                   TEXT NOT NULL,
    value_numeric                  NUMERIC,
    value_text                    TEXT,
    unit                          TEXT,
    reference_range                TEXT,
    observed_at                    TIMESTAMPTZ,
    source_document_id              UUID,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_observations_tenant_patient ON observations (tenant_id, patient_id);
CREATE INDEX idx_observations_loinc ON observations (loinc_code);
CREATE INDEX idx_observations_encounter ON observations (encounter_id);

-- -----------------------------------------------------------------------------
-- Document & retrieval tables
-- -----------------------------------------------------------------------------

CREATE TABLE documents (
    document_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                  UUID NOT NULL,
    patient_id                   UUID REFERENCES patients(patient_id),     -- null for non-patient-scoped docs (literature, guidelines)
    document_type                 TEXT NOT NULL,   -- discharge_summary | lab_report | radiology_note | trial_protocol | adverse_event_report | literature | guideline | hl7v2_message | fhir_bundle | dicom_study
    source_system                  TEXT NOT NULL,
    title                         TEXT,
    object_storage_uri              TEXT NOT NULL,   -- raw document location
    mime_type                      TEXT,
    deidentification_status         TEXT NOT NULL DEFAULT 'not_deidentified'
        CHECK (deidentification_status IN ('not_deidentified', 'safe_harbor', 'expert_determination')),
    access_scope_json               JSONB NOT NULL DEFAULT '{}',  -- minimum-necessary access constraints
    ingestion_status                 TEXT NOT NULL DEFAULT 'pending'
        CHECK (ingestion_status IN ('pending', 'parsed', 'extracted', 'embedded', 'graph_indexed', 'failed')),
    effective_date                   DATE,
    deleted_at                       TIMESTAMPTZ,
    purge_after                       DATE,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Idempotency: a document registered twice with the same source pointer is
    -- a re-submission (e.g. an ingestion-client retry), not a new document —
    -- see docs/api/openapi.yaml POST /documents Idempotency-Key handling.
    UNIQUE (tenant_id, source_system, object_storage_uri)
);
CREATE INDEX idx_documents_tenant_patient ON documents (tenant_id, patient_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_documents_tenant_type ON documents (tenant_id, document_type);

ALTER TABLE conditions ADD CONSTRAINT fk_conditions_document FOREIGN KEY (source_document_id) REFERENCES documents(document_id);
ALTER TABLE medications ADD CONSTRAINT fk_medications_document FOREIGN KEY (source_document_id) REFERENCES documents(document_id);
ALTER TABLE observations ADD CONSTRAINT fk_observations_document FOREIGN KEY (source_document_id) REFERENCES documents(document_id);
CREATE INDEX idx_conditions_document ON conditions (source_document_id);
CREATE INDEX idx_medications_document ON medications (source_document_id);
CREATE INDEX idx_observations_document ON observations (source_document_id);

-- Chunked, embedded text — see docs/architecture/02-rag-hybrid-retrieval.md §1.2-1.3.
-- Only narrative sections and generated structured-data summaries are chunked
-- here; raw table data (medication lists, lab panels) is retrieved directly
-- from `medications`/`observations` above, not via lossy table-to-text
-- flattening — see §1.2 for the rationale. `section_type` distinguishes a
-- generated summary chunk from true narrative so retrieval can weight them
-- differently.
CREATE TABLE document_chunks (
    chunk_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL,
    document_id                UUID NOT NULL REFERENCES documents(document_id),
    patient_id                 UUID REFERENCES patients(patient_id),
    chunk_index                  INTEGER NOT NULL,
    chunk_text                  TEXT NOT NULL,               -- PHI, column-encrypted
    section_type                 TEXT NOT NULL CHECK (section_type IN
        ('narrative', 'problem_list', 'generated_medication_summary', 'generated_lab_summary', 'other')),
    token_count                  INTEGER,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX idx_chunks_tenant_document ON document_chunks (tenant_id, document_id);
CREATE INDEX idx_chunks_patient ON document_chunks (patient_id) WHERE patient_id IS NOT NULL;

-- Embeddings are split into one table PER DIMENSIONALITY because pgvector
-- requires a fixed vector width per column and this platform is explicitly
-- model-agnostic (docs/architecture/02-rag-hybrid-retrieval.md §1.3). Add a
-- new chunk_embeddings_<dims> table (identical shape) when a model with a new
-- dimensionality is adopted, and extend platform.embedding_models.dimensions'
-- CHECK constraint to match. Retrieval fans out only to the table(s)
-- corresponding to currently-active (non-retired) embedding_models rows.
CREATE TABLE chunk_embeddings_1024 (
    chunk_embedding_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                    UUID NOT NULL,
    chunk_id                    UUID NOT NULL REFERENCES document_chunks(chunk_id),
    embedding_model_id            UUID NOT NULL REFERENCES platform.embedding_models(embedding_model_id),
    embedding                    vector(1024) NOT NULL,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, embedding_model_id)
);
CREATE INDEX idx_chunk_emb_1024_tenant ON chunk_embeddings_1024 (tenant_id);
CREATE INDEX idx_chunk_emb_1024_hnsw ON chunk_embeddings_1024
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE TABLE chunk_embeddings_1536 (
    chunk_embedding_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                    UUID NOT NULL,
    chunk_id                    UUID NOT NULL REFERENCES document_chunks(chunk_id),
    embedding_model_id            UUID NOT NULL REFERENCES platform.embedding_models(embedding_model_id),
    embedding                    vector(1536) NOT NULL,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, embedding_model_id)
);
CREATE INDEX idx_chunk_emb_1536_tenant ON chunk_embeddings_1536 (tenant_id);
CREATE INDEX idx_chunk_emb_1536_hnsw ON chunk_embeddings_1536
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE TABLE chunk_embeddings_3072 (
    chunk_embedding_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                    UUID NOT NULL,
    chunk_id                    UUID NOT NULL REFERENCES document_chunks(chunk_id),
    embedding_model_id            UUID NOT NULL REFERENCES platform.embedding_models(embedding_model_id),
    embedding                    vector(3072) NOT NULL,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, embedding_model_id)
);
CREATE INDEX idx_chunk_emb_3072_tenant ON chunk_embeddings_3072 (tenant_id);
CREATE INDEX idx_chunk_emb_3072_hnsw ON chunk_embeddings_3072
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- HNSW maintenance: `ef_construction`/`m` above are Phase 1 starting points,
-- to be tuned against the eval set in docs/architecture/02-rag-hybrid-retrieval.md
-- §4 once real corpus size/recall targets are known. After any bulk load
-- (>5% of a partition's row count in one batch), run `REINDEX INDEX
-- CONCURRENTLY` on the affected HNSW index — bulk-loaded HNSW graphs measurably
-- degrade in recall until rebuilt; this is a scheduled post-ingestion job, not
-- a manual one.

-- Sync watermark table — tracks what has been pushed to the Search Index
-- (OpenSearch) and the Graph Store (Neo4j) from this system of record, per the
-- single-event-source pipeline in docs/architecture/02-rag-hybrid-retrieval.md
-- §1. Deliberately has no tenant_id/RLS: it stores only document_id + sync
-- status, no PHI content, and every reader already has document_id-level
-- access resolved upstream — see review finding D24.
CREATE TABLE index_sync_state (
    document_id                 UUID NOT NULL REFERENCES documents(document_id),
    target_index                  TEXT NOT NULL CHECK (target_index IN ('opensearch', 'neo4j', 'vector')),
    synced_at                     TIMESTAMPTZ,
    attempts                      INTEGER NOT NULL DEFAULT 0,
    last_error                    TEXT,
    sync_status                   TEXT NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'failed', 'stale')),
    PRIMARY KEY (document_id, target_index)
);

-- -----------------------------------------------------------------------------
-- Pipeline observability (added in Phase 1 implementation)
--
-- Neither table was in the original Phase 0 design. Both were added while building
-- the ingestion pipeline, for reasons that only became concrete in implementation:
-- a document can be processed more than once, and "what is the current state of
-- this document" is a different question from "what happened to it".
-- -----------------------------------------------------------------------------

-- One execution of the ETL pipeline for one document.
--
-- Separate from documents.ingestion_status because reprocessing is a first-class
-- operation (re-parse after an OCR upgrade, re-chunk after a strategy change) and
-- each attempt needs its own stage timings and failure attribution for triage.
CREATE TABLE ingestion_runs (
    run_id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL,
    document_id                  UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    status                      TEXT NOT NULL,
    failed_stage                 TEXT,
    failure_reason                TEXT,
    stage_durations_ms             JSONB NOT NULL DEFAULT '{}',
    chunk_count                   INTEGER NOT NULL DEFAULT 0,
    parser_name                   TEXT,
    pipeline_version               VARCHAR(32) NOT NULL,
    -- Recorded per run so a corpus can be selectively reprocessed after a pipeline
    -- change ("re-ingest everything below version X" is an indexed query), instead
    -- of reprocessing everything or guessing what is stale.
    started_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at                   TIMESTAMPTZ
);
CREATE INDEX idx_ingestion_runs_tenant_document ON ingestion_runs (tenant_id, document_id);
CREATE INDEX idx_ingestion_runs_tenant_started ON ingestion_runs (tenant_id, started_at);

-- Data-quality assessment for one ingestion run.
--
-- `checks` stores every check's inputs and result in full, not just the aggregate
-- score, so a future threshold change can be evaluated against historical documents
-- without re-running the pipeline over the corpus.
CREATE TABLE document_quality_reports (
    report_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                    UUID NOT NULL,
    document_id                   UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    run_id                       UUID REFERENCES ingestion_runs(run_id) ON DELETE SET NULL,
    verdict                      TEXT NOT NULL CHECK (verdict IN ('pass', 'warn', 'fail')),
    score                        DOUBLE PRECISION NOT NULL CHECK (score >= 0.0 AND score <= 1.0),
    checks                       JSONB NOT NULL DEFAULT '{}',
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_quality_reports_tenant_document ON document_quality_reports (tenant_id, document_id);

-- Conversational session state — docs/architecture/04-conversational-ai.md §4.
CREATE TABLE chat_sessions (
    session_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL,
    user_id                     UUID NOT NULL,
    active_patient_id             UUID REFERENCES patients(patient_id),
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at                    TIMESTAMPTZ NOT NULL,
    deleted_at                    TIMESTAMPTZ
);
CREATE INDEX idx_chat_sessions_tenant ON chat_sessions (tenant_id);

CREATE TABLE chat_messages (
    message_id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                     UUID NOT NULL,          -- denormalized from chat_sessions so RLS is a direct predicate, not a correlated subquery, on the highest-QPS table in the product
    session_id                    UUID NOT NULL REFERENCES chat_sessions(session_id),
    role                          TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content                       TEXT NOT NULL,           -- PHI, column-encrypted
    citations_json                 JSONB,          -- resolved document_chunk_id / graph entity_id references; validated at write time by the Conversational AI Service against real IDs (JSONB has no native FK — see docs/design/phase-0-architecture-review.md B14)
    grounding_status                TEXT CHECK (grounding_status IN ('fully_grounded', 'partially_grounded', 'withheld')),
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_chat_messages_tenant_session ON chat_messages (tenant_id, session_id, created_at);

-- =============================================================================
-- Row-Level Security (shared-schema tenants only — see ADR-0003 §Decision table)
-- Application connections set: SELECT set_config('app.tenant_id', '<uuid>', true);
-- per request, inside the same transaction as the query.
--
-- Three details, each of which was strengthened during Phase 1 implementation
-- after the weaker form proved insufficient:
--
--   1. FORCE ROW LEVEL SECURITY, not just ENABLE. PostgreSQL exempts a table's
--      owner from its own policies unless FORCE is set, and the application role
--      is frequently also the owner in a single-role deployment — which would
--      silently disable every policy below.
--   2. WITH CHECK as well as USING. USING governs reads; without WITH CHECK a
--      caller can INSERT or UPDATE rows attributed to another tenant that it can
--      never read back. Verified by an integration test.
--   3. current_setting(..., true) — the `true` makes a missing setting return
--      NULL rather than raising. Combined with the equality comparison, an
--      unscoped connection then matches no rows, so the failure mode is
--      "see nothing" rather than "error" or, far worse, "see everything".
-- =============================================================================

ALTER TABLE patients ENABLE ROW LEVEL SECURITY;
ALTER TABLE encounters ENABLE ROW LEVEL SECURITY;
ALTER TABLE conditions ENABLE ROW LEVEL SECURITY;
ALTER TABLE medications ENABLE ROW LEVEL SECURITY;
ALTER TABLE observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunk_embeddings_1024 ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunk_embeddings_1536 ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunk_embeddings_3072 ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_quality_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;

ALTER TABLE patients FORCE ROW LEVEL SECURITY;
ALTER TABLE encounters FORCE ROW LEVEL SECURITY;
ALTER TABLE conditions FORCE ROW LEVEL SECURITY;
ALTER TABLE medications FORCE ROW LEVEL SECURITY;
ALTER TABLE observations FORCE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE chunk_embeddings_1024 FORCE ROW LEVEL SECURITY;
ALTER TABLE chunk_embeddings_1536 FORCE ROW LEVEL SECURITY;
ALTER TABLE chunk_embeddings_3072 FORCE ROW LEVEL SECURITY;
ALTER TABLE ingestion_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE document_quality_reports FORCE ROW LEVEL SECURITY;
ALTER TABLE chat_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE chat_messages FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_patients ON patients
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_encounters ON encounters
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_conditions ON conditions
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_medications ON medications
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_observations ON observations
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_documents ON documents
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_document_chunks ON document_chunks
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_emb_1024 ON chunk_embeddings_1024
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_emb_1536 ON chunk_embeddings_1536
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_emb_3072 ON chunk_embeddings_3072
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_ingestion_runs ON ingestion_runs
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_document_quality_reports ON document_quality_reports
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_chat_sessions ON chat_sessions
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
CREATE POLICY tenant_isolation_chat_messages ON chat_messages
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- No application role is ever granted BYPASSRLS. Only the migration/superuser
-- role used by the schema-migration job may bypass RLS, and that role is never
-- used by any request-serving service; every session it opens is itself
-- written to the audit_log hash chain (see platform.audit_log comment above)
-- so bypass usage is independently detectable, not just policy-forbidden.
