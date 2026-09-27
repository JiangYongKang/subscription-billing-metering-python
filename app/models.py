"""领域模型：枚举、实体与 API 入参 schema。

金额一律以整数「分」存储，避免浮点误差。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class PlanTier(str, Enum):
    BASIC = "basic"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"          # 正常
    PAST_DUE = "past_due"      # 账单逾期，重试/宽限期内
    CANCELED = "canceled"      # 已取消（可能当账期仍有效）


class InvoiceStatus(str, Enum):
    DRAFT = "draft"            # 当前账期进行中，可随变更/用量重算
    OPEN = "open"              # 已出账，等待扣款
    PAID = "paid"              # 已付清
    FAILED = "failed"          # 重试耗尽
    VOID = "void"              # 作废（取消当账期时的零处理等）


class LineKind(str, Enum):
    BASE = "base"                       # 套餐基础费（按比例段）
    OVERAGE = "overage"                 # 超额用量费
    PRORATION_CREDIT = "proration_credit"  # 变更产生的负数调整行


class FailureReason(str, Enum):
    NONE = "none"
    INSUFFICIENT_FUNDS = "insufficient_funds"  # 余额不足（可重试）
    CARD_DECLINED = "card_declined"            # 卡片失效（不可重试）
    RETRY_EXHAUSTED = "retry_exhausted"        # 重试耗尽
    UNKNOWN = "unknown"


RETRYABLE_REASONS = {FailureReason.INSUFFICIENT_FUNDS, FailureReason.UNKNOWN}
PERMANENT_REASONS = {FailureReason.CARD_DECLINED}


class TxnStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# 实体（dataclass，内存存储）
# ----------------------------------------------------------------
@dataclass(frozen=True)
class Plan:
    tier: PlanTier
    name: str
    base_price_cents: int          # 每账期基础费（分）
    included_units: int            # 每账期包含用量额度
    overage_price_cents: int       # 超额部分单价（分/单位）


@dataclass
class PlanSegment:
    """一个账期内套餐有效的时间片段，用于按比例计费。"""
    tier: PlanTier
    start: date          # 含
    end: date            # 不含
    note: str = ""


@dataclass
class LineItem:
    kind: LineKind
    tier: PlanTier | None
    description: str
    amount_cents: int
    units: int = 0
    meta: dict = field(default_factory=dict)


@dataclass
class UsageRecord:
    """去重后的一条用量事件。"""
    id: str
    subscription_id: str
    period_start: date
    event_id: str
    quantity: int
    occurred_on: date


@dataclass
class Transaction:
    """模拟支付网关上的一笔扣款流水。"""
    id: str
    invoice_id: str
    amount_cents: int
    status: TxnStatus
    attempt: int
    reason: FailureReason
    created_on: date
    idempotency_key: str


@dataclass
class Invoice:
    id: str
    subscription_id: str
    period_start: date
    period_end: date
    status: InvoiceStatus
    line_items: list[LineItem] = field(default_factory=list)
    total_cents: int = 0
    issued_on: date | None = None
    paid_on: date | None = None
    attempts: int = 0
    last_failure_reason: FailureReason = FailureReason.NONE
    next_attempt_on: date | None = None
    finalized: bool = False  # = 已离开 DRAFT，历史账期冻结


@dataclass
class Subscription:
    id: str
    tier: PlanTier
    status: SubscriptionStatus
    created_on: date
    current_period_start: date
    current_period_end: date
    segments: list[PlanSegment] = field(default_factory=list)
    cancel_on: date | None = None       # 立即取消时为取消日
    cancel_effective_period_end: bool = False
    grace_ends_on: date | None = None   # 宽限期截止日（含）
    payment_mode: dict = field(default_factory=dict)  # 模拟网关场景配置


class BillingError(Exception):
    """可向调用方解释的业务错误。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# API 入参 schema
# ---------------------------------------------------------------------------
class CreateSubscriptionIn(BaseModel):
    tier: PlanTier
    start_date: date | None = None
    # 模拟支付场景：insufficient_funds / card_declined / ok
    payment_scenario: str = "ok"
    fail_times: int = 0  # 前 N 次扣款返回余额不足，之后成功


class ChangePlanIn(BaseModel):
    tier: PlanTier
    effective_date: date | None = None


class CancelIn(BaseModel):
    effective_date: date | None = None
    at_period_end: bool = True


class UsageIn(BaseModel):
    event_id: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    occurred_on: date | None = None


class RunBillingIn(BaseModel):
    as_of: date | None = None


class ChargeIn(BaseModel):
    idempotency_key: str | None = None
    as_of: date | None = None
