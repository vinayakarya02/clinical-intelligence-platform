# Clinical Ecosystem Interoperability

Phase 6. Everything between "another organisation's system has a fact" and "this platform holds
it, correctly attributed, lawfully disclosed, and traceable back to the wire".

**Scope boundary:** retrieval (Phase 2), reasoning (Phase 3), the production platform (Phase 4),
and clinical decision support (Phase 5) are consumed through their own entry points. This
service adds no retrieval, no reasoning, and no clinical logic.

> **Nothing here has exchanged a message with a real hospital system.** Every conformance claim
> is against a specification document, not against a counterparty.

## The two findings that shape it

Interoperability is usually described as a format problem. It is not — formats are the easy
part. The hard parts are both identity problems.

**Patient identity does not survive an organisational boundary.** Published duplicate rates
inside a single organisation run near 18%, and cross-organisation matching as low as 50% even
between sites running the same EHR vendor. So the EMPI has a human review zone, merges are
reversible links, and a merge contradicted by a national identifier is refused outright.

**Holding a record and being permitted to disclose it are different questions.** So consent is
deny-by-default, evaluated at the point of disclosure with a stated purpose, and break-glass
writes its audit record before the data is returned.

## Pipeline

```
   MLLP/HL7 v2 ──┐
   FHIR REST ────┤                                                 ┌──► Clinical Data
   DICOM/PACS ───┼─► parse ─► validate ─► ack ─► map ─► resolve ──►┤    Repository
   Bulk import ──┘                                       identity  │    (per organisation)
                                                                   ▼
                                                   ┌───────────────────────────┐
                                                   │ event stream, partitioned │
                                                   │ by resolved person        │
                                                   └───────────────────────────┘
                                                          │           │
                                    workflows ◄───────────┘           └──► population health
                                    (referral, order,                      data lake
                                     imaging, discharge)                   dashboards
                                                     │                          │
                                                     └────────────┬─────────────┘
                                                                  ▼
                                                    ┌─────────────────────────┐
                                                    │ consent + agreement +   │ ◄── every
                                                    │ scope + ABAC gate       │     disclosure
                                                    └─────────────────────────┘
                                                                  ▼
                                                REST · FHIR · bulk export · events
```

## What cannot happen, structurally

**A parser that assumes delimiters.** They are read from `MSH-1`/`MSH-2` of the message being
parsed ([ADR-0025](../../docs/design/adr-0025-hl7-parsing.md)). A message that cannot be parsed
gets `AR`, never `AA` over a half-built message.

**A mapping written in Python.** Source path, target path, named transform, provenance note —
YAML, refused at load if the transform is unknown, the target is not a real element, the version
is missing, or two mappings write one target
([ADR-0026](../../docs/design/adr-0026-mapping-as-data.md)). A test fails the build if an HL7
field path appears in engine code.

**A silent merge.** Fellegi–Sunter with **two** thresholds and three zones; ambiguity is a
queued human decision and the queue depth is a monitored metric
([ADR-0027](../../docs/design/adr-0027-empi-review-not-automerge.md)).

**A disclosure without a stated purpose.** `purpose` is a required parameter, and
`no_consent_on_file` is a distinct outcome from `denied` because they need different operational
responses ([ADR-0028](../../docs/design/adr-0028-consent-deny-by-default.md)).

**Cross-organisation access from identity alone.** Resolved identity, a dated purpose-scoped
agreement, a permitting consent, and ABAC — all four, and the refusal names which one is missing
([ADR-0030](../../docs/design/adr-0030-cross-organisation-sharing.md)).

**An unscoped repository.** The organisation is a constructor argument, so an unscoped query is
unconstructable rather than a review finding.

**A claim of Expert Determination.** Safe Harbor is implemented and tested; the
`DeidentificationMethod` enumeration has no expert-determination member, because no code can
produce a qualified person's opinion ([ADR-0031](../../docs/design/adr-0031-deidentification-safe-harbor.md)).

## Modules

Layer *n* imports only layers below it, enforced by a test.

| Layer | Modules | Responsibility |
|---|---|---|
| 0 | `domain` | Identifiers, names, addresses, purpose of use, source records |
| 1 | `hl7`, `fhir`, `orgs` | Wire formats and the organisation hierarchy |
| 2 | `mapping`, `empi`, `imaging`, `consent`, `security`, `streaming` | Translation, identity, disclosure, transport |
| 3 | `routing`, `population`, `datalake`, `dashboards` | The integration engine and the analytics surfaces |
| 4 | `workflow` | Cross-system referrals and orders |
| 5 | `api` | REST, FHIR, bulk export and import |
| 6 | `demo` | The end-to-end run |

## HL7 v2

ADT, ORM, ORU, SIU, DFT, ACK. MLLP framing with a required frame bound, a delimiter-aware
scanner, escape decoding on access, repetitions preserved at every level, and `Z` segments
retained verbatim.

`AE` and `AR` are kept distinct because a sending system's retry logic uses them: `AE` means the
failure may be transient, `AR` means retrying the same message is load with no progress.

## FHIR

Eighteen resource types modelled the way FHIR models itself — element paths, cardinalities,
types, and required bindings as data rather than a class per resource. Both R4 and R5 from one
definition set, with version-specific elements declared, so an R4 `medicationCodeableConcept`
on an R5 resource is an error naming the version rather than a field no client reads.

Validation covers cardinality, primitive syntax, required bindings, reference target types,
choice-group exclusivity, and unrecognised modifier extensions — which block the resource,
because a modifier extension can invert the meaning of the element it sits on.

The `CapabilityStatement` is generated from what is registered, so it can only understate.

## Running it

```bash
python -m cip_interop.demo
```

A four-organisation ecosystem, real HL7 through the full pipeline, identity resolved across the
boundary, consent enforced, events streamed and replayed, imaging reconciled, population
analytics, a de-identified extract, a closed-loop referral, dashboards, a 500-message load
simulation, and benchmarks. See the
[Phase 6 engineering report](../../docs/design/phase-6-engineering-report.md).

## What is deliberately not real yet

**No counterparty has ever been on the other end.** Not one message has been exchanged with a
real EHR, lab, or PACS.

**Not a FHIR server.** The resource model, validation, versioning, bundles, and the operations
this platform needs. Not the full search grammar, not subscriptions, not terminology
operations — and the `CapabilityStatement` says so.

**No licensed terminology.** Codes are carried and compared; they are not validated against
SNOMED CT, LOINC, or RxNorm, which are licensed products.

**Kafka, a database, and a real IdP are protocols here.** The streaming semantics, repository
contract, and token verification are implemented and tested in process. The signature check in
token verification needs a key server and is stated as absent rather than stubbed.

**The `m` and `u` matching probabilities are defaults, not truth.** They are population
properties and must be estimated from the deployment's own data.
