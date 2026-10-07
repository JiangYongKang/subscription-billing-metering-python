"""HTTP 请求/响应 Pydantic 模型。"""

from datetime import datetime

from pydantic import BaseModel, Field


class PlanIn(BaseModel):
    code: str
    name: str
    base_price_cents: int = Field(ge=0)
    included_units: int = Field(ge=0)
    overage_unit_price_cents: int = Field(ge=0)
    currency: str = "CNY"


class AccountIn(BaseModel):
    customer_id: str
    balance_cents: int = 0
    card_active: bool = True


class SubscriptionIn(BaseModel):
    customer_id: str
    plan_code: str
    anchor_day: int = Field(default=1, ge=1, le=28)
    idempotency_key: str
    created_at: datetime | None = None


class PlanChangeIn(BaseModel):
    new_plan_code: str
    idempotency_key: str
    action: str | None = None


class CancelIn(BaseModel):
    effective: str = "immediate"
    idempotency_key: str


class UsageIn(BaseModel):
    event_id: str
    quantity: int = Field(gt=0)
    occurred_at: datetime
    idempotency_key: str = ""


class ChargeIn(BaseModel):
    pass


class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)
    idempotency_key: str


class RefundIn(BaseModel):
    amount_cents: int = Field(gt=0)
    idempotency_key: str
    reason: str = ""


class ClockIn(BaseModel):
    now: datetime
