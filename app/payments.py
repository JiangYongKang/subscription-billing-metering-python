"""本地模拟支付网关与扣款/分次付款编排。

- 扣款幂等键 = f"charge:{bill_id}:{attempt_no}"；分次付款幂等键由调用方
  提供。真实扣款 + 记账 + 账单状态迁移在同一把存储锁内完成，因此同一
  账单的并发扣款/付款合计不会超过应付，付满后唯一收敛为 PAID。
- 整笔扣款按"剩余应付"（应付 - 已实收 - 已抵扣）收取：先分次付款再
  触发自动扣款不会重复收钱。
- 失败原因可区分：余额不足(可重试) / 卡片失效(终态) / 网关错误(可重试) /
  无支付方式 / 重试耗尽。
- 重试节奏：base_interval * 2^(attempt_no-1) 秒退避。
"""

import logging
from dataclasses import dataclass

from app.clock import as_utc
from app.errors import OverpaymentError, PaymentDeclinedError, StateError
from app.ledger import record_attempt, record_payment
from app.models import (
    Bill,
    BillStatus,
    ChargeAttempt,
    ChargeResult,
    FailureReason,
    Payment,
    PaymentAccount,
    SubscriptionState,
)
from app.store import Store

logger = logging.getLogger("billing.payments")

RETRYABLE = {ChargeResult.INSUFFICIENT_FUNDS, ChargeResult.GATEWAY_ERROR}
TERMINAL = {ChargeResult.CARD_DECLINED}
_REASON_MAP = {
    ChargeResult.INSUFFICIENT_FUNDS: FailureReason.INSUFFICIENT_FUNDS,
    ChargeResult.CARD_DECLINED: FailureReason.CARD_DECLINED,
    ChargeResult.GATEWAY_ERROR: FailureReason.GATEWAY_ERROR,
}


def ensure_account(
    store: Store,
    customer_id: str,
    balance_cents: int,
    card_active: bool = True,
) -> PaymentAccount:
    with store.lock:
        acct = store.accounts.get(customer_id)
        if acct is None:
            acct = PaymentAccount(
                customer_id=customer_id,
                balance_cents=balance_cents,
                card_active=card_active,
                updated_at=store.now(),
            )
            store.accounts[customer_id] = acct
        else:
            acct.balance_cents = balance_cents
            acct.card_active = card_active
            acct.updated_at = store.now()
        logger.info("ensure_account customer=%s balance=%d card_active=%s",
                    customer_id, balance_cents, card_active)
        return acct


def gateway_authorize(
    store: Store, amount_cents: int, customer_id: str
) -> ChargeResult:
    """模拟网关判定（调用方需持有 store.lock）。"""
    acct = store.accounts.get(customer_id)
    if acct is None:
        return ChargeResult.GATEWAY_ERROR
    if not acct.card_active:
        logger.warning("gateway: customer=%s card declined (终态)", customer_id)
        return ChargeResult.CARD_DECLINED
    if acct.balance_cents < amount_cents:
        logger.warning("gateway: customer=%s balance=%d < amount=%d (可重试)",
                       customer_id, acct.balance_cents, amount_cents)
        return ChargeResult.INSUFFICIENT_FUNDS
    acct.balance_cents -= amount_cents
    acct.updated_at = store.now()
    logger.info("gateway: customer=%s charged=%d remaining=%d",
                customer_id, amount_cents, acct.balance_cents)
    return ChargeResult.SUCCESS


def next_retry_at(attempt_no: int, base_interval_sec: int, failed_at) -> int:
    import datetime as _dt

    delay = int(base_interval_sec * (2 ** max(0, attempt_no - 1)))
    when = as_utc(failed_at) + _dt.timedelta(seconds=delay)
    return int(when.timestamp())


def _last_attempt(store: Store, bill: Bill) -> ChargeAttempt | None:
    mine = [a for a in store.attempts.values() if a.bill_id == bill.id]
    if not mine:
        return None
    return max(mine, key=lambda a: a.attempt_no)


