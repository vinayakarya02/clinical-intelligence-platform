# ADR-0020: Severity and evidence quality are independent axes

**Status:** Accepted (Phase 5)

## Context

A drug-interaction warning needs a priority. The simple design is one number, and it is what
most first attempts produce.

Every clinical reference that has survived in practice separates two things. Drug Interaction
Facts grades severity as major/moderate/minor and documentation as
established/probable/suspected/possible/unlikely. Micromedex does the same with
excellent/good/fair/poor. They are separate because they answer different questions: *how bad
if it happens* and *how sure are we that it does*.

## Decision

Two independent enumerations, never multiplied into one score.

- **Severity** — `contraindicated`, `major`, `moderate`, `minor`.
- **Evidence quality** — `established`, `probable`, `suspected`, `possible`, `theoretical`.

**Severity decides whether an alert may be suppressed. Evidence quality decides how it is
worded.** A contraindicated interaction with only theoretical documentation still surfaces —
and says the documentation is theoretical. A minor interaction with established documentation
is suppressible for a prescriber and visible to a pharmacist.

Ranking uses both, plus recency, and reports each contribution separately.

## Consequences

- The two cannot be conflated by accident: they are different types, and a function taking
  both cannot be called with one.
- More knowledge-authoring burden — every artifact must state both. That is the correct
  burden: an author who cannot say how well-documented an interaction is has not finished
  researching it.
- A single "priority" number is still needed for ordering a list. It is *derived* at the point
  of display and never stored, so it cannot become the thing people reason about.
