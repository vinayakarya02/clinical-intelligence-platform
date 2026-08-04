# Phase 6 Engineering Report — Clinical Ecosystem Interoperability

> **Nothing in this phase has exchanged a message with a real hospital system.** Every
> conformance claim is against a specification document, not against a counterparty. Read §11
> before drawing any conclusion about readiness.

**Scope delivered:** an HL7 v2 engine (MLLP, delimiter-aware parsing, validation,
acknowledgement), a FHIR gateway over 18 resource types serving R4 and R5, declarative
HL7→FHIR mapping, an EMPI with a human review zone and reversible merges, a four-level
organisation hierarchy with dated sharing agreements, a deny-by-default consent engine with
audited break-glass, a partitioned clinical event stream with idempotent consumers, an
integration engine with dead-lettering, DICOM/PACS identity and worklist reconciliation,
population analytics with quality measures, a Safe Harbor de-identification pipeline and
point-in-time feature store, four-audience dashboards, closed-loop cross-system workflows,
SMART v2 scopes with ABAC/SCIM/delegation, and a clinical API with asynchronous bulk export.

**Verification:** 1,105 tests collected — 1,093 pass, 12 skip (integration tests needing live
PostgreSQL/MongoDB/Neo4j, and OCR needing Tesseract). 162 are new in Phase 6. `ruff format`,
`ruff check`, and `pyright` clean. Phases 1–5 consumed unchanged.

---

## 1. Research findings applied

| Finding | Applied as |
|---|---|
| Duplicate rates near **18%** within one organisation; cross-organisation matching as low as **50%** even on the same EHR vendor | The EMPI is a first-class subsystem with a **review zone**, not a lookup table ([ADR-0027](adr-0027-empi-review-not-automerge.md)) |
| Fellegi–Sunter: `log(m/u)` agreement weights, two thresholds, three zones | Implemented directly, with `m`/`u` as configuration and documented as defaults rather than truth |
| HL7 v2 declares its own delimiters in `MSH-1`/`MSH-2` | The parser reads them from the message; a scanner, not `split()` ([ADR-0025](adr-0025-hl7-parsing.md)) |
| MLLP framing is `0x0B` … `0x1C 0x0D`; a missing boundary makes a receiver hang or concatenate | A reader with a **required** frame bound, and a second start block abandons rather than merges |
| ADT `A40` merges at the identifier-list level; `A08` is an update, not a merge | Distinct handling; `A40` without `MRG` is rejected |
| `AE` vs `AR` drive a sender's retry logic | Kept distinct, with `sender_should_retry` on the enum |
| FHIR is defined by `StructureDefinition`, not by classes | Resources modelled as element definitions — paths, cardinalities, types, bindings |
| FHIR uses **weak** ETags, deviating from RFC 7232 | Followed FHIR, because the counterparty is a FHIR client |
| Bulk Data: `202` + status location + NDJSON manifest; system-level scopes only | Implemented, including a retention expiry stated in the manifest |
| SMART v2 `.cruds` is an **in-order** subset; granular scopes carry search parameters | Parsed as a grammar, and granular constraints are applied as filters rather than checked and dropped |
| Consent: base decision plus provisions, scoped by purpose, actor, category, period | Implemented, deny-by-default, evaluated at disclosure |
| Break-glass must be audited and post-reviewed | Audit is written **before** the data is returned; a sink failure denies ([ADR-0028](adr-0028-consent-deny-by-default.md)) |
| Kafka: partition key determines ordering; exactly-once lives in the consumer | Partitioned by resolved person; idempotency ledger in the consumer ([ADR-0029](adr-0029-event-ordering.md)) |
| Integration engines give each destination its own queue | Per-destination independence, retries classified transient vs permanent, bounded dead-letter queue |
| DICOM study/series/instance UIDs are the join key; the worklist prevents orphan studies | Modelled, with an explicit unreconciled queue |
| eCQM: initial population, denominator, exclusions, **exceptions**, numerator | Five separate populations; exclusions and exceptions never collapsed |
| Safe Harbor is 18 categories, ZIP to three digits, ages 90+ aggregated | Implemented and tested, including the low-population ZIP suppression |
| Expert Determination is a qualified person's opinion | **Not implemented and not claimed** ([ADR-0031](adr-0031-deidentification-safe-harbor.md)) |
| IHE 360X closed-loop referral: the loop closes or it does not | Task state machine with an explicit terminal set and a staleness threshold per workflow kind |

## 2. Architecture summary

