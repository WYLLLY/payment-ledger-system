"""SQLAlchemy persistence mappings. Alembic owns schema and privilege changes."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, LargeBinary, String, Text, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "payment"}

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    email: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginSession(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": "payment"}

    token_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("payment.users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = {"schema": "payment"}

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("payment.users.id"))
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"
    __table_args__ = {"schema": "payment"}

    id: Mapped[UUID] = mapped_column(primary_key=True)
    wallet_id: Mapped[UUID | None] = mapped_column(ForeignKey("payment.wallets.id"))
    currency: Mapped[str] = mapped_column(String(3))
    account_kind: Mapped[str] = mapped_column(Text)
    balance_minor: Mapped[int] = mapped_column(BigInteger)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = {"schema": "payment"}

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("payment.users.id"))
    event_type: Mapped[str] = mapped_column(Text)
    resource_id: Mapped[UUID | None]
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
