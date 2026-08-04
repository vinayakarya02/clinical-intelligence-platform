# Clinical Ecosystem Interoperability

Phase 6. Everything between "another organisation's system has a fact" and "this platform holds
it, correctly attributed, lawfully disclosed, and traceable back to the wire".

Phases 1–5 assumed data arrived. This phase is about where it comes from: hospitals, labs,
imaging centres, pharmacies, and EHRs that speak HL7 v2 and FHIR, run their own patient
identifiers, and belong to organisations that are not this one.

---

## 0. The premise, and the two findings that shape it

Interoperability is usually described as a format problem. It is not. Formats are the easy part
— an HL7 v2 parser is a weekend, and FHIR is JSON with rules. The hard parts are the two things
that go wrong at scale, and both are identity problems in disguise.

**Patient identity does not survive an organisational boundary.** Published figures put
duplicate rates inside a single organisation near **18%**, and matching *across* organisations
as low as **50%** even when both sides run the same EHR vendor. A platform that assumes an
incoming `PID-3` identifies a person it already knows is wrong roughly half the time on
cross-organisation traffic. That is why the EMPI is a first-class subsystem with a review queue,
not a lookup table.

**Disclosure is not a side effect of having the data.** Holding a record and being permitted to
disclose it are different questions, and the second one depends on who is asking, why, and what
the patient said. So consent is evaluated at the point of disclosure, deny-by-default, and the
answer is recorded whichever way it goes.

Everything below follows from those two.

## 1. The shape

```
   MLLP/HL7 v2 ──┐
   FHIR REST ────┤                                                    ┌──► Clinical Data
   DICOM/PACS ───┼──► ingest ──► parse ──► validate ──► map ──►┐      │    Repository
   XDS/XCA ──────┤                                             │      │
   Bulk import ──┘                                             ▼      │
                                                          ┌─────────┐ │
                                                          │  EMPI   │─┤
                                                          └─────────┘ │
                                                               │      │
                                                               ▼      │
                                             ┌───────────────────────┐│
                                             │  event stream         ││
                                             │  partitioned by       ├┘
                                             │  resolved person      │
                                             └───────────────────────┘
                                                    │        │
                        ┌───────────────────────────┘        └──────────┐
                        ▼                                               ▼
                  workflows                                     population health
                  (referral, order,                             data lake
                   imaging, discharge)                          dashboards
                        │                                               │
                        └───────────────┬───────────────────────────────┘
                                        ▼
                             ┌─────────────────────┐
                             │ consent + ABAC gate │  ◄── every disclosure, no exceptions
                             └─────────────────────┘
                                        │
                                        ▼
                            REST · FHIR · bulk export · events
```

The gate sits between the platform and every consumer, not between the network and the
platform. Authentication happens at the edge; **authorisation to see a specific patient's
specific data for a specific purpose happens at the point of disclosure**, because that is the
only place where all three of those facts are known.

## 2. HL7 v2 is parsed, never pattern-matched

A v2 message declares its own delimiters in `MSH-1` and `MSH-2`. Any parser that hardcodes `|`
and `^` is correct until the first sending system that does not use them, and then it is
silently wrong — fields shift by one and a result lands in the wrong patient's chart.

So the parser reads the delimiter set from the message it is parsing, and the tokeniser is a
character scanner, not a `split()` chain ([ADR-0025](../design/adr-0025-hl7-parsing.md)).

Four properties follow, each of which is a defect class in real interfaces:

