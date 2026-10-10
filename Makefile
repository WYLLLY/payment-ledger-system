UV ?= uv
.PHONY: lint format-check typecheck test test-unit test-integration test-ledger migrate seed-demo reconcile
lint:
	$(UV) run --frozen ruff check .
format-check:
	$(UV) run --frozen ruff format --check .
typecheck:
	$(UV) run --frozen mypy
test:
	$(UV) run --frozen pytest
test-unit:
	$(UV) run --frozen pytest tests/unit
test-integration:
	$(UV) run --frozen pytest tests/integration
test-ledger:
	$(UV) run --frozen pytest tests/integration/test_ledger.py
migrate:
	$(UV) run --frozen alembic upgrade head
seed-demo:
	$(UV) run --frozen python scripts/seed_demo.py
reconcile:
	$(UV) run --frozen python -m payment_ledger.ledger.reconciliation
