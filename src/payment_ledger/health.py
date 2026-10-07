"""HTTP health contracts; database checks live in the infrastructure module."""

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import Engine

from payment_ledger.database import database_is_ready

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]


def get_database_engine(request: Request) -> Engine:
    return cast(Engine, request.state.db_engine)


@router.get("/live")
def live() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", responses={503: {"model": HealthResponse}})
def ready(
    response: Response, engine: Annotated[Engine, Depends(get_database_engine)]
) -> HealthResponse:
    if not database_is_ready(engine):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="unavailable")
    return HealthResponse(status="ok")