| Property | The defect it prevents |
|---|---|
| Delimiters read from `MSH-1`/`MSH-2` | Field misalignment on non-standard senders |
| Escape sequences decoded (`\T\`, `\F\`, `\S\`, `\R\`, `\E\`, `\X..\`) | "Smith & Sons" parsed as two subcomponents |
| Repetitions preserved as a list, never joined | The second identifier in a repeating `PID-3` silently lost |
| Unknown and `Z` segments retained verbatim | Local segments dropped, and their loss invisible |

**Unparseable is not empty.** A message that cannot be parsed produces an `AR` (reject)
acknowledgement naming the failure, never an `AA` over a half-built message. An interface that
acknowledges what it did not understand is one that loses data silently, which is worse than one
that visibly stops.

## 3. Mapping is data

HL7 v2 to FHIR is a mapping table, expressed as YAML with a source path, a target path, a
transform, and a provenance note — never a function per message type
([ADR-0026](../design/adr-0026-mapping-as-data.md)). The same reasoning as
[ADR-0019](../design/adr-0019-knowledge-as-data.md): an integration analyst must be able to read
what an interface does to a field without reading Python, because that person is the one who
knows whether `OBX-3` at this site carries a LOINC code or a local mnemonic.

A mapping that cannot express a site's quirk gets a new *transform*, reviewed once, rather than
an escape hatch.

## 4. The EMPI never merges silently

Matching is Fellegi–Sunter: per-field agreement weights of `log(m/u)`, summed, compared against
**two** thresholds, producing three zones — match, **review**, non-match
([ADR-0027](../design/adr-0027-empi-review-not-automerge.md)).

The middle zone is the entire point. A two-zone matcher has to put every ambiguous pair
somewhere, and both choices are harmful: auto-merging two people creates a chart containing
someone else's allergies, and auto-splitting hides half a history. So ambiguity becomes a
queued human decision, and the queue depth is a monitored operational metric rather than a
hidden cost.

Two further rules:

- **A merge is a link, not a rewrite.** Source records keep their own identifiers and remain
  individually addressable. A merge is reversible because merges are wrong often enough that
  irreversibility is not defensible.
- **Deterministic identifiers override probability, in one direction only.** A shared
  government identifier can promote a pair to *match*; the absence of one can never demote a
  probabilistic match to non-match, because most records simply lack it.

## 5. Event ordering is per-person, and that is all it is

The stream partitions by **resolved person id**, so every event about one patient is totally
ordered ([ADR-0029](../design/adr-0029-event-ordering.md)). Across patients there is no
ordering, and nothing is allowed to depend on one.

Two consequences that interfaces get wrong:

**Ordering is by source sequence, not wall clock.** Sending systems' clocks disagree, sometimes
by hours; `MSH-7` is a timestamp from a machine this platform does not administer. A later
message with an earlier `MSH-7` is common and normal, and a consumer that sorts on it will
apply a stale update over a fresh one.

**Partition membership changes when the EMPI merges.** Two people who become one person have
two histories that were each ordered and are not ordered relative to each other. The merge event
is itself ordered into the surviving partition and marks the seam, so a consumer can see that
the sequence before it is a union rather than a sequence.

## 6. Consent is deny-by-default and evaluated late

A `Consent` carries a base decision and provisions that except it, scoped by purpose of use,
actor, data category, and time window ([ADR-0028](../design/adr-0028-consent-deny-by-default.md)).

- **No consent record is not permission.** An unknown patient with no filed consent gets deny,
  and the response says "no applicable consent on file" rather than "denied", because those
  drive different operational responses.
- **Revocation is immediate and is never retroactive fiction.** A revoked consent stops future
  disclosures; it cannot unsay what was already disclosed, and the audit trail keeps both.
- **Break-glass is a purpose of use, not a bypass.** It produces access *and* a
  high-severity audit record naming the human, the patient, and the stated reason. There is no
  code path that disables the audit — the record is written before the data is returned, so a
  failure to audit is a failure to disclose.

## 7. Tenant isolation across organisations

Phases 1–5 had one tenant boundary. This phase has three nested ones — organisation, facility,
department — and a fourth relationship that is not nesting: **cross-organisation sharing**.

Sharing is never inferred. Two organisations holding records for the same resolved person share
nothing by default; disclosure requires an explicit, dated, purpose-scoped agreement *and* a
consent that permits it ([ADR-0030](../design/adr-0030-cross-organisation-sharing.md)). The
EMPI's job is to know that two records are one person. It is emphatically not to make one
organisation's data visible to another.

## 8. Imaging is referenced, never processed

DICOM study/series/instance identity, modality, body site, and the retrieval endpoint are
modelled; pixels are not touched. The platform stores what is needed to find an image and to
reason about the fact that it exists — which is the part clinical decision support needs — and
leaves rendering and analysis to a PACS and a viewer that are validated for it.

## 9. De-identification claims exactly what it implements

Safe Harbor: the 18 identifier categories, removed or generalised, with dates reduced to year
and ages over 89 aggregated. That is implemented and tested
([ADR-0031](../design/adr-0031-deidentification-safe-harbor.md)).

Expert Determination is **not** implemented and is not claimed. It is a statistical opinion by a
qualified person about a specific dataset and a specific release context; software cannot
produce one. A pipeline that labelled its output "expert determined" would be asserting
something no code can assert.

## 10. What this deliberately is not

**Not a certified interface engine.** It has never exchanged a message with a real hospital
system. Every conformance claim here is against a specification document, not against a
counterparty.

**Not a FHIR server.** It implements the resource model, validation, versioning, bundles, and
the operations this platform needs. It does not implement the full search grammar, subscriptions,
or terminology operations, and its `CapabilityStatement` says so rather than overstating.

**Not a PACS, an HIE, or a warehouse.** It integrates with those roles and does not replace
them.
