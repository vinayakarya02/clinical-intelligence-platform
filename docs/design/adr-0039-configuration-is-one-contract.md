# ADR-0039: Configuration is one contract across code and deployment assets

**Status:** Accepted (Phase 8)

## Context

Phase 8 attempted the first production start using the repository's own committed deployment
assets. It failed four times before serving a request, and every failure was the same shape: an
artefact that was correct on its own terms and disagreed with another artefact nobody had
compared it to.

**The image did not contain three services.** `docker/Dockerfile` copied six source trees; the
repository had nine. The image built, started, and passed its health check with the decision,
interop, and analytics packages absent. No Dockerfile linter detects this, because the
Dockerfile is valid.

**The two settings systems disagreed about the word "production".** `cip_core.Environment`
accepted `prod`; `cip_platform.Environment` accepted `production`. Both read `CIP_ENVIRONMENT`.
The Dockerfile, the compose stack, and the ConfigMap all set the long form. Every containerised
start therefore failed at settings load. No test set the variable, so no test saw it.

**The ConfigMap configured one of the two systems.** It set cache, queue, telemetry, and limits —
all `cip_platform`. It set none of `cip_core`'s deployed-environment requirements, so that
system's safety validation correctly refused the configuration and the pod could not start.

**Secrets were mounted where nothing read them.** The Deployment mounts the Secret as files at
`/var/run/secrets/cip` and announces it with `CIP_SECRETS_DIR` — the right pattern, since files
rotate without a restart and do not appear in `/proc/<pid>/environ`. No code read either. Every
secret-derived setting held its default in production. Worse, three of the manifest's keys —
`postgres-dsn`, `api-key-pepper`, `jwt-public-key` — corresponded to no settings field at all,
so even a loader would have had nothing to do with them.

## Decision

Configuration is **one contract**, and the artefacts that express it are checked against each
other rather than trusted individually.

**Both environment vocabularies are accepted by both systems.** Neither was renamed. Operators,
runbooks, and existing manifests use either spelling, and a config layer that rejects
`production` for being spelled out adds no safety.

**Mounted secrets are read by `cip_core.secrets`, in the composition root, before any settings
are constructed.** `SECRET_FILE_MAP` maps each file to the variable the settings classes already
look for, so neither settings system needs to learn about files. An explicitly-set variable wins
over a mounted file, so an operator can override one value in a debugging session without
unmounting the Secret. A mounted file that maps to nothing is reported, because it means either
dead configuration or a setting silently running on its default.

**The Secret manifest and the loader map and the settings fields must agree**, and a test asserts
the three-way correspondence.

**`cip_gateway.deployment` validates the assets statically**, deriving the expected package list
from the repository rather than hardcoding it, so a service added in a later phase is covered the
day it is added.

**Checks that cannot run here are reported as `unverified`, never as passing.** `docker build`,
`kubectl apply --dry-run`, and image scanning need infrastructure this validator does not have. A
validator that reports success for a check it did not run is worse than one that does not run it,
because it converts an unknown into a false assurance.

## Consequences

The platform now starts under the repository's own manifests, and a change to either the code or
the assets that breaks the correspondence fails a test rather than a rollout.

These checks are static. They assert what the files say, not what a cluster does with them —
admission controllers, storage classes, and network policy enforcement remain unverified until
there is a cluster. That gap is stated in the verification run rather than papered over.

## Alternatives considered

**Rename one environment vocabulary.** Rejected: it breaks every existing manifest and runbook
to fix a problem that aliasing solves without a migration.

**Read secrets from environment variables instead of files.** Rejected: env vars cannot rotate
without a restart, are inherited by child processes, and appear in crash dumps.

**Generate the ConfigMap from the settings classes.** Attractive, and deferred. It would make
the drift impossible rather than merely detected, but it needs a defaults-versus-overrides model
that does not exist yet. The test asserting the correspondence is the cheap version of the same
guarantee.
