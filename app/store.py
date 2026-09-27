"""线程安全的内存存储：单把可重入锁保护所有可变状态。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date

from .models import Invoice, Subscription, Transaction, UsageRecord


@dataclass
class Store:
    subscriptions: dict[str, Subscription] = field(default_factory=dict)
    invoices: dict[str, Invoice] = field(default_factory=dict)
    # subscription_id -> period_start(iso) -> event_id -> UsageRecord
    usage: dict[str, dict[str, dict[str, UsageRecord]]] = field(default_factory=dict)
    transactions: list[Transaction] = field(default_factory=list)
    # (subscription_id, period_start) -> invoice_id
    invoice_index: dict[tuple[str, date], str] = field(default_factory=dict)
    # 已付清账单的成功流水标记：invoice_id -> transaction_id
    paid_txn_index: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.lock = threading.RLock()

    # -- 账单索引 ----------------------------------------------------------
    def get_invoice(self, sub_id: str, period_start: date) -> Invoice | None:
        inv_id = self.invoice_index.get((sub_id, period_start))
        return self.invoices.get(inv_id) if inv_id else None

    def put_invoice(self, invoice: Invoice) -> None:
        self.invoices[invoice.id] = invoice
        self.invoice_index[(invoice.subscription_id, invoice.period_start)] = invoice.id

    def require_invoice(self, invoice_id: str) -> Invoice:
        inv = self.invoices.get(invoice_id)
        if inv is None:
            from .models import BillingError
            raise BillingError("invoice_not_found", f"账单不存在: {invoice_id}")
        return inv

    def require_subscription(self, sub_id: str) -> Subscription:
        sub = self.subscriptions.get(sub_id)
        if sub is None:
            from .models import BillingError
            raise BillingError("subscription_not_found", f"订阅不存在: {sub_id}")
        return sub


STORE = Store()


def reset_store(store: Store | None = None) -> None:
    """清空全局存储（测试用）。"""
    target = store if store is not None else STORE
    with target.lock:
        target.subscriptions.clear()
        target.invoices.clear()
        target.usage.clear()
        target.transactions.clear()
        target.invoice_index.clear()
        target.paid_txn_index.clear()
