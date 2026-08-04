# ADR-0008: One copilot distribution with enforced internal boundaries

**Status:** Accepted (Phase 3)
**Context:** Phase 3 scope names twelve packages — copilot, agents, planner, reasoning, tools,
memory, timeline, explanations, safety, validation, prompts, output.

## Context

The requirement behind the twelve-package suggestion is real: no God classes, no circular
dependencies, no duplicated logic, everything reusable. The question is whether separate
*distributions* deliver that or merely appear to.

They do not, here. All twelve would share the same domain types (`Evidence`, `Claim`,
`CopilotState`), so those types must live somewhere every package can import. Either a
thirteenth shared package appears, or the types land in whichever package was written first
and the rest depend on it — which is a circular dependency with extra packaging. Twelve
distributions also mean twelve version numbers moving in lockstep for a single deployable,
which is overhead without a corresponding decoupling benefit.

Phase 1 already reasoned through this for the ingestion service
([ADR-0005](adr-0005-phase1-service-decomposition.md)) and reached the same conclusion.

## Decision

One distribution, `cip_copilot`, with the twelve concerns as internal modules, and the
dependency direction enforced rather than documented:

```
  domain.py                    ← depends on nothing in this package
     ▲
  llm/  prompts/  tools/  memory/       ← capabilities; depend only on domain
     ▲
  planner/  reasoning/  timeline/  explanations/  safety/  validation/  output/
     ▲                                   ← stages; depend on domain + capabilities
  agents/                                ← stage adapters
     ▲
  orchestrator.py                        ← depends on everything; nothing depends on it
```

A test walks the module graph and fails on any import that points upward or sideways at the
same level. The rule is checked by CI, which is the only version of an architectural rule
that survives contact with a deadline.

## Consequences

- One version, one install, one import path. Extraction to separate services remains possible
  because the boundaries are real; the test is what keeps them real.
- The dependency test is load-bearing. If it is deleted, this ADR becomes decoration.
- Anything genuinely shared with retrieval or ingestion goes in `cip_core`, not here.
