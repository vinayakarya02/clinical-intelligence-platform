# Runbook: Abstention rate high

**Alert:** `AbstentionRateHigh`
**Severity:** warning
**Dashboard:** [CIP Overview](../../../observability/grafana/cip-overview.json)

## What this means

The copilot is declining to answer more than 40% of questions. Abstaining is safe
but useless: a spike almost always means retrieval or the corpus broke, not that
the safety layer is working harder.

## Immediate check

1. Break down by mode — `blocked` (no evidence) and `uncertain` (low confidence)
   have different causes.
2. Check retrieval: are documents being returned at all?
3. Check the ingest pipeline for dead-lettered jobs.

## Likely causes, most common first

- **Ingest stalled**, so recent documents were never indexed. Check
  `cip_tasks_total{status="dead_lettered"}`.
- **An embedding version changed** without a re-index, so the vector store holds
  vectors from a different model and matches nothing.
- **A vector or graph store is unreachable**; the pipeline degrades rather than
  failing, so this shows as abstention rather than errors.
- **The confidence threshold was raised** in configuration.

## Mitigation

1. If embeddings changed, re-index. The compatibility matrix should have refused
   the deployment — check why it did not.
2. Drain the dead-letter queue and re-enqueue.
3. Lower the confidence threshold **only** after confirming evidence is actually
   being retrieved. Lowering it to hide a retrieval outage produces confident
   answers from thin evidence, which is the worst available outcome.

## What this is *not*

Not a reason to disable the safety layer. A high abstention rate is a symptom of
a broken input path, and answering anyway would convert a visible failure into an
invisible one.
