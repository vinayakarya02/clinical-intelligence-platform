# HL7 v2 to FHIR mapping

The reference mapping set lives at
[`default-v2-r4.yaml`](../../services/interop/src/cip_interop/mapping/maps/default-v2-r4.yaml)
and is **data, not code** ([ADR-0026](../design/adr-0026-mapping-as-data.md)). This document is
the integration analyst's view of it.

> **This is a reference mapping, not a site mapping.** Every real interface differs. A
> deployment copies this file, adjusts it against the sending site's interface specification,
> and versions the result.

## How to read a mapping entry

```yaml
- source: PID-5.1          # segment - field . component . subcomponent
  target: name[0].family   # a FHIR element path on the resource being built
  transform: identity      # a name from the closed transform set
  params: {}               # transform arguments
  note: >                  # why, for the next person
    ...
```

`[*]` on a source means every repetition; `[*]` on a target means "the repeat index of the
source", which is what turns a repeating `PID-3` into several identifier entries.

## Transforms

The set is closed. A site quirk that none of these expresses becomes a **new named transform,
reviewed once** — never an inline expression, because a mapping file is edited by operators and
must not be a program.

| Transform | Does | Refuses |
|---|---|---|
| `identity`, `trim`, `upper` | Copy | — |
| `constant` | Write a fixed value | — |
| `code_lookup` | Map through a declared table | An unmapped code, unless `passthrough_unmapped` |
| `date` | HL7 `TS` to a FHIR date | A malformed timestamp |
| `datetime`, `instant` | HL7 `TS` to FHIR, precision preserved | A time with no offset and no declared `timezone` |
| `sex` | HL7 administrative sex to the FHIR gender code | — |
| `boolean` | Y/N to a boolean | — |
| `decimal` | Numeric text to a number | Returns nothing for non-numeric, rather than inventing a value |
| `reference` | Source identifier to a typed FHIR reference | A missing `resource_type` |

### Timezone is a required decision

HL7 v2 timestamps routinely omit the UTC offset. FHIR **requires** one on any `dateTime`
carrying a time. There are three possible behaviours and only one is defensible:

- assume UTC — silently shifts every timestamp from that sender by its local offset, so a lab
  drawn at 08:00 is recorded as drawn at 03:00
- drop to date precision — loses the time that lab trending needs
- **require the interface to declare the sending facility's offset** — which is what this does

So every `datetime`/`instant` mapping carries `params.timezone`. **Set it per interface.** A
wrong offset is silently wrong by hours.

## Coverage

| Message | Trigger events | Produces |
|---|---|---|
| ADT | A01, A02, A03, A04, A08, A28, A31 | `Patient`, `Encounter` |
| ORU | R01, R30 | `DiagnosticReport`, one `Observation` per `OBX` |
| ORM | O01 | `ServiceRequest` |
| SIU | S12, S13, S14, S15, S26 | `Appointment` |
| DFT | P03 | one `ChargeItem` per `FT1` |

`MappingSet.consumed_fields()` lists every field the set reads — 48 in the reference mapping —
so an operator can compare it against what a sender actually transmits and see what arrives that
nothing looks at.

## Field mappings that carry a decision

| Source | Target | Why it is not obvious |
|---|---|---|
| `PID-3.1[*]` → `identifier[*].value` | Every repetition | `PID-3` routinely carries an MRN *and* a national identifier; reading only the first loses whichever the sender put second |
| `PID-3.4[*]` → `identifier[*].system` | Assigning authority | Without it an MRN is not comparable across organisations — "12345" is a different person at every hospital |
| `PV1-2` → `Encounter.class` | Patient class, not status | PV1 carries no status field; class and status are different questions and are mapped separately |
| `PV1-19` → `Encounter.identifier` and the resource id | Visit number | Keying the encounter on it makes A02 and A03 update the encounter A01 created rather than duplicating it |
| `OBR-25` → `DiagnosticReport.status` | `C` is a correction, `X` a cancellation | A corrected result stored as `final` leaves the superseded value looking current |
| `OBX-11` → `Observation.status` | `D` deleted, `W` wrong → `entered-in-error` | This is what stops a retracted result being returned as current by a search |
| `ORC-1` → `ServiceRequest.status` | `CA` is a cancellation | A cancelled order landing as `active` stays on a worklist |
| `OBX-5` → `valueQuantity.value` via `decimal` | Non-numeric results are common | "DETECTED", "<0.5", "TNP" produce nothing rather than a fabricated number |

## Identity: HL7 to the EMPI

The mapping produces organisation-local FHIR resources. Identity resolution is separate:
`PID` becomes a `PersonRecord`, the EMPI resolves it to a person, and the integration engine
registers the association between the local FHIR id and that person.

That association is load-bearing. Consent is filed against the **person**; FHIR ids are
**organisation-local**. Without the join, a patient with records at two organisations would need
two consents and revoking one would leave the other disclosing.

## What is not mapped

**`Z` segments.** Retained verbatim and reported as unmapped. Retaining is not understanding;
the mapping layer must be told what a local segment means before anything reads it.

**Anything not in the table above.** A field nobody mapped is absent, and absent is visible —
never silently defaulted, which is the failure mode where a missing unit becomes mg/dL by
assumption.

## FHIR R4 and R5

Both are served from one definition set. The differences that affect these resources are
declared per element, so a payload shaped for one version is an error naming the version rather
than a field the other version's clients never read:

| Element | R4 | R5 |
|---|---|---|
| `Encounter.class` | `Coding` 1..1 | `CodeableConcept` 0..* |
| `Encounter.period` | `period` | `actualPeriod` |
| `MedicationRequest.medication[x]` | choice of `CodeableConcept` or `Reference` | single `CodeableReference` |
| `DocumentReference.context` | `BackboneElement` | list of `Reference`, `period` moved out |
| `Consent.dateTime` / `decision` | `dateTime`, no `decision` | `date`, `decision` |
| `ImagingStudy.modality` | `Coding` | `CodeableConcept` |
