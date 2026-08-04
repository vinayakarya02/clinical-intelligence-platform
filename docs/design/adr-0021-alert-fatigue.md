# ADR-0021: Suppression is a safety feature, not a convenience

**Status:** Accepted (Phase 5)

## Context

Published override rates for CDS alerts run from 49% to 96%, and roughly 300 reminders are
required to prevent one adverse drug event. The systematic reviews are consistent that the
most effective intervention is **role tailoring** — showing prescribers and pharmacists
different alerts.

The implication is uncomfortable and load-bearing: **a CDS that fires on everything is worse
than no CDS.** It consumes the attention it needs, and it trains clinicians to dismiss alerts
reflexively — including the one that mattered. Recall is not free; every additional true-but-
unimportant alert has a cost paid in the response to every other alert.

## Decision

Suppression is a designed, tested, measured stage of the pipeline, with four mechanisms:

1. **Severity floor per role.** A prescriber sees `major` and above by default; a pharmacist
   sees `moderate` and above. `contraindicated` is never suppressible for anyone.
2. **Deduplication.** The same clinical concern reached by two rules is one alert with two
   supporting rules, not two alerts.
3. **Override memory.** A recommendation a clinician rejected for this patient does not fire
   again unchanged within the configured window. Rejecting it is information, and repeating it
   discards that information while spending attention.
4. **Volume ceiling per interaction.** Above the ceiling, the lowest-severity items are folded
   into a single summary rather than dropped silently — a suppressed alert must remain
   discoverable.

Every suppression is **recorded with its reason**. Suppression that cannot be audited is
indistinguishable from a bug.

## Consequences

- Some true alerts are not shown. That is the deliberate trade, made explicitly, on published
  evidence — and it is why `contraindicated` is exempt.
- Suppression rate and override rate are first-class metrics. A rising override rate means the
  knowledge base is wrong, and it is the only direct measurement of that available.
- Role tailoring requires the caller to state a role. A request that does not is treated as the
  most conservative role and shown everything, because guessing wrong in the permissive
  direction is the failure this ADR exists to prevent.
