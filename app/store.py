"""内存存储 + 全局锁。

单进程本地服务：所有跨实体的写操作都在 ``with store.lock`` 内完成，
保证"检查-判定-写入"的原子性，这是并发扣款最多成功一次的根基。

历史价格以 ``(code, version)`` 快照保存：调价只会新增版本，绝不覆盖
已出账账单引用的旧版本，因此历史账目不可被改写。
"""

import threading
import uuid
from datetime import datetime

from app.clock import Clock
from app.errors import NotFoundError
from app.models import (
    Bill,
    ChargeAttempt,
    CreditApplication,
    LedgerEntry,
    Payment,
    PaymentAccount,
    Plan,
    PlanChange,
    Refund,
    Subscription,
    UsageEvent,
)


class Store:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.lock = threading.RLock()
        # code -> 当前版本 Plan；(code, version) -> 历史快照
        self.plans: dict[str, Plan] = {}
        self.plan_versions: dict[tuple[str, int], Plan] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.changes: list[PlanChange] = []
        # event_id -> 事件；idempotency_key -> event_id
        self.events: dict[str, UsageEvent] = {}
        self.events_by_key: dict[str, str] = {}
        # bill_id -> Bill；(subscription_id, period_index) -> 主账单 id
        self.bills: dict[str, Bill] = {}
        self.bill_by_period: dict[tuple[str, int], str] = {}
        # attempt_id / 扣款幂等键
        self.attempts: dict[str, ChargeAttempt] = {}
        self.attempts_by_key: dict[str, str] = {}
        # 订阅变更等通用幂等键 -> 对象
        self.idempotents: dict[str, object] = {}
        self.ledger: list[LedgerEntry] = []
        self.accounts: dict[str, PaymentAccount] = {}
        # 分次付款 / 退款 / 抵扣
        self.payments: dict[str, Payment] = {}
        self.payments_by_key: dict[str, str] = {}      # 付款幂等键 -> payment_id
        self.refunds: dict[str, Refund] = {}
        self.refunds_by_key: dict[str, str] = {}       # 退款幂等键 -> refund_id
        self.credit_apps: dict[str, CreditApplication] = {}
        self.credit_app_by_bill: dict[str, str] = {}   # bill_id -> credit_app_id（每单限一次）
        self._counters: dict[str, int] = {}

    def now(self) -> datetime:
        return self.clock.now()

    def new_id(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{prefix}_{n:04d}_{uuid.uuid4().hex[:8]}"

    # ---- plans -------------------------------------------------------
    def get_plan(self, code: str, version: int | None = None) -> Plan:
        with self.lock:
            if version is None:
                plan = self.plans.get(code)
            else:
                plan = self.plan_versions.get((code, version))
            if plan is None:
                raise NotFoundError(f"plan not found: {code} v{version}")
            return plan

    def put_plan(self, plan: Plan) -> None:
        with self.lock:
            self.plan_versions[(plan.code, plan.version)] = plan
            cur = self.plans.get(plan.code)
            if cur is None or plan.version >= cur.version:
                self.plans[plan.code] = plan

    # ---- subscriptions & bills --------------------------------------
    def get_subscription(self, subscription_id: str) -> Subscription:
        with self.lock:
            sub = self.subscriptions.get(subscription_id)
            if sub is None:
                raise NotFoundError(f"subscription not found: {subscription_id}")
            return sub

    def get_bill(self, bill_id: str) -> Bill:
        with self.lock:
            bill = self.bills.get(bill_id)
            if bill is None:
                raise NotFoundError(f"bill not found: {bill_id}")
            return bill

    def period_bill(self, subscription_id: str, period_index: int) -> Bill | None:
        with self.lock:
            bid = self.bill_by_period.get((subscription_id, period_index))
            return self.bills[bid] if bid else None

    def main_bills(self, subscription_id: str) -> list[Bill]:
        with self.lock:
            return [
                self.bills[bid]
                for (sid, _idx), bid in sorted(
                    self.bill_by_period.items(), key=lambda kv: kv[0][1]
                )
                if sid == subscription_id
            ]

    def account(self, customer_id: str) -> PaymentAccount | None:
        with self.lock:
            return self.accounts.get(customer_id)
