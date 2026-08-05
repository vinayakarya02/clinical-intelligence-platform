# ADR-0040: The task queue is Redis, not Celery

**Status:** Accepted (Phase 9, W0)

## Context

`QueuePolicy.backend` had accepted `"memory"` or `"celery"` since Phase 4, and
`PlatformSettings` refused `memory` in a deployed environment — correctly, since an in-memory
queue executes inline and loses everything on restart. The Kubernetes ConfigMap therefore set
`celery`.

Nothing implemented it. `libs/cip_platform/tasks/` contained `base.py` and `memory.py` and
nothing else. Production configuration named a backend that no code path could build, and Phase
8's startup validation passed because it checked that the settings were *coherent* — nothing
asked whether they were *satisfiable*.

W0 had to resolve that. The roadmap's acceptance criterion was "Celery and Kafka implemented or
removed from config", so the choice was genuinely open.

## Decision

**A Redis-backed durable queue**, and `celery` is no longer an accepted value.

Celery would have required three additional operational systems: a broker (RabbitMQ, already in
the compose file for this reason), a result backend, and a separate worker runtime with its own
concurrency model. Against that, the platform has **three job kinds**.

Redis is already required and is becoming more so:

- the cache backend refuses `memory` in production, so Redis is mandatory there already
- W5 moves rate limiting behind Redis, because the in-process token bucket enforces its limit
  per replica and therefore loosens as the deployment scales

Adding Celery would mean operating RabbitMQ *and* Redis *and* Kafka. Kafka is a deliberate,
separate choice — it is the **event backbone**, where per-tenant ordering is a correctness
property (ADR-0026). A job queue and an event log are different things and the platform needs
both; it does not need a third messaging system to run three job kinds.

Celery is also awkward here on its own terms: it is not asyncio-native, and `TaskQueue` is an
async protocol.

### What was built

Three Redis structures per queue, in `cip_platform.tasks.redis_queue`:

| Key | Type | Purpose |
|---|---|---|
| `cip:q:{queue}` | sorted set, scored `(priority, scheduled_for)` | ready work — a sorted set because the queue must honour both priority *and* delayed scheduling, and a list gives neither |
| `cip:claimed:{queue}` | sorted set, scored by claim deadline | in-flight work; a passed deadline is redelivered by `reclaim_expired`, which is what makes a worker crash recoverable rather than a silent loss |
| `cip:result:{task_id}` | string, TTL-bounded | terminal outcome |

**Claiming is a Lua script**, not `ZRANGE` then `ZREM` from Python. The two-call version lets
two workers read the same member before either removes it, and both then run the job. That
failure is invisible under light load and appears only under the concurrency production has,
which is the worst possible combination.

**Delivery is at-least-once**, and that is a choice rather than a limitation. Exactly-once
across a queue and a database requires distributed transactions; every practical system instead
makes the work idempotent. `TaskSpec.idempotency_key` exists for precisely this and its
docstring already said so.

## Consequences

**This changes a configuration value operators set**, which is a public interface. A deployment
carrying `celery` is refused at load with a message naming the replacement and the migration —
not reported as merely "unknown backend", because an operator whose value used to be correct
needs to be told what replaced it.

Redis now carries three roles: cache (db 0), queue (db 1), and from W5 rate limiting. The compose
file separates them by database number so a saturated queue cannot evict cache entries, and the
`redis` service uses `allkeys-lru` — which is right for a cache-only instance and **wrong** once
queue state shares it. Production must run a separate Redis for the queue with `noeviction`, or
accept that eviction can silently drop queued work. That is recorded here rather than discovered
later; W5 owns making it true in the manifests.

## Alternatives considered

**Implement Celery.** Rejected: three operational systems for three job kinds, plus a non-async
runtime behind an async protocol.

**Put jobs on Kafka.** Rejected. Kafka is an excellent log and a poor job queue — no per-message
acknowledgement, no per-message retry, no delayed delivery, and head-of-line blocking within a
partition. Reusing it here because it was already being adopted would be choosing on
availability rather than fit.

**Keep `celery` as a name and implement it over Redis.** Rejected as actively misleading: an
operator reading `CIP_QUEUE_BACKEND=celery` would reasonably look for a Celery worker, Flower,
and a broker, none of which exist.
