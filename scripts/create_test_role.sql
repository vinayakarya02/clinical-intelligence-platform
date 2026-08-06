-- The database role the integration suite connects as.
--
-- Run after `alembic upgrade head`, because it reassigns ownership of the migrated tables. Both
-- CI and `make test-role` run this same file, which is the point: it lived only inside a heredoc
-- in .github/workflows/ci.yml, so a developer running the suite locally connected as the
-- superuser the compose stack creates — and under a superuser every RLS assertion below passes
-- without proving anything. Two environments, one of them silently vacuous, is the shape of
-- defect this whole workstream exists to remove.
--
-- Idempotent, so it is safe to re-run against a stack that is already set up.

-- NOSUPERUSER and NOBYPASSRLS are the load-bearing attributes. A superuser bypasses row-level
-- security unconditionally, and FORCE ROW LEVEL SECURITY does not change that: the RLS tests
-- connected as one for several runs and every policy was silently inert while the tests passed.
--
-- CREATEDB, because tests/integration/test_migrations.py builds a throwaway database per test
-- and runs the real chain against it. It has to: a migration test that downgrades the shared
-- database leaves every test after it against a schema that is half gone. CREATEDB grants
-- nothing *inside* a database and weakens neither of the attributes above.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cip_app') THEN
    CREATE ROLE cip_app LOGIN PASSWORD 'ci_app' NOSUPERUSER NOBYPASSRLS CREATEDB;
  ELSE
    ALTER ROLE cip_app LOGIN PASSWORD 'ci_app' NOSUPERUSER NOBYPASSRLS CREATEDB;
  END IF;
END $$;

-- CREATE on the database, because some fixtures create schemas. Without it the role connects,
-- fails on the first DDL with "permission denied for database", and every integration test
-- *skips* — which is how this suite ran green and inert for several runs.
GRANT CREATE ON DATABASE cip TO cip_app;

-- Every schema the migrations create, not just public. Migration 0001 creates `platform`, and
-- granting only on `public` left the role unable to use it — which surfaced as "permission
-- denied for schema platform" on 50 of the fixtures.
DO $$
DECLARE s record;
BEGIN
  FOR s IN
    SELECT nspname FROM pg_namespace
    WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'
  LOOP
    EXECUTE format('GRANT USAGE, CREATE ON SCHEMA %I TO cip_app', s.nspname);
    EXECUTE format(
      'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO cip_app', s.nspname);
    EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO cip_app', s.nspname);
  END LOOP;
END $$;

-- Ownership, so FORCE ROW LEVEL SECURITY applies to this role. A non-owner is subject to
-- policies anyway; an owner is only subject to them under FORCE, which the migrations set.
-- Reassigning keeps the role's relationship to the tables identical to production's.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT schemaname, tablename FROM pg_tables
    WHERE schemaname NOT LIKE 'pg\_%' AND schemaname <> 'information_schema'
  LOOP
    EXECUTE format('ALTER TABLE %I.%I OWNER TO cip_app', r.schemaname, r.tablename);
  END LOOP;
END $$;

-- Printed so the job log carries the proof rather than the intention. rolsuper and rolbypassrls
-- must both be false; if either is true, every isolation assertion in the suite is vacuous.
SELECT rolname, rolsuper, rolbypassrls, rolcreatedb FROM pg_roles WHERE rolname = 'cip_app';
