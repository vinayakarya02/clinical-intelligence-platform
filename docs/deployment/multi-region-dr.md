# Multi-region deployment and disaster recovery

Phase 6. Extends the single-region deployment in
[deployment-architecture.md](deployment-architecture.md) and the SLA/DR material in
[sla-dr.md](../operations/sla-dr.md) with what a multi-organisation clinical ecosystem adds.

> **None of this has been exercised.** No cluster has been built, no failover has been run, and
> no recovery time has been measured. The targets below are design intent; until a failover has
> actually been performed they are aspirations, and a DR plan nobody has tested is a document
> rather than a capability.

## 1. What clinical downtime actually costs

The distinguishing property of a clinical system is that **the work does not stop when the
system does**. Patients keep arriving, labs keep resulting, and clinicians revert to paper.
Recovery therefore has two halves, and the second is the one that gets forgotten:

1. bring the platform back
2. reconcile everything that happened while it was down

An interface engine that comes back and simply resumes has lost every message the sending
systems retried into a dead socket. Recovery is not complete until the backlog is drained and
the gap is proven closed.

## 2. Recovery objectives, per service class

| Class | Services | RPO | RTO | Rationale |
|---|---|---|---|---|
| Clinical ingest | MLLP listeners, integration engine | **0** | 5 min | A lost ADT or ORU is a clinical fact that never arrived. The sender's retry queue is the buffer, so ingest must return before senders give up. |
| Disclosure | FHIR API, consent engine | 0 | 15 min | Read path. Failing closed is safe but blocks care. |
| Identity | EMPI | 0 | 15 min | A merge lost on failover splits a chart; the merge log must be synchronously replicated. |
| Decision support | Phase 5 engine | 5 min | 30 min | Deterministic and rebuildable from the knowledge base. |
| Analytics, data lake | Population, export | 24 h | 24 h | Reproducible from bronze. |

**RPO 0 on ingest and identity is expensive and is the right expense.** Both hold decisions that
cannot be reconstructed from anywhere else: an audit record and a merge are original facts, not
derivations.

## 3. Topology

**Active–active for reads, active–passive for writes**, per region:

- The FHIR read path and consent evaluation run in every region.
- Writes are directed to one primary region. Multi-primary write on a clinical record invites
  divergent versions of the same resource, and the optimistic-concurrency contract cannot
  detect a conflict it never saw.
- The event stream replicates asynchronously. Consumers are idempotent by design
  ([ADR-0029](../design/adr-0029-event-ordering.md)), so replay after a failover is safe — which
  is what makes an asynchronous replica acceptable here and not for the audit log.
- The audit log replicates **synchronously**. An audit record that survives only in the region
  that just failed is the same as no audit record.

MLLP listeners are the exception to read/write symmetry: a sending hospital has one configured
endpoint, so failover means either a DNS or VIP move with the sender's retry window as the
budget, or a listener in each region with the primary writing through.

## 4. Per-organisation blast radius

Multi-tenancy is a DR property, not only a security one. Each organisation's repository is
scoped by construction, so:

- an organisation can be restored **independently** — a corruption at one hospital does not
  require restoring the others to a prior point in time
- a per-organisation restore is testable at a scale a full restore is not, which matters
  because an untested restore procedure is a hypothesis

## 5. Reconciliation after recovery

The step most plans omit. After ingest is restored:

1. **Drain the backlog.** Senders retry; the engine's duplicate suppression on `MSH-10` makes
   that safe, and the count of suppressed duplicates is the evidence it worked.
2. **Prove the gap is closed.** Per source system and per patient, the source sequence must be
   contiguous across the outage window. A gap is an interface incident and is reported as one
   rather than smoothed over.
3. **Re-run identity for the window.** Records ingested during degraded operation may have
   resolved against an incomplete index; re-resolution can produce review-queue entries that
   would not have arisen normally, and those need working.
4. **Reconcile imaging.** Studies acquired during the outage will have been entered at the
   modality console rather than from a worklist, so the unreconciled queue will be non-empty
   and every entry is a patient-attribution risk.
5. **Review break-glass.** Downtime drives emergency access. The break-glass queue after an
   outage is the largest it ever gets and is the one nobody has time to review.

## 6. Backup strategy

| Data | Method | Retention | Restore tested |
|---|---|---|---|
| Operational store | Continuous WAL archiving, point-in-time recovery | 35 days | Not yet |
| Audit log | Append-only, synchronously replicated, immutable storage | 7 years (HIPAA) | Not yet |
| Event stream | Topic retention plus periodic snapshot | 30 days | Not yet |
| Bronze lake | Object storage with versioning and object lock | 7 years | Not yet |
| Knowledge and mapping sets | Git, versioned artifacts | Indefinite | Yes — they are files |

The audit log's requirements are different in kind from everything else: it must be immutable,
because its purpose is to be trustworthy when somebody is being investigated, and a mutable
audit log is evidence of nothing.

## 7. Exercises

| Exercise | Cadence | Proves |
|---|---|---|
| Tabletop | Quarterly | People know the plan |
| Per-organisation restore | Monthly | Backups are readable and scoped |
| Region failover and failback | Annual | The topology works, and failback works — which is the half that is usually skipped |
| Interface backlog drain | With each failover | Reconciliation closes the gap |

An exercise that does not include **failback** has tested half the procedure, and the half it
skipped is the one performed under pressure with the clock running.

## 8. What is missing

- Nothing has been deployed, so no number here is measured.
- No chaos or fault-injection testing.
- Cross-region replication lag is unmeasured, so the RPO 0 claim for identity is a design
  requirement rather than an observed property.
- Sending-system retry windows are counterparty-specific and unknown; the 5-minute ingest RTO
  assumes they exceed it, which must be confirmed per interface before it means anything.
