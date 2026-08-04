# Runbook: Hallucination rate rising

**Alert:** `HallucinationRateRising`
**Severity:** critical
**Dashboard:** [CIP Overview](../../../observability/grafana/cip-overview.json)

## What this means

More claims are failing verification against the evidence they cite. This is a
clinical-safety signal: the system is generating statements the retrieved
evidence does not support. Note that the verifier *catches* these — a rising rate
means more are being attempted, not that more are reaching clinicians.

## Immediate check

1. Confirm the answer mode mix: `sum by (response_mode) (rate(cip_answers_total[15m]))`.
   Rising `uncertain` alongside this is the verifier doing its job.
2. Check whether a model, prompt, or embedding version changed in the last hour.
3. Pull three recent traces and read the `reflect` stage's `rejected` notes.

## Likely causes, most common first

- **A prompt version rolled out.** Most common. Check `gen_ai.prompt.name` versions
  on recent answers against the previous hour.
- **A model version changed.** A new model generates differently against the same
  claims.
- **Retrieval degraded**, so claims are built from thinner evidence. Correlate with
  cache hit rate and retrieval latency.
- **A corpus change** introduced documents that contradict each other.

## Mitigation

1. **Roll back the prompt** — `PromptCatalog` deployment pin, no deploy required.
2. **Roll back the model** — `ModelRegistry.rollback(name)`, one call.
3. If neither changed, raise the confidence threshold to trade answers for safety
   while investigating. The system abstaining is safe; the system asserting is not.

## What this is *not*

Not an infrastructure problem. CPU, memory, and pod restarts are irrelevant here —
do not start by looking at them.
