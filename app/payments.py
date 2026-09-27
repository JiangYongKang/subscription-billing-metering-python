"""本地模拟支付网关、扣款执行与重试策略。

并发/幂等约定：
- 每次扣款都带 idempotency_key（默认按 账单+尝试序号 派生）；
  同 key 的并发/重复请求只产生一条流水。
- 对同一账单的并发扣款由 store 锁串行化；账单一旦 PAID 直接幂等返回
  原成功流水，因此最多成功一次，绝不重复扣款。
- 失败原因区分：余额不足(可重试)、卡片失效(永久)、重试耗尽、未知。
"""
from __future__ import annotations

import itertools
import logging
from datetime import date

from .config import RetryConfig
from .models import (BillingError, FailureReason, Invoice, InvoiceStatus,
                     RETRYABLE_REASONS, Subscription, Transaction, TxnStatus)
from .store import Store

logger = logging.getLogger("billing.payments")

_txn_seq = itertools.count(1)


class MockGateway:
    """纯本地、确定性的模拟网关，无任何外部调用。

    场景由订阅的 payment_mode 控制：
    - {"scenario": "ok"}                                恒成功
    - {"scenario": "insufficient_funds", "fail_times": N}
          前 N 次尝试返回余额不足，之后成功
    - {"scenario": "card_declined"}                     恒为卡片失效
    """

    def charge(self, *, subscription: Subscription, amount_cents: int,
               attempt: int) -> tuple[TxnStatus, FailureReason]:
        mode = subscription.payment_mode or {}
        scenario = mode.get("scenario", "ok")
        if scenario == "ok":
            logger.info("gateway charge sub=%s attempt=%s amount=%s -> success",
                        subscription.id, attempt, amount_cents)
            return TxnStatus.SUCCESS, FailureReason.NONE
        if scenario == "insufficient_funds":
            fail_times = int(mode.get("fail_times", 1))
            if attempt <= fail_times:
                logger.info(
                    "gateway charge sub=%s attempt=%s amount=%s -> "
                    "insufficient_funds (将在 %s 次后恢复)",
                    subscription.id, attempt, amount_cents, fail_times)
                return TxnStatus.FAILED, FailureReason.INSUFFICIENT_FUNDS
            logger.info(
                "gateway charge sub=%s attempt=%s amount=%s -> success(充值后)",
                subscription.id, attempt, amount_cents)
            return TxnStatus.SUCCESS, FailureReason.NONE
        if scenario == "card_declined":
            logger.info("gateway charge sub=%s attempt=%s amount=%s -> "
                        "card_declined(永久)",
                        subscription.id, attempt, amount_cents)
            return TxnStatus.FAILED, FailureReason.CARD_DECLINED
        logger.warning("gateway charge sub=%s attempt=%s 未知场景 %s",
                       subscription.id, attempt, scenario)
        return TxnStatus.FAILED, FailureReason.UNKNOWN


def _txn_by_idem(store: Store, key: str) -> Transaction | None:
    for txn in reversed(store.transactions):
        if txn.idempotency_key == key:
            return txn
    return None


def can_attempt_now(invoice: Invoice, as_of: date, config: RetryConfig) -> bool:
    """是否已到计划扣款时间（未到 next_attempt_on 前不自动重试）。"""
    if invoice.next_attempt_on is None:
        return False
    return as_of >= invoice.next_attempt_on


def mark_retry_exhausted(invoice: Invoice) -> None:
    """达到最大尝试次数仍失败：账单 FAILED，原因记为重试耗尽。"""
    invoice.status = InvoiceStatus.FAILED
    invoice.finalized = True
    invoice.last_failure_reason = FailureReason.RETRY_EXHAUSTED
    invoice.next_attempt_on = None


