"""核心领域模型与枚举（纯数据，不含业务逻辑）。"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SubscriptionState(str, Enum):
    ACTIVE = "active"              # 正常服务中
    PAST_DUE = "past_due"          # 扣款失败、重试与宽限期内
    CANCELED = "canceled"          # 用户主动取消（到期/立即）
    EXPIRED = "expired"            # 宽限期结束仍未付清，系统收敛


class BillStatus(str, Enum):
    OPEN = "open"                  # 账期进行中，可继续累加用量/变更
    FINALIZED = "finalized"        # 已出账，金额冻结，等待扣款
    PAID = "paid"                  # 已付清
    FAILED = "failed"             # 重试耗尽的终态失败
    CANCELED = "canceled"          # 作废（不发生资金）


class ChargeResult(str, Enum):
    SUCCESS = "success"
    INSUFFICIENT_FUNDS = "insufficient_funds"   # 可重试
    CARD_DECLINED = "card_declined"             # 卡片失效，终态
    GATEWAY_ERROR = "gateway_error"             # 可重试


class FailureReason(str, Enum):
    NONE = "none"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    CARD_DECLINED = "card_declined"
    GATEWAY_ERROR = "gateway_error"
    RETRY_EXHAUSTED = "retry_exhausted"         # 重试次数用尽
    NO_PAYMENT_METHOD = "no_payment_method"


class LedgerEntryType(str, Enum):
    CHARGE_SUCCESS = "charge_success"
    CHARGE_FAILURE = "charge_failure"
    PAYMENT = "payment"                    # 分次付款网关成功扣款
    CREDIT_APPLIED = "credit_applied"      # 退款额度抵扣账单
    CREDIT_RELEASED = "credit_released"    # 扣款失败，已抵扣额度回滚
    REFUND = "refund"                      # 退款（转为账户抵扣额度）


@dataclass
class Plan:
    code: str                                  # 套餐编码，如 basic/pro
    name: str
    base_price_cents: int                      # 每账期基础价（分）
    included_units: int                        # 每账期包含用量额度
    overage_unit_price_cents: int              # 超额部分单价（分/单位）
    currency: str = "CNY"
    version: int = 1                           # 价格版本，调价递增
    active: bool = True
    updated_at: datetime | None = None


@dataclass
class PlanChange:
    """同一账期内一次订阅变更（创建/升级/降级/取消）的按比例分段依据。"""

    id: str
    subscription_id: str
    period_index: int
    effective_at: datetime
    plan_code: str
    plan_version: int
    action: str                                # create/upgrade/downgrade/cancel
    base_price_cents: int                      # 变更时套餐价快照
    included_units: int
    overage_unit_price_cents: int
    idempotency_key: str
    proration_days: int = 0                    # 覆盖天数（出账时回填）
    proration_cents: int = 0                   # 按比例金额（出账时回填）
    note: str = ""


@dataclass
class Subscription:
    id: str
    customer_id: str
    plan_code: str
    plan_version: int
    state: SubscriptionState
    anchor_day: int                            # 账期锚点（每月几号出账）
    created_at: datetime
    canceled_at: datetime | None = None
    cancel_effective: str = "immediate"        # immediate / end_of_period
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    grace_until: datetime | None = None        # 宽限截止
    last_bill_id: str | None = None


@dataclass
class UsageEvent:
    event_id: str                              # 业务侧唯一，幂等去重键
    subscription_id: str
    period_index: int
    quantity: int
    occurred_at: datetime                      # 业务发生时间（可能乱序/延迟）
    recorded_at: datetime                      # 服务端落库时间
    billed: bool = False
    idempotency_key: str = ""


@dataclass
class BillLineItem:
    kind: str                                  # proration / included / overage / adjustment
    description: str
    amount_cents: int
    detail: dict = field(default_factory=dict)


@dataclass
class Bill:
    id: str
    subscription_id: str
    customer_id: str
    period_index: int
    period_start: datetime
    period_end: datetime
    currency: str
    status: BillStatus
    lines: list[BillLineItem] = field(default_factory=list)
    subtotal_cents: int = 0
    total_units: int = 0
    overage_units: int = 0
    created_at: datetime | None = None
    finalized_at: datetime | None = None
    paid_at: datetime | None = None
    failure_reason: FailureReason = FailureReason.NONE
    charge_attempts: int = 0
    idempotency_key: str = ""                  # 出账幂等键
    is_adjustment: bool = False                # 账后迟到用量调整单
    paid_cents: int = 0                        # 累计实收（网关+额度抵扣，总额口径，不因退款回退）
    refunded_cents: int = 0                    # 累计退款（退回账户抵扣额度）


@dataclass
class ChargeAttempt:
    id: str
    bill_id: str
    subscription_id: str
    attempt_no: int
    amount_cents: int
    result: ChargeResult
    reason_detail: str
    idempotency_key: str                       # 同一账单同一尝试的扣款幂等键
    attempted_at: datetime
    ledger_entry_id: str | None = None
    dedup_idempotency_key: str = ""            # 已付清账单上并发/重复调用复用的尝试键
    kind: str = "auto"                         # auto（整笔自动扣款）/ partial（分次付款）
    payment_id: str | None = None              # kind=partial 时关联的 Payment
    reversed: bool = False                     # 抵扣额度后网关失败，抵扣回滚标记


@dataclass
class Payment:
    """同一张已出账账单上的一笔分次付款（可重复直到付清）。"""

    id: str
    bill_id: str
    subscription_id: str
    customer_id: str
    amount_cents: int                          # 本次网关实际扣款
    credit_cents: int                          # 本次先用账户抵扣额度冲抵的金额
    idempotency_key: str                       # 付款幂等键（业务提交唯一）
    charge_attempt_id: str
    created_at: datetime
    status: str = "success"                    # success / failed
    result: ChargeResult | None = None         # 网关判定（失败时）
    note: str = ""


@dataclass
class RefundAllocation:
    """一笔退款在各成功付款尝试上的分摊明细（解释退的是哪一笔钱）。"""

    charge_attempt_id: str
    payment_id: str | None
    amount_cents: int


@dataclass
class Refund:
    """账单退款：全额或部分，退款默认转为该客户的账户抵扣额度。"""

    id: str
    bill_id: str
    subscription_id: str
    customer_id: str
    amount_cents: int                          # 本次退款金额
    idempotency_key: str
    created_at: datetime
    reason: str = ""
    allocations: list[RefundAllocation] = field(default_factory=list)
    ledger_entry_id: str | None = None
    note: str = ""


@dataclass
class CreditApplication:
    """账户抵扣额度冲抵某张账单的独立可对账记录（不可重复抵扣）。"""

    id: str
    bill_id: str
    subscription_id: str
    customer_id: str
    amount_cents: int
    created_at: datetime
    source: str = "auto"                       # auto（出账后自动冲抵）/ manual
    reversed: bool = False                     # 伴随的网关扣款失败而回滚
    ledger_entry_id: str | None = None
    reversal_entry_id: str | None = None
    charge_attempt_id: str | None = None
    note: str = ""


@dataclass
class LedgerEntry:
    """资金流水。

    与扣款尝试一一对应的扣款流水（成功正收入、失败 0）；分次付款、额度抵扣、
    抵扣回滚、退款各自独立成条，保证每一分钱都可对账。
    """

    id: str
    bill_id: str
    subscription_id: str
    entry_type: LedgerEntryType
    amount_cents: int                        # 成功为正收入；失败为 0；退款/抵扣按类型取符号约定
    currency: str
    created_at: datetime
    charge_attempt_id: str | None = None
    payment_id: str | None = None
    refund_id: str | None = None
    credit_application_id: str | None = None
    customer_id: str = ""
    note: str = ""


@dataclass
class PaymentAccount:
    customer_id: str
    card_active: bool = True                  # 卡片是否有效（失效则终态拒付）
    balance_cents: int = 0                    # 模拟余额
    credit_cents: int = 0                     # 退款留存的可抵扣额度（分）
    updated_at: datetime | None = None
