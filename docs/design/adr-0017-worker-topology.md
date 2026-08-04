# ADR-0017: One image, three entrypoints

**Status:** Accepted (Phase 4)

## Context

The system needs an API, a queue worker, and a periodic scheduler. Standard practice is
separate images per service, each with a minimal dependency set, so a compromised worker cannot
import the web framework and an API image stays small.

Against that: the evaluation job runs the copilot, the embedding job runs the retrieval
pipeline, and the ingest job runs the whole Phase 1 ETL. A worker with a genuinely minimal
dependency set could not run any of them.

## Decision

One image, three entrypoints, selected by command. `api` serves HTTP, `worker` consumes the
queue, `scheduler` enqueues periodic work.

They differ in what they *run*, not in what they *contain*, and Kubernetes gives each its own
Deployment with its own replica count, resource envelope, and autoscaling policy — which is
where the operational separation that actually matters lives.

The scheduler runs at replica count **one** with a leader lock, not because one replica is
reliable but because two would enqueue every periodic job twice, and a duplicated nightly
re-index is expensive rather than merely untidy.

## Consequences

- One build, one scan, one SBOM, one version. A version skew between API and worker becomes
  impossible rather than merely unlikely.
- The API image carries worker dependencies it never imports, which is a larger attack surface
  than a minimal image. Mitigated by a non-root, read-only-rootfs, all-capabilities-dropped
  runtime rather than by dependency pruning.
- If the worker's dependency footprint ever diverges sharply from the API's, this should be
  revisited. It is a size-dependent decision, not a principle.
