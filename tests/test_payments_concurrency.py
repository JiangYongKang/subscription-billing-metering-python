"""并发扣款幂等、失败原因区分、重试耗尽、宽限期收敛、资金对账。"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app import billing, grace, payments, subscriptions
from app.ledger import reconcile
from app.models import BillStatus, ChargeResult, FailureReason, SubscriptionState


def _finalized_bill(env, balance=1_000_000, card_active=True):
    s = env["store"]
    sub, _ = subscriptions.create_subscription(
        s, "c1", "basic", anchor_day=1, idempotency_key="r1")
    payments.ensure_account(s, "c1", balance_cents=balance, card_active=card_active)
    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    return s, sub, bill


def test_concurrent_charge_succeeds_only_once(env, caplog):
    """同一账单并发发起扣款：真实成功恰好一次，余额只扣一次。"""
    caplog.set_level(logging.INFO)
    s, sub, bill = _finalized_bill(env, balance=1_000_000)
    attempts_out = []
    barrier = threading.Barrier(12)

    def worker():
        barrier.wait()
        attempts_out.append(payments.charge_bill(s, bill.id))

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: worker(), range(12)))

    bill = s.get_bill(bill.id)
    # 所有并发调用都得到成功语义（返回同一 attempt），但真实扣款只能一次：
    # 1) 返回的都是同一个成功 attempt
    assert all(a.result == ChargeResult.SUCCESS for a in attempts_out)
    assert len({a.id for a in attempts_out}) == 1
    # 2) 真实扣款尝试记录只有 1 条（锁串行化 + 幂等键仲裁）
    attempts = [a for a in s.attempts.values() if a.bill_id == bill.id]
    assert len(attempts) == 1
    assert bill.status == BillStatus.PAID
    # 3) 余额只扣一次，成功流水只有一条
    assert s.accounts["c1"].balance_cents == 1_000_000 - bill.subtotal_cents
    rec = reconcile(s, bill.id)
    assert rec["balanced"]
    success_entries = [e for e in rec["entries"] if e["type"] == "charge_success"]
    assert len(success_entries) == 1
    # 4) 资金安全的硬证据：12 个并发调用的网关扣减总额 == 单次账单金额
    assert sum(
        e.amount_cents for e in s.ledger if e.bill_id == bill.id
    ) == bill.subtotal_cents


def test_insufficient_funds_is_retryable_and_retries_do_not_double_charge(env):
    s, clock, sub_ref = env["store"], env["clock"], {}
    sub_ref["s"], sub, bill = _finalized_bill(env, balance=50)
    a1 = payments.charge_bill(s, bill.id)
    assert a1.result == ChargeResult.INSUFFICIENT_FUNDS
    assert s.get_bill(bill.id).failure_reason == FailureReason.INSUFFICIENT_FUNDS
    assert s.get_subscription(sub.id).state == SubscriptionState.PAST_DUE

    # 未到退避点不重试
    grace.run_retries(s, bill.id, max_attempts=4, base_interval_sec=3600)
    assert s.get_bill(bill.id).charge_attempts == 1
    # 到点充值，重试成功
    clock.advance(seconds=3601)
    s.accounts["c1"].balance_cents = 1_000_000
    grace.run_retries(s, bill.id, max_attempts=4, base_interval_sec=3600)
    bill = s.get_bill(bill.id)
    assert bill.status == BillStatus.PAID
    assert s.get_subscription(sub.id).state == SubscriptionState.ACTIVE
    # 重试成功后再扣款不重复收钱
    bal = s.accounts["c1"].balance_cents
    a_again = payments.charge_bill(s, bill.id)
    assert a_again.attempt_no == 2
    assert s.accounts["c1"].balance_cents == bal
    assert reconcile(s, bill.id)["balanced"]


def test_card_declined_is_terminal(env):
    s, sub, bill = _finalized_bill(env, balance=1_000_000, card_active=False)
    a = payments.charge_bill(s, bill.id)
    assert a.result == ChargeResult.CARD_DECLINED
    # 跑重试不会再发起尝试：终态直接 failed
    out = grace.run_retries(s, bill.id)
    assert out.status == BillStatus.FAILED
    assert out.failure_reason == FailureReason.CARD_DECLINED
    assert out.charge_attempts == 1


def test_retry_exhausted_then_grace_expires(env):
    """余额始终不足：重试次数耗尽 -> failed；宽限期过 -> 订阅 expired。"""
    s, clock, sub_ref = env["store"], env["clock"], {}
    sub_ref["s"], sub, bill = _finalized_bill(env, balance=0)
    base = 100  # 固定退避基准；第 n 次失败后等待 base * 2^(n-1)
    grace.run_retries(s, bill.id, max_attempts=4, base_interval_sec=base)
    for n in (1, 2, 3):  # 依次跨过 attempt 1/2/3 的退避点
        clock.advance(seconds=base * 2 ** (n - 1) + 1)
        grace.run_retries(s, bill.id, max_attempts=4, base_interval_sec=base)
    bill = s.get_bill(bill.id)
    assert bill.status == BillStatus.FAILED
    assert bill.failure_reason == FailureReason.RETRY_EXHAUSTED
    assert bill.charge_attempts == 4

    # 宽限期内仍 past_due；宽限结束 expired
    state = grace.apply_grace_policy(s, bill.id, grace_seconds=3 * 24 * 3600)
    assert state == SubscriptionState.PAST_DUE.value
    clock.advance(days=4)
    state = grace.apply_grace_policy(s, bill.id, grace_seconds=3 * 24 * 3600)
    assert state == SubscriptionState.EXPIRED.value
    assert s.get_subscription(sub.id).state == SubscriptionState.EXPIRED


def test_no_payment_method_distinguished(env):
    s = env["store"]
    sub, _ = subscriptions.create_subscription(
        s, "c2", "basic", anchor_day=1, idempotency_key="r1")
    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    a = payments.charge_bill(s, bill.id)
    assert a.result == ChargeResult.GATEWAY_ERROR
    assert s.get_bill(bill.id).failure_reason == FailureReason.NO_PAYMENT_METHOD


def test_ledger_matches_bill_on_retry_path(env):
    """失败-重试成功路径：失败流水为 0，成功流水唯一且等于账单金额。"""
    s, clock, sub_ref = env["store"], env["clock"], {}
    sub_ref["s"], sub, bill = _finalized_bill(env, balance=0)
    payments.charge_bill(s, bill.id)
    clock.advance(seconds=3601)
    s.accounts["c1"].balance_cents = 5000
    grace.run_retries(s, bill.id, base_interval_sec=3600)
    rec = reconcile(s, bill.id)
    assert rec["balanced"]
    types = [(e["type"], e["amount_cents"]) for e in rec["entries"]]
    assert types.count(("charge_failure", 0)) == 1
    assert types.count(("charge_success", bill.subtotal_cents)) == 1