def charge_invoice(store: Store, invoice: Invoice, *, as_of: date,
                   config: RetryConfig, gateway: MockGateway,
                   idempotency_key: str | None = None,
                   respect_schedule: bool = False) -> Transaction:
    """对账单执行一次扣款。

    respect_schedule=True 时（周期任务），未到计划重试日则跳过；
    手动扣款接口传 False 可立即尝试（仍受最大次数限制）。
    """
    with store.lock:
        sub = store.require_subscription(invoice.subscription_id)

        # 1) 已付清：幂等返回原成功流水，任何并发/重复调用都不会再扣。
        if invoice.status == InvoiceStatus.PAID:
            paid_id = store.paid_txn_index.get(invoice.id)
            paid = next((t for t in store.transactions if t.id == paid_id), None)
            logger.info("charge-skip invoice=%s 已付清，幂等返回流水 %s",
                        invoice.id, paid_id)
            if paid is None:
                raise BillingError("reconcile_error",
                                   f"账单 {invoice.id} 已付清但缺少成功流水")
            return paid

        if invoice.status == InvoiceStatus.FAILED:
            raise BillingError("invoice_failed",
                               f"账单 {invoice.id} 已重试耗尽，不再扣款")
        if invoice.status != InvoiceStatus.OPEN:
            raise BillingError("invoice_not_open",
                               f"账单 {invoice.id} 状态 {invoice.status.value} 不可扣款")

        if respect_schedule and not can_attempt_now(invoice, as_of, config):
            raise BillingError(
                "retry_not_due",
                f"账单 {invoice.id} 下次计划扣款日为 {invoice.next_attempt_on}",
            )

        attempt = invoice.attempts + 1
        if attempt > config.max_attempts:
            mark_retry_exhausted(invoice)
            raise BillingError("retry_exhausted",
                               f"账单 {invoice.id} 已达最大尝试次数 {config.max_attempts}")

        key = idempotency_key or f"inv:{invoice.id}:attempt:{attempt}"
        dup = _txn_by_idem(store, key)
        if dup is not None:
            logger.info("charge-idempotent invoice=%s key=%s 返回既有流水 %s",
                        invoice.id, key, dup.id)
            return dup

        # 2) 调用网关（在锁内，保证同账单并发串行）。
        status, reason = gateway.charge(
            subscription=sub, amount_cents=invoice.total_cents, attempt=attempt)
        txn = Transaction(
            id=f"txn-{next(_txn_seq)}",
            invoice_id=invoice.id,
            amount_cents=invoice.total_cents,
            status=status,
            attempt=attempt,
            reason=reason,
            created_on=as_of,
            idempotency_key=key,
        )
        store.transactions.append(txn)
        invoice.attempts = attempt

        if status == TxnStatus.SUCCESS:
            invoice.status = InvoiceStatus.PAID
            invoice.paid_on = as_of
            invoice.last_failure_reason = FailureReason.NONE
            invoice.next_attempt_on = None
            store.paid_txn_index[invoice.id] = txn.id
            logger.info("charge-success invoice=%s attempt=%s amount=%s txn=%s",
                        invoice.id, attempt, invoice.total_cents, txn.id)
            return txn

        # 3) 失败：记账并决定后续。
        invoice.last_failure_reason = reason
        if reason not in RETRYABLE_REASONS:
            # 卡片失效等永久失败：立即终止，保留原始原因。
            invoice.status = InvoiceStatus.FAILED
            invoice.finalized = True
            invoice.next_attempt_on = None
            logger.warning("charge-failed-permanent invoice=%s reason=%s",
                           invoice.id, reason.value)
            return txn

        next_attempt_no = attempt + 1
        scheduled = config.scheduled_attempt_on(
            invoice.issued_on or as_of, next_attempt_no)
        if next_attempt_no > config.max_attempts or scheduled is None:
            mark_retry_exhausted(invoice)
            logger.warning(
                "charge-failed invoice=%s attempt=%s/%s reason=%s -> 重试耗尽",
                invoice.id, attempt, config.max_attempts, reason.value)
        else:
            invoice.next_attempt_on = scheduled
            logger.warning(
                "charge-failed invoice=%s attempt=%s/%s reason=%s -> 计划 %s 重试",
                invoice.id, attempt, config.max_attempts,
                reason.value, scheduled)
        return txn
