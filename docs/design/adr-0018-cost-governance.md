# ADR-0018: Spend budgets are separate from rate limits

**Status:** Accepted (Phase 4)

## Context

Rate limiting protects a service from load. For a system that pays a provider per token, it
does not protect the *bill*: a hundred cheap requests and a hundred requests carrying a
200-page discharge summary each look identical to a token bucket and differ by orders of
magnitude in cost. Under-bounded agentic workloads producing runaway spend is a well-documented
failure mode, and the gateway pattern that addresses it is a budget with a warn threshold and a
hard threshold.

## Decision

Two independent controls at the gateway:

- **Rate limit** — token bucket per tenant and per principal, bounding requests per second.
  Per-principal as well as per-tenant, so one leaked API key cannot exhaust its whole tenant.
- **Spend budget** — accumulated USD per tenant per window, with two thresholds: `ALERT`
  (record, emit, keep serving) and `REJECT` (refuse with `429` and a `Retry-After`).

Cost is charged **after** a request completes, from the token usage the answer reports, because
the true cost is not knowable in advance. The check before a request therefore tests the
*accumulated* spend — a request that crosses the limit is allowed to finish, and the next one
is refused. Estimating cost up-front and reserving it would be more precise and would require
a reservation protocol with compensation on failure; the simpler design overshoots by at most
one request.

## Consequences

- A tenant can exceed its budget by one request's cost. Acceptable, and documented.
- Rejection is `429` with `Retry-After` set to the window boundary, so a client backs off to a
  time when the budget will actually have reset rather than retrying into a wall.
- Budgets are per tenant, not per user. Per-user budgets need a quota model nobody has asked
  for, and would let a shared service account starve individual clinicians.
- With no provider configured, cost is zero and the budget never trips — so this control is
  correct but *unexercised* until a real model is wired in, which the readiness report says.
