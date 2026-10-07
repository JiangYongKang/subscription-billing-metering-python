"""账单分次付款。

- 已出账（FINALIZED）账单可以一笔一笔付，直到付清。
- 每笔付款用业务侧 ``idempotency_key`` 防重：重复提交同一笔返回首次结果，
  绝不重复扣钱；同键用于不同账单按冲突拒绝。
- 同一张账单并发多笔付款在同一把存储锁内仲裁：每笔金额按"未付差额"截断，
  合计实收不超过应付；付满后状态唯一收敛为 PAID，后来的请求按已结清处理
  （status=already_settled，不再扣款）。
- 付款时若账户有退款留存的抵扣额度，先冲抵额度（记入 Payment.credit_cents），
  不够的部分再走网关（记入 Payment.amount_cents）。
"""

import logging

from app.credits import apply_credit_to_bill, reverse_credit_application
from app.errors import ConflictError, StateError
from app.ledger import (
    record_attempt,
    record_payment,
    refresh_bill_totals,
)
from app.models import (
    Bill,
    BillStatus,
    ChargeAttempt,
    ChargeResult,
    FailureReason,
    Payment,
    SubscriptionState,
)
from app.payments import _last_attempt, gateway_authorize
from app.store import Store

logger = logging.getLogger("billing.installments")


def amount_due(store: Store, bill: Bill) -> int:
    """账单当前还差多少钱（应付 - 已生效实收，不小于 0）。"""
    return max(0, bill.subtotal_cents - bill.paid_cents)


def _key(idempotency_key: str) -> str:
    return f"pay:{idempotency_key}"


def _settled_noop(store: Store, bill: Bill, idem_full: str, raw_key: str) -> Payment:
    existing_id = store.payments_by_key.get(idem_full)
    if existing_id:
        return store.payments[existing_id]
    payment = Payment(
        id=store.new_id("pay"),
        bill_id=bill.id,
        subscription_id=bill.subscription_id,
        customer_id=bill.customer_id,
        amount_cents=0,
        credit_cents=0,
        idempotency_key=raw_key,
        charge_attempt_id="",
        created_at=store.now(),
        status="already_settled",
        note="账单已结清，重复/并发付款按已结清处理，不再扣款",
    )
    store.payments[payment.id] = payment
    store.payments_by_key[idem_full] = payment.id
    logger.info("pay bill=%s already settled -> noop key=%s", bill.id, raw_key)
    return payment


def pay_bill(
    store: Store,
    bill_id: str,
    amount_cents: int,
    idempotency_key: str,
) -> Payment:
    if amount_cents <= 0:
        raise ConflictError("payment amount_cents must be positive")
    if not idempotency_key:
        raise ConflictError("idempotency_key is required for payments")

    with store.lock:
        bill = store.get_bill(bill_id)
        idem_full = _key(idempotency_key)

        # 1) 已付清：按已结清处理，不扣款
        if bill.status == BillStatus.PAID:
            return _settled_noop(store, bill, idem_full, idempotency_key)
        if bill.status != BillStatus.FINALIZED:
            raise StateError(
                f"bill {bill.id} is {bill.status.value}; finalize before payment"
            )

        # 2) 幂等防重：同一付款键直接回放首次结果
        replay_id = store.payments_by_key.get(idem_full)
        if replay_id:
            replay = store.payments[replay_id]
            if replay.bill_id != bill.id:
                raise ConflictError(
                    f"idempotency_key {idempotency_key} already used on another bill"
                )
            logger.info("pay replay bill=%s key=%s status=%s",
                        bill.id, idempotency_key, replay.status)
            return replay

        # 3) 计算本次目标：按未付差额截断，保证并发合计不超收
        due = amount_due(store, bill)
        if due <= 0:
            return _settled_noop(store, bill, idem_full, idempotency_key)
        target = min(amount_cents, due)

        # 4) 先冲抵账户抵扣额度，再走网关
        credit_app = apply_credit_to_bill(
            store, bill, source="manual", max_cents=target
        )
        credit_used = credit_app.amount_cents if credit_app else 0
        cash_needed = target - credit_used

        last = _last_attempt(store, bill)
        attempt_no = (last.attempt_no + 1) if last else 1
        attempt = ChargeAttempt(
            id=store.new_id("att"),
            bill_id=bill.id,
            subscription_id=bill.subscription_id,
            attempt_no=attempt_no,
            amount_cents=cash_needed,
            result=ChargeResult.SUCCESS,
            reason_detail="partial payment",
            idempotency_key=f"charge:{bill.id}:{attempt_no}",
            attempted_at=store.now(),
            kind="partial",
        )

        payment = Payment(
            id=store.new_id("pay"),
            bill_id=bill.id,
            subscription_id=bill.subscription_id,
            customer_id=bill.customer_id,
            amount_cents=cash_needed,
            credit_cents=credit_used,
            idempotency_key=idempotency_key,
            charge_attempt_id=attempt.id,
            created_at=store.now(),
        )
        attempt.payment_id = payment.id
        if credit_app is not None:
            credit_app.charge_attempt_id = attempt.id

        if cash_needed <= 0:
            result = ChargeResult.SUCCESS
        else:
            result = gateway_authorize(store, cash_needed, bill.customer_id)
        attempt.result = result
        attempt.reason_detail = (
            "partial payment authorized" if result == ChargeResult.SUCCESS else result.value
        )

        store.attempts[attempt.id] = attempt
        store.attempts_by_key[attempt.idempotency_key] = attempt.id
        bill.charge_attempts = max(bill.charge_attempts, attempt_no)
        store.payments[payment.id] = payment
        store.payments_by_key[idem_full] = payment.id

        if result == ChargeResult.SUCCESS:
            entry = record_payment(store, payment, attempt)
            attempt.ledger_entry_id = entry.id
            payment.status = "success"
            refresh_bill_totals(store, bill)
            if bill.paid_cents >= bill.subtotal_cents:
                bill.status = BillStatus.PAID
                bill.paid_at = store.now()
                bill.failure_reason = FailureReason.NONE
                sub = store.get_subscription(bill.subscription_id)
                if sub.state == SubscriptionState.PAST_DUE:
                    sub.state = SubscriptionState.ACTIVE
                    sub.grace_until = None
            logger.info(
                "pay SUCCESS bill=%s key=%s no=%d credit=%d cash=%d paid=%d/%d status=%s",
                bill.id, idempotency_key, attempt_no, credit_used, cash_needed,
                bill.paid_cents, bill.subtotal_cents, bill.status.value,
            )
        else:
            # 网关失败：回滚本次额度冲抵，付款记失败，不产生成功流水
            if credit_app is not None:
                reverse_credit_application(store, credit_app)
                attempt.reversed = True
            fail_entry = record_attempt(store, attempt)
            attempt.ledger_entry_id = fail_entry.id
            payment.status = "failed"
            payment.result = result
            payment.amount_cents = 0
            payment.credit_cents = 0
            refresh_bill_totals(store, bill)
            logger.warning(
                "pay FAIL bill=%s key=%s no=%d reason=%s credit_released=%d",
                bill.id, idempotency_key, attempt_no, result.value, credit_used,
            )
        return payment
