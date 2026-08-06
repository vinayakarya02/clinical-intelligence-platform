.PHONY: help install services-up services-down migrate api cli demo test coverage test-role test-integration lint type-check check clean

PY ?= .venv/bin/python
ifeq ($(OS),Windows_NT)
	PY = .venv/Scripts/python.exe
endif

help:
	@echo "install           Create venv and install runtime + dev dependencies"
	@echo "services-up       Start Postgres/Mongo/Neo4j/Redis/Kafka via docker compose"
	@echo "services-down     Stop local backing services"
	@echo "migrate           Apply Alembic migrations to the configured database"
	@echo "api               Run the ingestion API with autoreload on :8000"
	@echo "demo              Run the Phase 2 end-to-end retrieval verification + benchmarks"
	@echo "test              Run the unit test suite (no external services required)"
	@echo "coverage          Unit suite with coverage across all nine packages, floor 75%"
	@echo "test-role         Create the non-superuser role the RLS tests require"
	@echo "test-integration  Run integration tests against live backing services"
	@echo "lint              Ruff lint + format check"
	@echo "type-check        pyright"
	@echo "check             lint + type-check + test"

install:
	python -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev,s3]"

# -f, because the compose file lives in docker/. Without it these targets picked up a root
# docker-compose.yml that was a Phase 1 relic: no Redis, no Kafka, and different passwords. A
# developer running `make services-up` got a stack the integration suite could not use, which is
# gap M-13 in docs/design/phase-9-design.md. That file is gone; this is the only one.
COMPOSE = docker compose -f docker/docker-compose.yml

services-up:
	$(COMPOSE) up -d
	$(COMPOSE) ps

services-down:
	$(COMPOSE) down

migrate:
	$(PY) -m cip_ingestion.cli db upgrade

api:
	$(PY) -m uvicorn cip_ingestion.api.app:create_app --factory --reload --port 8000

demo:
	$(PY) -m cip_retrieval.demo

test:
	$(PY) -m pytest -q

# The same nine packages and the same floor CI enforces, so a coverage regression is visible
# before it is a failed build rather than after.
coverage:
	$(PY) -m pytest -q \
		--cov=cip_core --cov=cip_platform \
		--cov=cip_ingestion --cov=cip_retrieval --cov=cip_copilot --cov=cip_gateway \
		--cov=cip_decision --cov=cip_interop --cov=cip_analytics \
		--cov-report=term-missing --cov-fail-under=75

# The role the RLS tests need. A superuser bypasses row-level security unconditionally, so
# running these tests as one proves nothing about isolation — the CI job made exactly that
# mistake for several runs. Run once after `services-up` and `migrate`; it is idempotent.
test-role:
	$(COMPOSE) exec -T postgres psql -U cip -d cip -v ON_ERROR_STOP=1 \
		-f /dev/stdin < scripts/create_test_role.sql

# -rs prints the reason for every skip. Without it a run that touched nothing reports "56
# skipped" and reads as success. --integration-min and the guard in tests/conftest.py turn a
# suite that did not run into a failure rather than a quiet pass.
#
# Credentials match docker/docker-compose.yml, except the PostgreSQL role: `cip` is a superuser
# and bypasses row-level security, so the suite connects as `cip_app` from `make test-role`.
test-integration:
	CIP_RUN_INTEGRATION=1 \
	CIP_POSTGRES__HOST=localhost CIP_POSTGRES__PORT=5432 CIP_POSTGRES__DATABASE=cip \
	CIP_POSTGRES__USER=cip_app CIP_POSTGRES__PASSWORD=ci_app \
	CIP_REDIS_URL=redis://localhost:6379/0 \
	CIP_MONGO__URI="mongodb://cip:devpassword@localhost:27017/cip?authSource=admin" \
	CIP_NEO4J__URI=bolt://localhost:7687 \
	CIP_NEO4J__USER=neo4j CIP_NEO4J__PASSWORD=devpassword \
	CIP_EVENTS_BROKER_URL=localhost:9092 \
	$(PY) -m pytest -q -rs -m integration --integration-min=54

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

# pyright rather than mypy: mypy's binary is blocked by Windows Application Control policy
# on the development machine. The mypy config in pyproject.toml is kept current so other
# environments can run either.
type-check:
	$(PY) -m pyright

check: lint type-check test

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