`parse → validate → acknowledge → map → resolve identity → store → publish`, then every
consumer reads through a gate that checks **agreement, consent, scope, and ABAC** before
anything is disclosed.

Seven layers with the dependency direction enforced by a test
([10-clinical-ecosystem-interoperability.md](../architecture/10-clinical-ecosystem-interoperability.md)).
The two organising decisions:

**Wire formats are parsed, mappings are data.** The parser assumes nothing; the mapping is YAML
that fails at load on an unknown transform, a non-existent target element, a missing version, or
two mappings writing one target. A test fails the build if an HL7 field path appears in engine
code.

**Identity and authorisation are separate subsystems.** The EMPI decides that two records
describe one person. That grants nobody the right to read either.

## 3. Integration architecture

Channels with independent per-destination queues, classified retries, and a bounded
dead-letter queue that counts what it drops. Acknowledgement happens after durable acceptance
and before downstream processing, because a sender that is not acknowledged retransmits and a
processing failure is not something the sender can fix by resending.

Three refusals on the ingest path:

- **unparseable → `AR`**, never `AA` over a half-built message
- **`MSH-11` not `P` → rejected**, because test data in a clinical repository is a data-integrity
  incident and the flag is the sender saying exactly that
- **duplicate `MSH-10` → acknowledged, not reprocessed**, because reprocessing an ADT
  re-resolves identity and reprocessing an ORU duplicates results

## 4. Enterprise interoperability design

**Four organisation kinds in a validated hierarchy**, plus sharing agreements that are
directional, dated in both directions, and purpose-scoped. An expired agreement stops working on
its expiry date rather than running for years after a partnership ends.

A cross-organisation disclosure needs **all four** of a resolved person link, an in-force
agreement, a permitting consent, and satisfied ABAC — and the refusal names which one is
missing, because an operator needs to know whether to chase an agreement, a consent, or a role.

A unified longitudinal record is therefore assembled **per requester**. Two requesters
legitimately see different records for the same person; a test asserting one canonical chart
would be asserting a bug.

## 5. Security design

Order of checks, cheapest first: token validity → SMART scope → patient launch context →
organisation agreement → consent → ABAC. Consent is evaluated before the record is read, so a
denial never requires having loaded it.

- **SMART v2 parsed as a grammar.** `.sr` is not a valid scope even though it names the same
  operations as `.rs`; accepting it means accepting typos as permissions.
- **Granular scopes are applied as filters**, not checked afterwards — checking afterwards
  returns the full set and leaks the total.
- **Patient launch context is enforced.** A server that parses it and ignores it turns a
  single-patient app into a whole-population one.
- **ABAC is deny-overrides**, and no matching permit is a deny.
- **Delegation must be a subset** of the delegator's scopes.
- **SCIM deactivation never deletes**, because a reissued directory id would make years of audit
  records point at the wrong person.

Not implemented: JWT signature verification, which needs a key server. Stated as absent rather
than stubbed silently.

## 6. Scalability assessment

Measured in-process with no network, broker, or database.

| Operation | Rate | Per operation |
|---|---|---|
| HL7 parse (7 segments) | 1,110 /s | 0.90 ms |
| FHIR validation | 1,978 /s | 0.51 ms |
| EMPI pair comparison | 3,767 /s | 0.27 ms |
| Stream publish | 2,601 /s | 0.38 ms |
| API read (full authorisation chain) | 3,060 /s | 0.33 ms |
| HL7 → FHIR mapping | 401 /s | 2.49 ms |
| **HL7 ingest, end to end** | **98 /s** | **10.2 ms** |

Peak memory 21 MB for the whole demonstration.

**The bottleneck is identity, and it is quadratic in blocking-bucket size.** Each ingest scores
the incoming record against every candidate its blocking keys return, and a key that stops
discriminating turns that into all-pairs. The first load simulation ran at **12 messages per
second** for exactly this reason.

Two properties follow, and both are honest limits rather than solved problems:

- Ingest throughput is **population-dependent**. A population whose surnames cluster will match
  more slowly than one that does not.
- Per-patient ordering means a single high-volume patient is one partition and cannot be
  parallelised. Accepted: the alternative is giving up the ordering that matters clinically.

At 98 messages per second per process, a hospital ADT feed of a few thousand per minute needs
single-digit process counts. That is plausible and unproven — nothing has run against real
infrastructure, where I/O will dominate these numbers.

## 7. Bugs found

Every one found by the end-to-end run, the load simulation, or the adversarial pass. None by
unit tests written alongside the code.

