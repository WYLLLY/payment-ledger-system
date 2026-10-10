"""Application operations and their explicit PostgreSQL transaction boundaries."""

import hashlib
from datetime import timedelta
from uuid import UUID

from sqlalchemy import Engine, func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from payment_ledger.errors import (
    AlreadyExists,
    AuthenticationFailed,
    RateLimitExceeded,
    ResourceNotFound,
)
from payment_ledger.models import AuditEvent, LedgerAccount, LoginSession, User, Wallet
from payment_ledger.schemas import (
    AccountResponse,
    Currency,
    LoginRequest,
    RegistrationRequest,
    SessionResponse,
    UserResponse,
    WalletResponse,
)
from payment_ledger.security import hash_password, new_session_token, token_digest, verify_password


def enforce_auth_rate(engine: Engine, peer: str, limit: int, window_seconds: int) -> None:
    # Commit the attempt independently: failed authentication must consume a slot too.
    with engine.begin() as connection:
        retry_after = connection.execute(
            text("SELECT payment.take_auth_slot(:key, :limit, :window)"),
            {
                "key": hashlib.sha256(peer.encode()).digest(),
                "limit": limit,
                "window": window_seconds,
            },
        ).scalar_one()
    if retry_after:
        raise RateLimitExceeded(int(retry_after))


def _audit(session: Session, event: str, user_id: UUID | None) -> None:
    # No RETURNING: runtime may append audit events, but may not read or edit them.
    session.execute(insert(AuditEvent).inline().values(event_type=event, user_id=user_id))


def register(engine: Engine, request: RegistrationRequest) -> UserResponse:
    password_hash = hash_password(request.password)
    try:
        with Session(engine) as session, session.begin():
            user = User(email=str(request.email), password_hash=password_hash)
            session.add(user)
            session.flush()
            _audit(session, "user_registered", user.id)
            response = UserResponse.model_validate(user)
    except IntegrityError as error:
        if getattr(error.orig, "sqlstate", None) != "23505":
            raise
        raise AlreadyExists("Registration unavailable") from None
    return response


def login(engine: Engine, request: LoginRequest, ttl_seconds: int) -> SessionResponse:
    with Session(engine) as session:
        user = session.scalar(select(User).where(User.email == str(request.email)))
        user_id = user.id if user is not None else None
        stored_hash = user.password_hash if user is not None else None
    # Expensive password verification takes place without an open DB transaction.
    if not verify_password(request.password, stored_hash):
        with Session(engine) as session, session.begin():
            _audit(session, "login_failed", user_id)
        raise AuthenticationFailed
    token = new_session_token()
    with Session(engine) as session, session.begin():
        expires_at = session.scalar(
            select(func.statement_timestamp() + timedelta(seconds=ttl_seconds))
        )
        session.execute(
            insert(LoginSession)
            .inline()
            .values(token_hash=token_digest(token), user_id=user_id, expires_at=expires_at)
        )
        _audit(session, "login_succeeded", user_id)
        response = SessionResponse(access_token=token, expires_at=expires_at)
    return response


def authenticated_user(engine: Engine, token: str) -> UserResponse:
    digest = token_digest(token)
    if digest is None:
        raise AuthenticationFailed
    with Session(engine) as session:
        user = session.scalar(
            select(User)
            .join(LoginSession)
            .where(
                LoginSession.token_hash == digest,
                LoginSession.revoked_at.is_(None),
                LoginSession.expires_at > func.statement_timestamp(),
            )
        )
        if user is None:
            raise AuthenticationFailed
        return UserResponse.model_validate(user)


def logout(engine: Engine, token: str) -> None:
    digest = token_digest(token)
    if digest is None:
        raise AuthenticationFailed
    with engine.begin() as connection:
        user_id = connection.execute(
            text("SELECT payment.revoke_session(:token)"), {"token": digest}
        ).scalar_one()
        if user_id is None:
            raise AuthenticationFailed


def _wallet_response(session: Session, wallet: Wallet) -> WalletResponse:
    accounts = session.scalars(
        select(LedgerAccount)
        .where(LedgerAccount.wallet_id == wallet.id)
        .order_by(LedgerAccount.account_kind)
    )
    return WalletResponse(
        id=wallet.id,
        currency=wallet.currency,
        created_at=wallet.created_at,
        accounts=[AccountResponse.model_validate(account) for account in accounts],
    )


def create_wallet(engine: Engine, user_id: UUID, currency: Currency) -> WalletResponse:
    try:
        with Session(engine) as session, session.begin():
            wallet_id = session.execute(
                text("SELECT payment.create_wallet(:owner, :currency)"),
                {"owner": user_id, "currency": currency},
            ).scalar_one()
            wallet = session.get(Wallet, wallet_id)
            assert wallet is not None
            response = _wallet_response(session, wallet)
    except IntegrityError as error:
        if getattr(error.orig, "sqlstate", None) != "23505":
            raise
        raise AlreadyExists("Wallet already exists for this currency") from None
    return response


def get_wallet(engine: Engine, user_id: UUID, wallet_id: UUID) -> WalletResponse:
    with Session(engine) as session:
        wallet = session.scalar(
            select(Wallet).where(Wallet.id == wallet_id, Wallet.user_id == user_id)
        )
        if wallet is None:
            raise ResourceNotFound
        return _wallet_response(session, wallet)


def list_wallets(engine: Engine, user_id: UUID) -> list[WalletResponse]:
    with Session(engine) as session:
        wallets = session.scalars(
            select(Wallet).where(Wallet.user_id == user_id).order_by(Wallet.id)
        )
        return [_wallet_response(session, wallet) for wallet in wallets]
