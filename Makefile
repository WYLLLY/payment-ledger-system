UV ?= uv
.PHONY: lint format-check typecheck test test-unit test-integration test-ledger test-transfers test-payments migrate seed-demo reconcile
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
test-transfers:
	$(UV) run --frozen pytest tests/integration/test_transfers.py
test-payments:
	$(UV) run --frozen pytest tests/integration/test_payments.py tests/unit/test_payment_provider.py
migrate:
	$(UV) run --frozen alembic upgrade head
seed-demo:
	$(UV) run --frozen python scripts/seed_demo.py
reconcile:
	$(UV) run --frozen python -m payment_ledger.ledger.reconciliation
