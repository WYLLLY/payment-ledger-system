"""Validated API inputs and explicit public projections without credential hashes."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

type Currency = Literal["EUR", "USD", "JPY"]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        if not value.isascii():
            raise ValueError("Use an ASCII email address")
        return value.lower()


class RegistrationRequest(LoginRequest):
    password: SecretStr = Field(min_length=12, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: str
    created_at: datetime


class SessionResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime


class WalletRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: Currency


class AccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    account_kind: Literal["available", "reserved"]
    balance_minor: int


class WalletResponse(BaseModel):
    id: UUID
    currency: Currency
    created_at: datetime
    accounts: list[AccountResponse]
