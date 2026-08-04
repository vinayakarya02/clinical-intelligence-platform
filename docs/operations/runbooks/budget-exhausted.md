# Runbook: Tenant spend budget exhausted

**Alert:** `SpendBudgetRejections`
**Severity:** warning
**Dashboard:** [CIP Overview](../../../observability/grafana/cip-overview.json)

## What this means

A tenant hit its daily USD budget and requests are being refused with `429`.
The control is working; the question is whether the spend is legitimate.

## Immediate check

1. Identify the tenant: `sum by (tenant) (increase(cip_budget_rejections_total[1h]))`.
2. Compare today's spend against the trailing week for that tenant.
3. Check the token-usage panel: is spend up because of more requests, or larger ones?

## Likely causes, most common first

- **Legitimate growth.** Raise the budget.
- **A runaway client** retrying on failure without backoff. Larger request count,
  flat request size.
- **A prompt change that inflated context size.** Flat request count, larger
  requests — check `gen_ai.usage.input_tokens` per answer.
- **A leaked API key.** Correlate per-principal rate-limit rejections.

## Mitigation

1. Raise `CIP_DAILY_BUDGET_USD` if the spend is legitimate — it takes effect on
   the next window without a restart.
2. Revoke the key if a single principal is responsible.
3. If a prompt inflated context, roll the prompt back; the budget is a symptom.

## What this is *not*

Not a rate-limit problem. Rate limits bound requests; this bounds spend, and a
tenant can exhaust its budget while well inside its request limit (ADR-0018).
