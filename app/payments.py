"""本地模拟支付网关与扣款编排（整笔自动扣款 + 分次付款共用网关）。

- 扣款幂等键 = f"charge:{bill_id}:{attempt_no}"。真实扣款 + 记账 + 账单
  状态迁移在同一把存储锁内完成，因此同一账单的并发扣款最多一次成功，
  重试成功后也不会重复扣款。
- 扣款前若客户账户有退款留存的抵扣额度，先用额度冲抵（独立 CreditApplication
  + credit_applied 流水），不够的部分再走网关；若网关失败，本次额度冲抵
  整体回滚（credit_released 流水），保证不丢额度、账目对得上。
- 失败原因可区分：余额不足(可重试) / 卡片失效(终态) / 网关错误(可重试) /
  无支付方式 / 重试耗尽。
- 重试节奏：base_interval * 2^(n-1) 秒退避。
"""

import logging

from app.clock import as_utc
from app.credits import apply_credit_to_bill, reverse_credit_application
from app.errors import StateError
from app.ledger import record_attempt, refresh_bill_totals
from app.models import (
    Bill,
    BillStatus,
    ChargeAttempt,
    ChargeResult,
    FailureReason,
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
            # 只重置模拟余额与卡片状态；退款留存的抵扣额度独立保留，不被覆盖
            acct.balance_cents = balance_cents
            acct.card_active = card_active
            acct.updated_at = store.now()
        logger.info("ensure_account customer=%s balance=%d credit=%d card_active=%s",
                    customer_id, balance_cents, acct.credit_cents, card_active)
        return acct


def gateway_authorize(
    store: Store, amount_cents: int, customer_id: str
) -> ChargeResult:
    """模拟网关判定（调用方需持有 store.lock）。"""
    if amount_cents <= 0:
        logger.info("gateway: customer=%s amount=0 skip charge", customer_id)
        return ChargeResult.SUCCESS
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


def charge_bill(
    store: Store, bill_id: str, *, _force_attempt_no: int | None = None
) -> ChargeAttempt:
    with store.lock:
        bill = store.get_bill(bill_id)
        if bill.status == BillStatus.PAID:
            succ = next(
                a for a in store.attempts.values()
                if a.bill_id == bill.id and a.result == ChargeResult.SUCCESS
            )
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

        last = _last_attempt(store, bill)
        attempt_no = _force_attempt_no or (last.attempt_no + 1 if last else 1)
        idem = f"charge:{bill.id}:{attempt_no}"

        # 并发/重试幂等：同一尝试键已存在则原样返回，不再真实扣款
        existing_id = store.attempts_by_key.get(idem)
        if existing_id:
            logger.info("charge idempotent hit bill=%s attempt=%d", bill.id, attempt_no)
            return store.attempts[existing_id]

        # 1) 先用账户抵扣额度冲抵未付部分
        due_before = max(0, bill.subtotal_cents - bill.paid_cents)
        credit_app = apply_credit_to_bill(
            store, bill, source="auto", max_cents=due_before
        )
        credit_used = credit_app.amount_cents if credit_app else 0
        cash_needed = due_before - credit_used

        acct = store.accounts.get(bill.customer_id)
        if cash_needed <= 0:
            result = ChargeResult.SUCCESS
            detail = "covered by account credit"
        elif acct is None:
            result = ChargeResult.GATEWAY_ERROR
            detail = "no payment account configured"
        else:
            result = gateway_authorize(store, cash_needed, bill.customer_id)
            detail = "authorized" if result == ChargeResult.SUCCESS else result.value

        attempt = ChargeAttempt(
            id=store.new_id("att"),
            bill_id=bill.id,
            subscription_id=bill.subscription_id,
            attempt_no=attempt_no,
            amount_cents=cash_needed,
            result=result,
            reason_detail=detail,
            idempotency_key=idem,
            attempted_at=store.now(),
            kind="auto",
        )
        store.attempts[attempt.id] = attempt
        store.attempts_by_key[idem] = attempt.id
        bill.charge_attempts = max(bill.charge_attempts, attempt_no)
        if credit_app is not None:
            credit_app.charge_attempt_id = attempt.id

        # 网关失败：回滚本次额度冲抵，失败流水金额 0，保持原有重试语义
        if result != ChargeResult.SUCCESS and credit_app is not None:
            reverse_credit_application(store, credit_app)
            attempt.reversed = True

        entry = record_attempt(store, attempt)
        attempt.ledger_entry_id = entry.id
        # 额度全额覆盖时，attempt 现金为 0；抵扣流水在 apply 时已写入并挂账
        if cash_needed <= 0 and credit_app is not None:
            attempt.ledger_entry_id = entry.id

        refresh_bill_totals(store, bill)

        sub = store.get_subscription(bill.subscription_id)
        if result == ChargeResult.SUCCESS:
            bill.status = BillStatus.PAID
            bill.paid_at = store.now()
            bill.failure_reason = FailureReason.NONE
            if sub.state == SubscriptionState.PAST_DUE:
                sub.state = SubscriptionState.ACTIVE
                sub.grace_until = None
            logger.info(
                "charge SUCCESS bill=%s attempt=%d credit=%d cash=%d -> PAID",
                bill.id, attempt_no, credit_used, cash_needed,
            )
        else:
            if acct is None:
                bill.failure_reason = FailureReason.NO_PAYMENT_METHOD
            else:
                bill.failure_reason = _REASON_MAP.get(result, FailureReason.GATEWAY_ERROR)
            if sub.state == SubscriptionState.ACTIVE:
                sub.state = SubscriptionState.PAST_DUE
            logger.warning(
                "charge FAIL bill=%s attempt=%d reason=%s detail=%s credit_released=%d",
                bill.id, attempt_no, bill.failure_reason.value, detail, credit_used,
            )
        return attempt
