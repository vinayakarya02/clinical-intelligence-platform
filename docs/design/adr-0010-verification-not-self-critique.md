# ADR-0010: Reflection is verification against evidence, not self-critique

**Status:** Accepted (Phase 3)

## Context

The reflection pattern — generate, critique, revise — is well established, and the research is
equally clear about its failure mode: a model critiquing its own output can reinforce an error
instead of catching it, and each pass costs latency and tokens. The variants that hold up
(CRITIC and similar) replace introspection with *tool-interactive verification*: check the
claim against something external before accepting a revision.

In this system something external already exists and is already assembled — the evidence set.

## Decision

Reflection is a deterministic verification pass over `(claim, cited evidence)` pairs:
citation resolvability, lexical support, numeric fidelity, cross-source contradiction, and
question coverage.

**A claim that fails verification is dropped, never rewritten.** Rewriting a failed claim is
how a reflection loop turns into a generator of plausible corrections that are themselves
unverified. Dropping is auditable: the trace records what was dropped and why, and the
confidence score falls accordingly — which is the honest signal.

Numeric fidelity is exact-match, not fuzzy. "Potassium 5.4" and "potassium 5.6" are similar
strings and clinically different facts, and a tolerance here would be a decision to sometimes
report the wrong lab value.

## Consequences

- No unbounded loop; verification is one pass with a fixed cost.
- Verification cannot catch a claim that is *supported by the evidence but the evidence is
  wrong*. That is a retrieval and source-quality problem, and it is why source quality feeds
  confidence separately.
- A fluent but unsupported sentence cannot survive, which is the failure mode that matters
  most clinically.
