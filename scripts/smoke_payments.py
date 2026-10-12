"""Opt-in dev/CI: real Compose API → outbox worker → HTTP provider → terminal ledger.

Leaves one fictitious funded wallet and immutable payment history. Never run on
a demonstration deployment. Migration credentials are used only by this fixture
CLI, not by the API or worker.
"""

import os
import secrets
from threading import Event
from time import monotonic
from uuid import uuid4

import psycopg

from payment_ledger.config import Settings
from payment_ledger.database import create_database_engine
from payment_ledger.ledger.reconciliation import reconcile
from payment_ledger.schemas import WalletResponse
from scripts.seed_demo import Credentials, provision_dev_role
from scripts.smoke_api import call


def main() -> None:
    if os.environ.get("PAYMENT_LEDGER_DEV_FIXTURES") != "1":
        raise SystemExit("Requires PAYMENT_LEDGER_DEV_FIXTURES=1 on a dedicated dev/CI cluster")
    settings = Settings()
    credentials = Credentials()
    login: dict[str, object] = {
        "email": f"payment-smoke-{uuid4().hex}@example.com",
        "password": secrets.token_urlsafe(24),
    }
    owner = call("/auth/register", method="POST", payload=login, expected=201)
    token = str(call("/auth/login", method="POST", payload=login)["access_token"])
    try:
        wallet = call(
            "/wallets", method="POST", payload={"currency": "EUR"}, token=token, expected=201
        )
        with psycopg.connect(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user="payment_migrator",
            password=credentials.migration_db_password.get_secret_value(),
            connect_timeout=3,
        ) as connection:
            provision_dev_role(connection)
            connection.execute("SET LOCAL ROLE payment_dev")
            connection.execute(
                "SELECT payment.open_fixture_wallet(%s,%s)", (owner["id"], wallet["id"])
            )
        request = {
            "wallet_id": wallet["id"],
            "recipient": "compose-smoke",
            "currency": "EUR",
            "amount_minor": 1500,
        }
        payment = call(
            "/payments",
            method="POST",
            payload=request,
            token=token,
            idempotency_key="compose-smoke",
            expected=202,
        )
        assert payment["state"] == "pending"
        deadline = monotonic() + 20
        while True:
            current = call(f"/payments/{payment['id']}", token=token)
            if current["state"] == "succeeded":
                break
            assert current["state"] == "pending" and monotonic() < deadline, current["state"]
            Event().wait(0.1)  # Bounded service readiness polling, not a concurrency assertion.
        assert (
            call(
                "/payments",
                method="POST",
                payload=request,
                token=token,
                idempotency_key="compose-smoke",
                expected=202,
            )
            == payment
        )
        current_wallet = WalletResponse.model_validate(
            call(f"/wallets/{wallet['id']}", token=token)
        )
        assert {
            account.account_kind: account.balance_minor for account in current_wallet.accounts
        } == {"available": 98500, "reserved": 0}
        engine = create_database_engine(settings)
        try:
            assert reconcile(engine) == []
        finally:
            engine.dispose()
    finally:
        call("/auth/logout", method="POST", token=token, expected=204)
    print(
        "PASS: Compose payment reservation, asynchronous HTTP settlement, replay and reconciliation"
    )


if __name__ == "__main__":
    main()
