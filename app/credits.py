"""账户抵扣额度：退款留存额度对后续账单的冲抵与回滚。

- 退回来的钱默认留在客户账户上成为可抵扣额度（PaymentAccount.credit_cents）。
- 已出账账单在网关扣款前先冲抵额度，不够的部分再走支付方式。
- 每次冲抵都有独立的 CreditApplication + ``credit_applied`` 流水，可对账、
  不重复；伴随的网关扣款失败时整体回滚（``credit_released`` 流水，额度退回）。
"""

import logging

from app.ledger import (
    bill_credit_net_cents,
    record_credit_applied,
    record_credit_released,
    refresh_bill_totals,
)
from app.models import (
    Bill,
    BillStatus,
    CreditApplication,
)
from app.store import Store

logger = logging.getLogger("billing.credits")


def applied_credit_cents(store: Store, bill_id: str) -> int:
    """账单当前仍有效的额度抵扣合计（不含已回滚）。"""
    return bill_credit_net_cents(store, bill_id)


def apply_credit_to_bill(
    store: Store,
    bill: Bill,
    *,
    source: str = "auto",
    max_cents: int | None = None,
    charge_attempt_id: str | None = None,
) -> CreditApplication | None:
    """用账户可用抵扣额度冲抵账单未付部分（调用方需持有 store.lock）。

    额度不足或账单无未付金额时返回 None；冲抵成功返回 CreditApplication。
    额度扣减、冲抵记录、流水写入、账单回算在同一锁内完成。
    """
    if bill.status != BillStatus.FINALIZED:
        return None
    acct = store.accounts.get(bill.customer_id)
    if acct is None or acct.credit_cents <= 0:
        return None

    due = max(0, bill.subtotal_cents - bill.paid_cents)
    ceiling = due if max_cents is None else min(due, max_cents)
    amount = min(acct.credit_cents, ceiling)
    if amount <= 0:
        return None

    acct.credit_cents -= amount
    application = CreditApplication(
        id=store.new_id("crd"),
        bill_id=bill.id,
        subscription_id=bill.subscription_id,
        customer_id=bill.customer_id,
        amount_cents=amount,
        created_at=store.now(),
        source=source,
        charge_attempt_id=charge_attempt_id,
    )
    store.credit_applications[application.id] = application
    entry = record_credit_applied(store, application)
    application.ledger_entry_id = entry.id
    refresh_bill_totals(store, bill)
    logger.info(
        "credit applied bill=%s customer=%s cents=%d source=%s credit_left=%d",
        bill.id, bill.customer_id, amount, source, acct.credit_cents,
    )
    return application


def reverse_credit_application(
    store: Store, application: CreditApplication
) -> None:
    """回滚一次抵扣：额度退回账户并写反向流水（调用方需持有 store.lock）。"""
    if application.reversed:
        return
    acct = store.accounts.get(application.customer_id)
    if acct is None:
        # 理论上不可能：额度来自该客户退款；防御性处理避免静默丢钱
        raise RuntimeError(
            f"account missing while reversing credit {application.id} "
            f"customer={application.customer_id}"
        )
    acct.credit_cents += application.amount_cents
    application.reversed = True
    entry = record_credit_released(store, application)
    application.reversal_entry_id = entry.id
    bill = store.bills.get(application.bill_id)
    if bill is not None:
        refresh_bill_totals(store, bill)
    logger.info(
        "credit reversed app=%s bill=%s customer=%s cents=%d credit_back=%d",
        application.id, application.bill_id, application.customer_id,
        application.amount_cents, acct.credit_cents,
    )
