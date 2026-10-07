"""退款：把已实收的钱整笔或部分退回账户抵扣额度。

规则：
- 退款以 Payment 为锚点，累计退款不得超过该笔实收（refundable_cents）；
- 退款不改写已出账账单：账单金额、明细、状态保持原样，资金变化只体现
  在流水（refund 负金额）与账户抵扣额度上；
- 幂等：同一 idempotency_key 重复提交返回首次退款，不重复退钱；
- 退回的钱默认沉淀为账户抵扣额度（credit_balance_cents），后续账单
  出账时优先冲抵。
"""

import logging

from app.errors import NotFoundError, RefundExceededError, StateError
from app.ledger import record_refund
from app.models import Refund
from app.store import Store

logger = logging.getLogger("billing.refunds")


def refund_payment(
    store: Store,
    payment_id: str,
    amount_cents: int | None = None,
    reason: str = "",
    idempotency_key: str = "",
) -> tuple[Refund, bool]:
    """对一笔付款退款；返回 (refund, created)。重复提交返回 (首次退款, False)。"""
    with store.lock:
        payment = store.payments.get(payment_id)
        if payment is None:
            raise NotFoundError(f"payment not found: {payment_id}")

        if idempotency_key:
            existing_id = store.refunds_by_key.get(idempotency_key)
            if existing_id is not None:
                logger.info("refund idempotent hit key=%s refund=%s",
                            idempotency_key, existing_id)
                return store.refunds[existing_id], False

        amount = payment.refundable_cents if amount_cents is None else amount_cents
        if amount <= 0:
            raise StateError("refund amount must be positive")
        if amount > payment.refundable_cents:
            logger.warning("refund exceeded payment=%s amount=%d refundable=%d",
                           payment_id, amount, payment.refundable_cents)
            raise RefundExceededError(
                f"refund {amount} exceeds refundable {payment.refundable_cents} "
                f"of payment {payment_id}"
            )

        refund = Refund(
            id=store.new_id("ref"),
            payment_id=payment.id,
            bill_id=payment.bill_id,
            subscription_id=payment.subscription_id,
            customer_id=payment.customer_id,
            amount_cents=amount,
            reason=reason,
            idempotency_key=idempotency_key or f"refund:{payment.id}:{store.now().timestamp()}",
            created_at=store.now(),
        )
        store.refunds[refund.id] = refund
        store.refunds_by_key[refund.idempotency_key] = refund.id

        payment.refunded_cents += amount
        # 退回的钱默认留作账户抵扣额度
        acct = store.accounts.get(payment.customer_id)
        if acct is not None:
            acct.credit_balance_cents += amount
            acct.updated_at = store.now()
        record_refund(store, refund)
        logger.info(
            "refund created id=%s payment=%s cents=%d reason=%r credit_balance=%d",
            refund.id, payment.id, amount, reason,
            acct.credit_balance_cents if acct else 0,
        )
        return refund, True


def refunds_for_bill(store: Store, bill_id: str) -> list[Refund]:
    with store.lock:
        return [r for r in store.refunds.values() if r.bill_id == bill_id]
