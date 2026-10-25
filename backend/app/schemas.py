from decimal import Decimal
from typing import Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field

Money = Annotated[Decimal, Field(ge=0, le=100000000000, decimal_places=2, allow_inf_nan=False)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Strict):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class CompanyEdit(Strict):
    name: str = Field(min_length=2, max_length=200)
    industry: str = Field(min_length=2, max_length=100)
    revenue: Money
    expenses: Money
    monthly_debt: Money


class Calculation(Strict):
    product_id: Literal["working", "equipment"]
    amount: Money
    months: int = Field(ge=1, le=120)


class ApplicationCreate(Calculation):
    purpose: str = Field(min_length=3, max_length=1000)
    revenue: Money | None = None
    expenses: Money | None = None
    monthly_debt: Money | None = None


class ApplicationEdit(ApplicationCreate):
    version: int = Field(ge=1)


class Action(Strict):
    action: Literal[
        "submit", "retry", "respond", "approve", "reject", "offer", "request_info", "accept", "decline", "sign", "disburse", "cancel"
    ]
    version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=1000)
    message: str | None = Field(default=None, max_length=1000)
    amount: Money | None = None
    months: int | None = Field(default=None, ge=1, le=120)
    revenue: Money | None = None
    expenses: Money | None = None
    monthly_debt: Money | None = None


class NoteCreate(Strict):
    message: str = Field(min_length=1, max_length=2000)
