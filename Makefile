.PHONY: fmt lint test test-unit test-integration up down coverage-check

fmt:
	ruff format src tests
	ruff check --fix src tests

lint:
	ruff format --check src tests
	ruff check src tests
	mypy --strict src/

test-unit:
	pytest tests/unit -m unit -v --cov=src/hc/queue --cov=src/hc/cli --cov=src/hc/models --cov-report=term-missing --cov-report=json

test-integration:
	pytest tests/integration -m integration -v --cov=src/hc/queue --cov-report=term-missing --cov-append

test: test-unit test-integration

coverage-check:
	python scripts/check_coverage.py --min-queue 85

up:
	docker compose up -d redis postgres

down:
	docker compose down -v
