"""账户抵扣额度：退款沉淀的钱在后续账单出账时优先冲抵。

规则：
- 出账（finalize / 调整单生成）时自动应用：抵扣额 = min(账户可用额度, 账单剩余应付)；
- 一张账单只应用一次（credit_app_by_bill 仲裁），绝不重复抵扣；
- 每次应用都有独立的 CreditApplication 记录与 credit_apply 流水，可对账；
- 账户不变量：credit_balance = 累计退款 - 累计抵扣，不会抵成负数；
- 抵扣后账单付满则直接收敛为 PAID，剩余部分仍走原支付方式。
"""

import logging

from app.ledger import record_credit_apply
from app.models import BillStatus, CreditApplication
from app.payments import _settle_if_paid
from app.store import Store

logger = logging.getLogger("billing.credits")


def apply_credit_to_bill(store: Store, bill_id: str) -> CreditApplication | None:
    """出账时用账户抵扣额度冲抵账单；无额度/已应用过返回 None。"""
    with store.lock:
        bill = store.get_bill(bill_id)
        if bill.status != BillStatus.FINALIZED or bill.remaining_cents <= 0:
            return None
        if bill.id in store.credit_app_by_bill:
            logger.info("credit already applied bill=%s, skip", bill.id)
            return None
        acct = store.accounts.get(bill.customer_id)
        if acct is None or acct.credit_balance_cents <= 0:
            return None

        amount = min(acct.credit_balance_cents, bill.remaining_cents)
        app_record = CreditApplication(
            id=store.new_id("crd"),
            bill_id=bill.id,
            subscription_id=bill.subscription_id,
            customer_id=bill.customer_id,
            amount_cents=amount,
            idempotency_key=f"credit:{bill.id}",
            created_at=store.now(),
        )
        store.credit_apps[app_record.id] = app_record
        store.credit_app_by_bill[bill.id] = app_record.id
        acct.credit_balance_cents -= amount
        acct.updated_at = store.now()
        bill.credit_applied_cents += amount
        record_credit_apply(store, app_record)
        logger.info("credit applied bill=%s cents=%d credit_left=%d remaining=%d",
                    bill.id, amount, acct.credit_balance_cents, bill.remaining_cents)
        _settle_if_paid(store, bill)
        return app_record


def credit_balance(store: Store, customer_id: str) -> int:
    with store.lock:
        acct = store.accounts.get(customer_id)
        return acct.credit_balance_cents if acct else 0
