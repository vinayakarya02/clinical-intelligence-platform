.PHONY: help install services-up services-down migrate api cli demo test test-integration lint type-check check clean

PY ?= .venv/bin/python
ifeq ($(OS),Windows_NT)
	PY = .venv/Scripts/python.exe
endif

help:
	@echo "install           Create venv and install runtime + dev dependencies"
	@echo "services-up       Start local Postgres/Mongo/Neo4j via docker compose"
	@echo "services-down     Stop local backing services"
	@echo "migrate           Apply Alembic migrations to the configured database"
	@echo "api               Run the ingestion API with autoreload on :8000"
	@echo "demo              Run the Phase 2 end-to-end retrieval verification + benchmarks"
	@echo "test              Run the unit test suite (no external services required)"
	@echo "test-integration  Run integration tests against live backing services"
	@echo "lint              Ruff lint + format check"
	@echo "type-check        pyright"
	@echo "check             lint + type-check + test"

install:
	python -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev,s3]"

services-up:
	docker compose up -d
	docker compose ps

services-down:
	docker compose down

migrate:
	$(PY) -m cip_ingestion.cli db upgrade

api:
	$(PY) -m uvicorn cip_ingestion.api.app:create_app --factory --reload --port 8000

demo:
	$(PY) -m cip_retrieval.demo

test:
	$(PY) -m pytest -q

test-integration:
	CIP_RUN_INTEGRATION=1 $(PY) -m pytest -q -m integration

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
