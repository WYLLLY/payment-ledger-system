"""Internal CLI: metrics, reconciliation, provider lookup and safe recovery scheduling."""

import argparse
import json
from uuid import UUID, uuid4

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.exc import SQLAlchemyError

from payment_ledger.observability import configure_logging, correlation_id, logger
from payment_ledger.operations import service
from payment_ledger.payments.provider import ProviderClient


class OperatorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPERATOR_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    db_host: str = "127.0.0.1"
    db_port: int = Field(default=15432, ge=1, le=65535)
    db_name: str = "payment_ledger"
    db_password: SecretStr = Field(min_length=1)
    provider_url: str = "http://127.0.0.1:8001"
    http_timeout: float = Field(default=3, gt=0, le=10)


def create_operator_engine(settings: OperatorSettings) -> Engine:
    return create_engine(
        URL.create(
            "postgresql+psycopg",
            username="payment_operator_login",
            password=settings.db_password.get_secret_value(),
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
        ),
        isolation_level="READ COMMITTED",
        hide_parameters=True,
        pool_timeout=3,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=3000"},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("metrics")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument(
        "--limit", type=int, default=100, choices=range(1, 1001), metavar="1..1000"
    )
    reconcile.add_argument("--after", type=UUID)
    for command in ("lookup", "retry"):
        sub = commands.add_parser(command)
        sub.add_argument("payment_id", type=UUID)
        sub.add_argument("--reason", required=True, help="Ticket reference; never include secrets")
    args = parser.parse_args()
    configure_logging()
    identifier = uuid4()
    correlation_id.set(str(identifier))
    settings = OperatorSettings()
    engine = create_operator_engine(settings)
    provider = ProviderClient(settings.provider_url, settings.http_timeout)
    try:
        report: dict[str, object]
        status = 0
        if args.command == "metrics":
            report = {"metrics": service.snapshot(engine)["metrics"]}
        elif args.command == "reconcile":
            report = dict(service.snapshot(engine))
            external = service.reconcile_external(
                engine, provider, after=args.after, limit=args.limit
            )
            report["external"] = external
            status = int(bool(report["issues"] or external["findings"] or not external["complete"]))
            report["status"] = "ATTENTION" if status else "PASS"
        elif args.command == "lookup":
            report = {
                "outcome": service.lookup(
                    engine, provider, args.payment_id, args.reason, identifier
                )
            }
        else:
            report = {
                "result": service.action(engine, args.payment_id, "retry", args.reason, identifier)
            }
        print(json.dumps({"correlation_id": str(identifier), **report}))
        logger.info("operator_command_completed")
        return status
    except SQLAlchemyError:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "detail": "Operator command unavailable",
                    "correlation_id": str(identifier),
                }
            )
        )
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
