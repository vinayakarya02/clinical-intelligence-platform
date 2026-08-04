# Runbook: Answer confidence collapsed

**Alert:** `ConfidenceCollapse`
**Severity:** warning
**Dashboard:** [CIP Overview](../../../observability/grafana/cip-overview.json)

## What this means

Median answer confidence is below 0.4. Confidence is a weighted score over six
named components, so the useful question is *which component* fell.

## Immediate check

1. Break the score down: the answer JSON reports every component and names the
   weakest one.
2. `coverage` falling means evidence no longer addresses the questions asked.
3. `verification` falling means claims are failing their evidence check — treat as
   the hallucination runbook instead.

## Likely causes, most common first

- **Corpus drift**: users are asking about things the corpus does not cover.
- **Retrieval degraded**, so evidence is thinner.
- **Source quality fell** — new documents of an unclassified type.

## Mitigation

1. If `coverage` is the weakest component, this is a content problem, not a
   software one. Ingest the missing material.
2. If `verification` is weakest, follow `hallucination-rate.md`.

## What this is *not*

Not fixed by lowering the confidence threshold. That converts a visible "I do not
know" into an invisible weak answer.