### Blockers

**B1 — Consent was looked up under an organisation-local identifier.** Consent is filed against
the EMPI person; FHIR resource ids are organisation-local. The API resolved neither, so consent
never matched. Worse than a failure to disclose: the obvious "fix" is to file consent against
the FHIR id, and then a patient with records at two organisations needs two consents and
revoking one leaves the other disclosing. A consent bypass that presents as a data-entry gap.

**B2 — A population export ignored per-patient consent entirely.** Population-level
authorisation (system scope, purpose, ABAC) says the *client* may export. It says nothing about
whether the *patient* agreed. A patient who explicitly refused research use was in the research
extract anyway — the exact failure [ADR-0028](adr-0028-consent-deny-by-default.md) exists to
prevent.

**B3 — EMPI person ids were not legal FHIR ids.** They were `person:<uuid>`, and FHIR permits
only `A-Z a-z 0-9 - .`. Every reference built from one was invalid, and the failure surfaced
three layers away in the imaging projection rather than where it was caused. The same defect
existed for organisation ids (`org:mercy`) and workflow task ids.

### High

**H4 — The matcher merged household members.** Address and telephone were summed as independent
evidence. They are one fact — a shared household — and counting it twice outweighed disagreement
on given name, birth date, **and** sex combined. Fellegi–Sunter's conditional-independence
assumption, violated and unnoticed.

**H5 — Blocking degenerated to all-pairs and throughput collapsed.** A blocking key whose bucket
holds a large share of the index has stopped narrowing anything. The candidate cap contained the
damage and converted it into **silent recall loss** — only the first N sorted candidates were
ever compared, with no signal that any were skipped.

**H6 — The agreement evaluator raised on an unknown organisation.** On the authorisation path,
an unhandled exception is a 500 where a 403 was meant, which fails open in any caller that
catches broadly.

**H7 — No trace context was created at ingest.** HL7 carries no trace header, so every
asynchronous consumer began an orphan trace and the path from a wire message to a downstream
effect could not be reconstructed — which is the question asked during an incident.

### Medium

**M8 — Benchmarks were measured with `tracemalloc` running**, which instruments every allocation
and inflated allocation-heavy figures several-fold. The numbers were measuring the profiler.

**M9 — `MSH-2` was split on the delimiters it declares.** It *contains* `^`, `~`, and `&`, so
tokenising it normally shreds it.

**M10 — The ACK emitted `MSH-1` as a field value**, producing an extra empty field and shifting
every subsequent field by one — the same off-by-one on the way out that the parser handles on
the way in.

## 8. Bugs fixed

All ten, each with a regression test.

- **B1**: `ClinicalApi` takes a **required** `resolve_person` resolver — not an optional one
  with an identity default, because a default silently reintroduces the bug. The integration
  engine registers the association because it is the only component holding both ids. `pyright`
  caught the one call site that had not been wired, which is the point of making it required.
- **B2**: `run_export` evaluates consent **per patient** for the job's purpose, and the manifest
  reports `excludedForConsent` with a note that the cohort is filtered by patient choice and is
  not a complete population. Filtering silently would hand a researcher a biased sample they
  believed was complete.
- **B3**: a documented `fhir_id()` mapping, applied wherever an internal identifier becomes a
  FHIR reference; person ids now use a hyphen.
- **H4**: `correlation_group` on field weights — members contribute only their strongest single
  weight. The household member went from `review` to `non_match`, and the true duplicate and the
  transposed birth date still match.
- **H5**: an over-large bucket is **skipped and counted**, not silently truncated. Throughput on
  degenerate data went from 12 to 85 messages per second, and `degenerate_blocking` reports what
  was skipped so the recall cost is visible.
- **H6**: returns a refusal naming the unregistered organisation.
- **H7**: the engine generates a W3C `traceparent` at ingest and threads a correlation id built
  from the channel and control id.
- **M8**: tracing is stopped before benchmarking; rows measured under it are labelled `(traced)`
  and shown for comparison only.
- **M9**: `MSH-2` is stored literally.
- **M10**: `MSH-1` is produced by joining, never passed as a value.

## 9. Regression tests

**162 tests** in `tests/interop/test_interop.py`, organised by what they attack. The
load-bearing ones:

- **`TestModuleBoundaries`** — layer map, no cross-service imports, and **no HL7 field path in
  engine code**, which is how ADR-0026 stays true rather than aspirational.
