# Integration testing

How the suite that runs against real infrastructure is set up, why it is set up that way, and
what is still unverified.

Written in Phase 9 W6, after the discovery that this suite **had never actually executed**. Every
design decision below is a response to something that went wrong, so the reasoning is recorded
alongside the mechanics; a rule whose reason is lost gets removed by the next person who finds it
inconvenient.

## What went wrong

The integration job was green from Phase 1 until Phase 9 W6 and ran nothing. Three independent
failures had to line up, and they did:

1. **`pytest -q` hides skip reasons.** The job printed `10 skipped` and exited 0. Nothing in that
   output says the database was never touched.
2. **Nothing failed a run in which everything skipped.** A suite that skips everything and passes
   is worse than no suite. It is false assurance, and it stays invisible precisely because the
   job is green.
3. **The fixtures skipped on *any* exception.** Setup was wrapped in
   `except Exception: pytest.skip("PostgreSQL is not reachable")`, so a permission error, an
   unmigrated schema, and a genuinely absent server were indistinguishable — and all three were
   silent.

Underneath those, the fixture that was supposed to give each test a clean database **destroyed
the migrated schema**: `Base.metadata.drop_all` followed by `DROP SCHEMA platform CASCADE`. That
is coherent for a suite that owns an empty database and catastrophic for one that runs after
`alembic upgrade head`. The first test's teardown dropped the tables; every test after it errored;
every error surfaced as a skip.

Two further defects meant that even the tests that *did* run proved less than they claimed:

- **The RLS tests created their own policy** before asserting on it, so they tested a policy the
  test had just written. The migration's policy — the one production runs — was never exercised.
- **CI connected as a superuser.** `POSTGRES_USER` creates one, and a superuser bypasses
  row-level security unconditionally; `FORCE ROW LEVEL SECURITY` does not change that. Every
  isolation assertion was vacuous.

## The rules the suite now follows

**A skip means "the infrastructure is not here" and nothing else.** Every other condition is a
failure. Reachable but unmigrated is a failure. A permission error is a failure. Those are broken
environments, not absent ones, and a suite that skips them reports success for work it never did.

**Tests clean data, never structure.** `TRUNCATE ... RESTART IDENTITY CASCADE` before each test.
The schema comes from the migrations and stays there, so every assertion is about what the
migrations actually produced rather than about a schema the tests invented.

**Migration tests get a throwaway database.** `tests/integration/test_migrations.py` creates
`cip_mig_<hex>` per test and drops it afterwards. It has to: a migration test that downgrades the
shared database leaves every test after it running against a schema that is half gone.

**One place says "this service is absent".** `tests/integration/services.py`. The fixtures call
it, and it both formats the skip reason and records which service was missed. The first version
of the guard matched phrases in skip messages, which meant rewording a message silently stopped
the guard noticing that whole service — a regression that makes the suite *quieter*.

**A run that reached nothing fails.** Two guards in `tests/conftest.py`:

- `--integration-min=N` fails a run in which fewer than N tests executed.
- With `CIP_RUN_INTEGRATION=1`, **any** skip naming an unreachable backing service fails the run
  outright. The floor alone degrades as the suite grows — add twenty tests and a floor of fifteen
  is satisfied while five services sit unreached.

**The suite asserts its own premises.** `TestTheTestsThemselves` checks that the connected role is
neither `SUPERUSER` nor `BYPASSRLS`, and that the policies under test are `FORCE`d. Without that,
a credentials change quietly neuters every isolation assertion in the file — which is exactly
what happened.

## Running it

Locally, with Docker:

```bash
make services-up && make migrate && make test-role && make test-integration
```

`make test-role` is not optional. It creates `cip_app`, a `NOSUPERUSER NOBYPASSRLS` role that owns
the migrated tables — the only conditions under which the RLS tests mean anything. It runs
`scripts/create_test_role.sql`, the same file CI runs, so the two environments cannot drift.

In CI, the `integration` job starts all five services and runs:

```bash
pytest tests -q -rs -m integration --integration-min=50
```

`-rs` prints the reason for every skip. Without it a run that touched nothing reads as success.

## What is verified, per service

| Service | Covered |
| --- | --- |
| PostgreSQL | RLS isolation both directions, `WITH CHECK`, fail-closed with no tenant set, JSONB round trip and containment, concurrent duplicate inserts, pool saturation, connection loss mid-transaction, pool recovery, the migration chain from empty, downgrade to base, upgrade/downgrade round-trip stability, per-revision downgrade, `FORCE` presence, policy null-safety |
| Redis | Cache round trip, cross-tenant key isolation, namespace invalidation, durable enqueue visible to a second client, concurrent workers never double-claim, abandoned claims reclaimed |
| Neo4j | Health check, write visible to a separate read session, tenant-scoped query isolation, failed transaction leaves no partial graph |
| MongoDB | Health check, nested document round trip, `TenantScopedCollection` isolation for read/count/delete, `ensure_indexes` idempotence |
| Kafka | Producer has cluster metadata, `acks=all` publish acknowledged, `Event` publish, unreachable broker fails loudly rather than dropping silently |
| Outbox (Postgres + Kafka) | Append-and-relay end to end, rollback publishes nothing, concurrent relays never duplicate, the relay sees every tenant, a tenant session sees only its own rows |

## What is still not verified

Stated plainly, because an unstated gap reads as coverage.

- **Managed services.** CI runs containers. MongoDB Atlas vector search, Neo4j Aura, and a managed
  Kafka behave differently under failover and network partition, and none of that is exercised.
- **Atlas `$vectorSearch`.** The production vector path (ADR-0009) needs an Atlas cluster; the
  container has no vector index. The tests here cover MongoDB as a document store only.
- **Multi-broker Kafka.** One broker, replication factor 1. Per-tenant ordering across partitions
  on multiple brokers (ADR-0026) is untested, as is leader failover.
- **Neo4j clustering.** Single instance, so `read_session`'s READ access mode routes nowhere and
  the read/write split is unproven.
- **Kubernetes.** There is no staging cluster. The manifests are checked statically by
  `cip_gateway.deployment` and have never been applied.
- **Sustained load.** The concurrency tests use tens of operations, not thousands. They prove the
  mechanism, not the behaviour at production volume.
