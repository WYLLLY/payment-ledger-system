"""Thin HTTP handlers; credentials are never reflected in validation diagnostics."""

from typing import Annotated, cast
from uuid import UUID

from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from payment_ledger import services
from payment_ledger.config import Settings
from payment_ledger.errors import (
    AlreadyExists,
    AuthenticationFailed,
    RateLimitExceeded,
    ResourceNotFound,
)
from payment_ledger.health import get_database_engine
from payment_ledger.ledger.history import HistoryEntry, history
from payment_ledger.schemas import (
    LoginRequest,
    RegistrationRequest,
    SessionResponse,
    UserResponse,
    WalletRequest,
    WalletResponse,
)
from payment_ledger.transfers import service as transfers
from payment_ledger.transfers.schemas import TransferRequest, TransferResponse

router = APIRouter()
EngineDependency = Annotated[Engine, Depends(get_database_engine)]
bearer = HTTPBearer(auto_error=False)


def configuration(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


SettingsDependency = Annotated[Settings, Depends(configuration)]


def auth_rate(request: Request, engine: EngineDependency, settings: SettingsDependency) -> None:
    peer = request.client.host if request.client is not None else "unknown"
    services.enforce_auth_rate(
        engine, peer, settings.auth_rate_limit, settings.auth_rate_window_seconds
    )


def require_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if credentials is None:
        raise AuthenticationFailed
    return credentials.credentials


TokenDependency = Annotated[str, Depends(require_token)]


def current_user(engine: EngineDependency, token: TokenDependency) -> UserResponse:
    return services.authenticated_user(engine, token)


UserDependency = Annotated[UserResponse, Depends(current_user)]


@router.post("/auth/register", status_code=201, dependencies=[Depends(auth_rate)])
def register(
    request: RegistrationRequest, engine: EngineDependency, response: Response
) -> UserResponse:
    response.headers["Cache-Control"] = "no-store"
    return services.register(engine, request)


@router.post("/auth/login", dependencies=[Depends(auth_rate)])
def login(
    request: LoginRequest,
    engine: EngineDependency,
    settings: SettingsDependency,
    response: Response,
) -> SessionResponse:
    response.headers["Cache-Control"] = "no-store"
    return services.login(engine, request, settings.session_ttl_seconds)


@router.post("/auth/logout", status_code=204)
def logout(engine: EngineDependency, token: TokenDependency, response: Response) -> None:
    services.logout(engine, token)
    response.headers["Cache-Control"] = "no-store"


@router.get("/users/me")
def me(user: UserDependency, response: Response) -> UserResponse:
    response.headers["Cache-Control"] = "no-store"
    return user


@router.post("/wallets", status_code=201)
def create_wallet(
    request: WalletRequest,
    user: UserDependency,
    engine: EngineDependency,
) -> WalletResponse:
    return services.create_wallet(engine, user.id, request.currency)


@router.get("/wallets")
def list_wallets(user: UserDependency, engine: EngineDependency) -> list[WalletResponse]:
    return services.list_wallets(engine, user.id)


@router.get("/wallets/{wallet_id}")
def get_wallet(wallet_id: UUID, user: UserDependency, engine: EngineDependency) -> WalletResponse:
    return services.get_wallet(engine, user.id, wallet_id)


@router.get("/wallets/{wallet_id}/ledger")
def wallet_history(
    wallet_id: UUID,
    user: UserDependency,
    engine: EngineDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
) -> list[HistoryEntry]:
    return history(engine, user.id, wallet_id, limit, offset)


@router.post(
    "/transfers",
    status_code=201,
    response_model=TransferResponse,
    responses={
        404: {"description": "Resource not found"},
        409: {"description": "Key conflict or insufficient funds"},
    },
)
def create_transfer(
    request: TransferRequest,
    user: UserDependency,
    engine: EngineDependency,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128, pattern=r"^[!-~]+$")],
) -> JSONResponse:
    result = transfers.create_transfer(engine, user.id, idempotency_key, request)
    return JSONResponse(
        status_code=result.status_code, content=result.body, headers={"Cache-Control": "no-store"}
    )


@router.get("/transfers/{transfer_id}")
def get_transfer(
    transfer_id: UUID, user: UserDependency, engine: EngineDependency
) -> TransferResponse:
    return transfers.get_transfer(engine, user.id, transfer_id)


@router.get("/transfers")
def list_transfers(
    user: UserDependency,
    engine: EngineDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
) -> list[TransferResponse]:
    return transfers.list_transfers(engine, user.id, limit, offset)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthenticationFailed)
    async def authentication_error(request: Request, error: AuthenticationFailed) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid credentials or session"},
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )

    @app.exception_handler(AlreadyExists)
    async def conflict_error(request: Request, error: AlreadyExists) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(ResourceNotFound)
    async def missing_error(request: Request, error: ResourceNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Resource not found"})

    @app.exception_handler(RateLimitExceeded)
    async def rate_error(request: Request, error: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many authentication attempts"},
            headers={"Retry-After": str(error.retry_after), "Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        "loc": [
                            part
                            if part
                            in {
                                "body",
                                "query",
                                "path",
                                "email",
                                "password",
                                "currency",
                                "wallet_id",
                            }
                            else "field"
                            for part in item["loc"]
                        ],
                        "type": item["type"],
                        "msg": "Invalid request field",
                    }
                    for item in error.errors()
                ]
            },
        )

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(VerificationError)
    @app.exception_handler(InvalidHashError)
    async def storage_error(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "Service temporarily unavailable"})
