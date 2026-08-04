# ADR-0022: No model participates in a decision

**Status:** Accepted (Phase 5)

## Context

Phase 3 confined the language model to composing prose from verified claims
([ADR-0009](adr-0009-deterministic-orchestration.md)). This phase produces *recommendations*,
and the pressure to use a model is stronger — a model could weigh factors a rule set cannot,
and would handle the open-ended tail of clinical situations the rules do not cover.

## Decision

The decision path is entirely deterministic. Rules, drug checks, risk models, contradiction
detection, and suppression are code and data. The model composes explanation prose from
decisions already made, and is not consulted about what to recommend.

The reasoning is different from Phase 3's and stronger:

- A recommendation is a **regulated clinical claim**, and FDA's CDS guidance turns on whether
  a clinician can understand its basis. "The model weighed it" is not a basis.
- A rule that fires differently on identical input **cannot be validated**, and a knowledge
  base that cannot be validated cannot be clinically reviewed — which makes the whole
  knowledge-as-data decision worthless.
- The failure mode of a probabilistic recommender is a *plausible* recommendation. That is
  precisely the kind a busy clinician accepts without checking.

## Consequences

- Situations the rules do not cover produce **no recommendation**, not a guess. Silence is the
  correct output for an unmodelled situation, and the missing-information detector says what
  would have been needed.
- Coverage is a knowledge-base property and is measured as one, so a gap is visible as a
  number rather than hidden behind a model's willingness to answer anyway.
- Adding clinical capability means adding reviewed knowledge, which is slower than prompting —
  and is the point.
