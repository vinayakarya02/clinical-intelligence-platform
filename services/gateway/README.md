# Gateway — Composition Root

The only module that knows about both the platform library and the application layers, which
is what keeps the dependency direction one-way.

## Middleware order

Order is the design. Each layer assumes the one outside it may have failed, and each sits
where it costs least while still being effective.

```
correlation → body limit → authentication → authorisation
            → rate limit (principal, then tenant) → spend budget → handler
                                                                 ↓
                                                    metrics wrap everything
```

- **correlation first**, so every later log line and metric carries the id — including the
  ones emitted while rejecting the request.
- **body limit before authentication**, because rejecting an oversized body should not require
  first doing cryptography on it.
- **rate limit after authentication**, because the limit is per tenant and per principal and
  neither is known before.
- **principal before tenant**, because the principal limit is tighter: one leaked key is
  stopped before it can consume any of its tenant's allowance.
- **budget after the rate limit**, because it is the more expensive check.
- **metrics installed first so they wrap everything**, because "how often are we rejecting" is
  the question a rate limit exists to answer, and a metrics layer inside the limiter would not
  see rejections.

## Health endpoints

Three, because Kubernetes asks three different questions:

| Endpoint | Question | Checks dependencies? |
|---|---|---|
| `/health/live` | Is the process wedged? | **No** |
| `/health/ready` | Should this replica get traffic? | Yes |
| `/health/startup` | Has it finished booting? | No, generous timeout |

Liveness checks nothing external on purpose. A liveness probe that touches the database
restarts every replica during a database blip, turning a degradation into an outage — the most
common way a well-intentioned probe causes the incident it was meant to detect.

## Worker and scheduler

Both entrypoints **refuse to start** without a configured broker and registered handlers. That
is deliberate: a worker that boots, registers nothing, consumes nothing, and reports itself
healthy is the worst available failure mode — the queue fills while every signal says the
system is fine.

## Not yet built

No HTTP routes. `GatewayGuards` is framework-agnostic — it takes a header mapping and returns
a context — and is exercised directly by tests, but nothing mounts it. The middleware ordering
above is verified by construction rather than by a request.