def _register_payment(
    store: Store,
    bill: Bill,
    amount_cents: int,
    method: str,
    idempotency_key: str,
    charge_attempt_id: str | None = None,
) -> Payment:
    """登记一笔实收并写流水（调用方需持有 store.lock）。"""
    payment = Payment(
        id=store.new_id("pay"),
        bill_id=bill.id,
        subscription_id=bill.subscription_id,
        customer_id=bill.customer_id,
        amount_cents=amount_cents,
        method=method,
        idempotency_key=idempotency_key,
        created_at=store.now(),
        charge_attempt_id=charge_attempt_id,
    )
    store.payments[payment.id] = payment
    store.payments_by_key[idempotency_key] = payment.id
    bill.paid_cents += amount_cents
    # 自动扣款成功的资金已由 charge_success 流水记录，这里只为手动付款补流水；
    # Payment 行本身作为后续退款的锚点，两种途径都需要。
    if method == "manual":
        record_payment(store, payment)
    return payment


def _settle_if_paid(store: Store, bill: Bill) -> None:
    """付满后唯一收敛为 PAID，并把订阅从宽限中恢复（调用方需持锁）。"""
    if bill.remaining_cents > 0 or bill.status != BillStatus.FINALIZED:
        return
    bill.status = BillStatus.PAID
    bill.paid_at = store.now()
    bill.failure_reason = FailureReason.NONE
    sub = store.get_subscription(bill.subscription_id)
    if sub.state == SubscriptionState.PAST_DUE:
        sub.state = SubscriptionState.ACTIVE
        sub.grace_until = None
    logger.info("bill=%s fully settled -> PAID (paid=%d credit=%d)",
                bill.id, bill.paid_cents, bill.credit_applied_cents)


def charge_bill(
    store: Store, bill_id: str, *, _force_attempt_no: int | None = None
) -> ChargeAttempt:
    with store.lock:
        bill = store.get_bill(bill_id)
        if bill.status == BillStatus.PAID:
            succ = next(
                (a for a in store.attempts.values()
                 if a.bill_id == bill.id and a.result == ChargeResult.SUCCESS),
                None,
            )
            if succ is None:
                # 通过分次付款/抵扣结清的账单没有扣款尝试可回放
                raise StateError(f"bill {bill.id} already settled; nothing to charge")
            caller_key = f"charge:{bill.id}:{_force_attempt_no or succ.attempt_no}"
            if not succ.dedup_idempotency_key and caller_key != succ.idempotency_key:
                succ.dedup_idempotency_key = caller_key
            logger.info(
                "charge replay bill=%s already paid attempt=%d caller_key=%s",
                bill.id, succ.attempt_no, caller_key,
            )
            return succ
        if bill.status == BillStatus.FAILED:
            raise StateError(f"bill {bill.id} failed permanently; retry exhausted")
        if bill.status != BillStatus.FINALIZED:
            raise StateError(f"bill {bill.id} is {bill.status.value}; finalize before charge")

        acct = store.accounts.get(bill.customer_id)
        last = _last_attempt(store, bill)
        attempt_no = _force_attempt_no or (last.attempt_no + 1 if last else 1)
        idem = f"charge:{bill.id}:{attempt_no}"

        # 并发/重试幂等：同一尝试键已存在则原样返回，不再真实扣款
        existing_id = store.attempts_by_key.get(idem)
        if existing_id:
            logger.info("charge idempotent hit bill=%s attempt=%d", bill.id, attempt_no)
            return store.attempts[existing_id]

        amount_due = bill.remaining_cents
        if acct is None:
            result = ChargeResult.GATEWAY_ERROR
            detail = "no payment account configured"
        else:
            result = gateway_authorize(store, amount_due, bill.customer_id)
            detail = "authorized" if result == ChargeResult.SUCCESS else result.value

        attempt = ChargeAttempt(
            id=store.new_id("att"),
            bill_id=bill.id,
            subscription_id=bill.subscription_id,
            attempt_no=attempt_no,
            amount_cents=amount_due,
            result=result,
            reason_detail=detail,
            idempotency_key=idem,
            attempted_at=store.now(),
        )
        store.attempts[attempt.id] = attempt
        store.attempts_by_key[idem] = attempt.id
        bill.charge_attempts = max(bill.charge_attempts, attempt_no)

        entry = record_attempt(store, attempt)
        attempt.ledger_entry_id = entry.id

        sub = store.get_subscription(bill.subscription_id)
        if result == ChargeResult.SUCCESS:
            _register_payment(
                store, bill, amount_due, method="charge",
                idempotency_key=f"payment:{attempt.id}",
                charge_attempt_id=attempt.id,
            )
            _settle_if_paid(store, bill)
            logger.info("charge SUCCESS bill=%s attempt=%d cents=%d -> %s",
                        bill.id, attempt_no, amount_due, bill.status.value)
        else:
            if acct is None:
                bill.failure_reason = FailureReason.NO_PAYMENT_METHOD
            else:
                bill.failure_reason = _REASON_MAP.get(result, FailureReason.GATEWAY_ERROR)
            if sub.state == SubscriptionState.ACTIVE:
                sub.state = SubscriptionState.PAST_DUE
            logger.warning(
                "charge FAIL bill=%s attempt=%d reason=%s detail=%s",
                bill.id, attempt_no, bill.failure_reason.value, detail,
            )
        return attempt


