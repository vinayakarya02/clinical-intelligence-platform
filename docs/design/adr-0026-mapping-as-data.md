# ADR-0026: HL7-to-FHIR mapping is declarative data, not code

**Status:** Accepted (Phase 6)

## Context

Every interface differs. Two hospitals sending `ORU^R01` will disagree about whether `OBX-3`
carries LOINC or a local mnemonic, whether `PID-19` is populated, what `PV1-3` means at their
site, and which `Z` segment holds the thing you actually need.

The person who knows those answers is an integration analyst, not a Python developer. If the
mapping lives in a function per message type, every site-specific quirk is a code change,
reviewed by someone who cannot evaluate whether it is clinically right, and invisible to the
person who can.

## Decision

Mappings are YAML: a source path in HL7 terms (`PID-5.1`), a target path in FHIR terms
(`Patient.name[0].family`), a named transform, and a provenance note. Loaded and validated at
startup by a strict loader, the same shape as
[ADR-0019](adr-0019-knowledge-as-data.md)'s clinical knowledge.

The loader refuses:

- an unknown transform name — a silently unapplied transform produces plausible wrong data
- a target path that is not a real field on the target resource — a typo would write a field no
  validator checks and no consumer reads
- a mapping set with no `version` — an interface whose behaviour changed on an unknown date
  cannot be investigated after an incident
- two mappings writing the same target path without a declared precedence

Transforms are a closed, named set (identity, code lookup, timestamp, name part, coded
concept, unit-aware quantity, identifier with assigner). Anything a site needs that they cannot
express becomes a *new named transform*, reviewed once, rather than an inline expression.

## Consequences

- No arbitrary expressions in mapping files. This is the same refusal as ADR-0025's on parsing
  and [ADR-0019](adr-0019-knowledge-as-data.md)'s on rules: a file an operator edits must not be
  a program, or the mapping file is a remote code execution vector wearing a clipboard.
- Per-site mapping sets are versioned artifacts that can be diffed, reviewed, and rolled back.
  "What changed about this interface on Tuesday" is answerable.
- A field nobody mapped is *absent*, and absent is visible. It is not silently defaulted, which
  is the failure mode where a missing unit becomes mg/dL by assumption.
- Mapping coverage is measurable: which segments and fields a mapping set actually consumes,
  and which arriving fields nothing reads.