- **`TestAdversarialRegressions`** — one test per Blocker and per High, each named by the attack.
- **`TestResourceBounds`** — dead letters bounded and counting drops, ledger bounded, **open**
  workflow tasks never evicted, repository history never evicting the current version.
- **`TestIngestionPipeline`** — end-to-end flow, retransmission suppression, two organisations
  resolving to one person, and the assertion that identity resolution shares **no data** across
  organisations.
- Parser tests for non-standard delimiters, `MSH` off-by-one, `MSH-2` literality, escapes,
  repetitions, and UTF-16 refusal.

The suite also carries a `_seg()` helper that builds HL7 by field *number*. Hand-counting pipes
is how a test asserts the wrong field and then "proves" a bug that is not there — which happened
twice during this phase, once in the demo and once in the tests.

## 10. Benchmarks and simulations

**Load simulation** — 500 ADT messages, one process:

| Population | Throughput | Notes |
|---|---|---|
| Varied surnames | **76 msg/s** | 500 records → 500 people, no false merges |
| Clustered surnames | **85 msg/s** | Blocking guard skipped 399 lookups on each of two degenerate keys |
| Clustered surnames, before the guard | 12 msg/s | All-pairs matching, with silent recall loss |

**Integration simulation** — five realistic messages across four channels: ADT admit, ORU with
three results and a `Z` segment, cross-organisation registration, an order, and a discharge. All
acknowledged `AA`; all four deliberately-bad messages refused with the correct code.

**Event stream** — 5 records, one partition per patient, replay redelivering 5 and the ledger
suppressing all 5, one ordering violation correctly reported on an injected sequence jump.

## 11. Production readiness

**Ready:** the engineering. The parser, the validator, the mapping loader, the matcher, the
consent engine, the authorisation chain, the stream semantics, and the bounds are all
production-grade, tested, and honest about their limits.

**Not ready, in order of severity:**

1. **No counterparty has ever been on the other end.** Not one message exchanged with a real
   EHR, lab, or PACS. Every conformance claim is against a document. This is the gap that
   matters and no amount of internal testing closes it.
2. **The matching probabilities are somebody else's population.** `m` and `u` must be estimated
   from the deployment's own data. Shipping the defaults means matching on assumptions, and
   the error profile is a merged chart.
3. **No review queue has ever been worked.** The design routes ambiguity to a human. Whether
   that human exists, and at what queue depth they stop looking, is unknown — and an unworked
   review queue silently becomes "ambiguity is discarded".
4. **Nothing has run against real infrastructure.** No Kafka, no database, no cluster, no IdP.
   Inherited from Phase 4 and still true.
5. **No licensed terminology.** Codes are carried and compared, not validated against SNOMED CT,
   LOINC, or RxNorm.
6. **Search is deliberately partial**, and chained search, `_include`, and subscriptions are
   absent. Declared in the `CapabilityStatement`, which is the safe failure.
7. **DR is designed, not exercised.** No failover has been performed
   ([multi-region-dr.md](../deployment/multi-region-dr.md)).
8. **Throughput is population-dependent and single-process.** 98 messages per second is a
   starting point, not a capacity plan.

**Assessment:** Phase 6 delivers an interoperability layer built to production standards that
has never met a counterparty. The separation is deliberate and is the same shape as Phase 5's:
the *engineering* can be validated in isolation, and the *integration* can only be validated
against a real sending system.

The honest summary is that **the end-to-end run and the load simulation found every defect that
mattered, and three of them were security-relevant** — consent looked up under the wrong
identifier, a population export that ignored patient consent entirely, and an authorisation path
that raised instead of refusing. All three would have passed any review that read the code
without running it, because each component was individually correct and the defect lived in the
seam between them.

## 12. Technical debt

| Item | Why it matters | Next |
|---|---|---|
| No counterparty testing | Every conformance claim is untested against reality | Before any deployment |
| `m`/`u` unestimated | Matching on somebody else's population | Before any deployment |
| Review queue unstaffed | The design's safety valve depends on it | With operations |
| No terminology validation | A local mnemonic can pass as a standard code | With licensed terminology |
| JWT signatures unverified | Needs a JWKS endpoint | With a real IdP |
| Partial FHIR search | Declared, but limits integration | As integrators need it |
| Blocking is population-dependent | Throughput varies with name distribution | Tune per deployment; the guard makes it visible |
| Consent evaluated per resource on export | O(patients) consent evaluations per export | With a cached decision set |
| No CQL | FHIR's expression language for measures | If importing published eCQMs |
| DR unexercised | An untested plan is a document | With infrastructure |
