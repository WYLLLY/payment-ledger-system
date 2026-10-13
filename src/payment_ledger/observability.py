"""Allowlisted JSON events: no headers, query strings, payloads or exception text."""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, event, text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
logger = logging.getLogger("payment_ledger.events")
FIELDS = ("payment_id", "generation", "outcome", "applied", "status", "route", "duration_ms")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", correlation_id.get()),
        }
        value.update({key: getattr(record, key) for key in FIELDS if hasattr(record, key)})
        return json.dumps(value, separators=(",", ":"))


def configure_logging() -> None:
    package = logging.getLogger("payment_ledger")
    if not any(handler.name == "payment_json" for handler in package.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler.name = "payment_json"
        package.addHandler(handler)
    package.setLevel(logging.INFO)
    package.propagate = False


def correlate_transactions(engine: Engine) -> None:
    @event.listens_for(engine, "begin")
    def set_correlation(connection: Connection) -> None:
        connection.execute(
            text("SELECT set_config('payment.correlation_id', :id, true)"),
            {"id": correlation_id.get() or ""},
        )


class RequestLogging:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = next((v for k, v in scope["headers"] if k.lower() == b"x-request-id"), b"")
        try:
            identifier = str(UUID(raw.decode("ascii")))
        except ValueError, UnicodeError:
            identifier = str(uuid4())
        token = correlation_id.set(identifier)
        start = monotonic()
        status = 500
        started = False

        async def response(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ] + [(b"x-request-id", identifier.encode())]
            await send(message)

        try:
            await self.app(scope, receive, response)
        except Exception:
            # Do not let server traceback logging disclose SQL parameters or credentials.
            logger.error("request_failed")
            if started:
                raise RuntimeError("Response interrupted") from None
            await response(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await response({"type": "http.response.body", "body": b'{"detail":"Internal error"}'})
        finally:
            route = scope.get("route")
            logger.info(
                "request_completed",
                extra={
                    "route": getattr(route, "path", "unmatched"),
                    "status": status,
                    "duration_ms": round((monotonic() - start) * 1000),
                },
            )
            correlation_id.reset(token)