@dataclass
class PaymentOutcome:
    """一次分次付款调用的结果。"""

    payment: Payment | None          # 已结清/幂等命中时为对应付款或 None
    bill_status: BillStatus
    paid_cents: int                  # 账单累计实收
    remaining_cents: int             # 还差多少付清
    duplicate: bool = False          # 幂等键命中，未重复扣钱
    already_settled: bool = False    # 账单已结清，本次未再收钱


def pay_bill(
    store: Store,
    bill_id: str,
    amount_cents: int,
    idempotency_key: str,
) -> PaymentOutcome:
    """分次付款：一张已出账账单可一笔一笔付，直到付清。

    - 幂等：同一 idempotency_key 重复提交返回首次结果，不重复扣钱；
    - 并发：判定与扣款在同一锁内，合计实收不超过应付；
    - 付满后状态唯一收敛为 PAID，后来的请求按已结清处理，不再收钱。
    """
    if amount_cents <= 0:
        raise StateError("payment amount must be positive")
    with store.lock:
        bill = store.get_bill(bill_id)

        # 幂等重放：同一付款键直接返回首次结果
        existing_id = store.payments_by_key.get(idempotency_key)
        if existing_id is not None:
            payment = store.payments[existing_id]
            logger.info("payment idempotent hit bill=%s key=%s payment=%s",
                        bill.id, idempotency_key, payment.id)
            return PaymentOutcome(
                payment=payment, bill_status=bill.status,
                paid_cents=bill.paid_cents,
                remaining_cents=bill.remaining_cents, duplicate=True,
            )

        # 已结清：后来的请求按已结清处理，不再收钱
        if bill.status == BillStatus.PAID:
            logger.info("payment on settled bill=%s ignored cents=%d",
                        bill.id, amount_cents)
            return PaymentOutcome(
                payment=None, bill_status=bill.status,
                paid_cents=bill.paid_cents, remaining_cents=0,
                already_settled=True,
            )
        if bill.status != BillStatus.FINALIZED:
            raise StateError(
                f"bill {bill.id} is {bill.status.value}; only finalized bills accept payments"
            )

        # 超付拦截：合计实收不得超过应付
        if amount_cents > bill.remaining_cents:
            logger.warning("overpayment rejected bill=%s amount=%d remaining=%d",
                           bill.id, amount_cents, bill.remaining_cents)
            raise OverpaymentError(
                f"amount {amount_cents} exceeds remaining {bill.remaining_cents} "
                f"for bill {bill.id}"
            )

        acct = store.accounts.get(bill.customer_id)
        if acct is None:
            raise PaymentDeclinedError("no payment account configured")
        result = gateway_authorize(store, amount_cents, bill.customer_id)
        if result != ChargeResult.SUCCESS:
            logger.warning("payment declined bill=%s cents=%d reason=%s",
                           bill.id, amount_cents, result.value)
            raise PaymentDeclinedError(
                f"payment declined: {result.value}"
            )

        payment = _register_payment(
            store, bill, amount_cents, method="manual",
            idempotency_key=idempotency_key,
        )
        _settle_if_paid(store, bill)
        logger.info("payment accepted bill=%s payment=%s cents=%d remaining=%d status=%s",
                    bill.id, payment.id, amount_cents, bill.remaining_cents,
                    bill.status.value)
        return PaymentOutcome(
            payment=payment, bill_status=bill.status,
            paid_cents=bill.paid_cents, remaining_cents=bill.remaining_cents,
        )
