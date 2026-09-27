"""失败重试调度、重试耗尽与宽限期收敛。

- 退避：第 n 次失败后，下次重试在 base_interval * 2^(n-1) 秒后。
- 可重试原因（余额不足/网关错误）用尽次数 -> 账单 failed(RETRY_EXHAUSTED)。
- 卡片失效为终态：不再重试，账单 failed(CARD_DECLINED)。
- 宽限期自首次失败起算；期内补齐则 active，超时未付清则订阅 expired。
"""

import logging
from datetime import timedelta

from app.clock import as_utc
from app.models import (
    Bill,
    BillStatus,
    ChargeResult,
    FailureReason,
    SubscriptionState,
)
from app.payments import TERMINAL, charge_bill, next_retry_at
from app.store import Store

logger = logging.getLogger("billing.grace")

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_INTERVAL_SEC = 3600          # 首次失败 1h 后重试
DEFAULT_GRACE_SECONDS = 3 * 24 * 3600     # 3 天宽限


def _attempts(store: Store, bill: Bill):
    return sorted(
        (a for a in store.attempts.values() if a.bill_id == bill.id),
        key=lambda a: a.attempt_no,
    )


def _first_failed_at(store: Store, bill: Bill):
    for a in _attempts(store, bill):
        if a.result != ChargeResult.SUCCESS:
            return a.attempted_at
    return None


def _ensure_grace_until(store: Store, bill: Bill, grace_seconds: int):
    sub = store.get_subscription(bill.subscription_id)
    first_fail = _first_failed_at(store, bill)
    if first_fail is None:
        return sub
    if sub.grace_until is None:
        sub.grace_until = as_utc(first_fail) + timedelta(seconds=grace_seconds)
        logger.info("grace starts bill=%s sub=%s until=%s",
                    bill.id, sub.id, sub.grace_until.isoformat())
    return sub


def run_retries(
    store: Store,
    bill_id: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_interval_sec: int = DEFAULT_BASE_INTERVAL_SEC,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
) -> Bill:
    """处理"当前时刻"所有到期的重试，直到：成功 / 终态 / 耗尽 / 未到点。"""
    with store.lock:
        bill = store.get_bill(bill_id)
        if bill.status == BillStatus.OPEN:
            raise RuntimeError("finalize the bill before running retries")

        while bill.status == BillStatus.FINALIZED:
            attempts = _attempts(store, bill)
            last = attempts[-1] if attempts else None

            if last is not None and last.result in TERMINAL:
                bill.status = BillStatus.FAILED
                bill.failure_reason = FailureReason.CARD_DECLINED
                logger.warning("run_retries bill=%s terminal: card_declined", bill.id)
                break

            if last is not None and last.attempt_no >= max_attempts:
                bill.status = BillStatus.FAILED
                bill.failure_reason = FailureReason.RETRY_EXHAUSTED
                logger.warning("run_retries bill=%s retry exhausted after %d",
                               bill.id, last.attempt_no)
                break

            if last is not None:
                due = next_retry_at(last.attempt_no, base_interval_sec,
                                    last.attempted_at)
                if store.now().timestamp() < due:
                    logger.info("run_retries bill=%s wait until ts=%d", bill.id, due)
                    break

            # 无尝试（立即首扣）或已到退避点 -> 发起下一次扣款
            attempt = charge_bill(store, bill.id)
            bill = store.get_bill(bill.id)
            if attempt.result == ChargeResult.SUCCESS:
                break
            # 失败后回到 while 顶：由上面的分支统一判定终态/耗尽/继续等待
        _ensure_grace_until(store, bill, grace_seconds)
        return store.get_bill(bill.id)


def apply_grace_policy(
    store: Store,
    bill_id: str,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
) -> str:
    """检查宽限期并收敛订阅状态。"""
    with store.lock:
        bill = store.get_bill(bill_id)
        sub = store.get_subscription(bill.subscription_id)
        if bill.status == BillStatus.PAID:
            sub.state = SubscriptionState.ACTIVE
            sub.grace_until = None
            logger.info("grace bill=%s paid -> sub=%s active", bill.id, sub.id)
            return sub.state.value

        _ensure_grace_until(store, bill, grace_seconds)
        now = store.now()
        if sub.grace_until is not None and now >= as_utc(sub.grace_until):
            sub.state = SubscriptionState.EXPIRED
            if bill.status == BillStatus.FINALIZED:
                bill.status = BillStatus.FAILED
                bill.failure_reason = FailureReason.RETRY_EXHAUSTED
            logger.warning("grace expired bill=%s sub=%s -> expired at=%s",
                           bill.id, sub.id, now.isoformat())
        else:
            sub.state = SubscriptionState.PAST_DUE
            logger.info("grace active bill=%s sub=%s past_due until=%s now=%s",
                        bill.id, sub.id,
                        sub.grace_until.isoformat() if sub.grace_until else None,
                        now.isoformat())
        return sub.state.value
