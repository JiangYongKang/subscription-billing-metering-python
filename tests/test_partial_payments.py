"""分次付款：逐笔付满、幂等防重、并发不超收、超付拦截、已结清不再收钱。"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import billing, payments, subscriptions
from app.errors import OverpaymentError, StateError
from app.ledger import reconcile
from app.models import BillStatus


def _finalized_bill(env, balance=1_000_000, customer="c1"):
    s = env["store"]
    sub, _ = subscriptions.create_subscription(
        s, customer, "basic", anchor_day=1, idempotency_key=f"sub-{customer}")
    payments.ensure_account(s, customer, balance_cents=balance)
    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    return s, sub, s.get_bill(bill.id)


def test_partial_payments_accumulate_until_paid(env, caplog):
    """场景：一张账单分两笔付清 —— 每步可见剩余，付满后唯一收敛为 PAID。"""
    caplog.set_level(logging.INFO)
    s, sub, bill = _finalized_bill(env)
    total = bill.subtotal_cents
    first, second = total // 3, total - total // 3

    out1 = payments.pay_bill(s, bill.id, first, idempotency_key="p1")
    assert out1.bill_status == BillStatus.FINALIZED      # 未付满仍是 finalized
    assert out1.remaining_cents == total - first         # 能看出还差多少
    assert s.accounts["c1"].balance_cents == 1_000_000 - first

    out2 = payments.pay_bill(s, bill.id, second, idempotency_key="p2")
    assert out2.bill_status == BillStatus.PAID           # 付满唯一收敛
    assert out2.remaining_cents == 0
    bill = s.get_bill(bill.id)
    assert bill.paid_cents == total and bill.paid_at is not None
    assert s.accounts["c1"].balance_cents == 1_000_000 - total
    rec = reconcile(s, bill.id)
    assert rec["balanced"] and rec["paid_cents"] == total


def test_same_idempotency_key_does_not_double_charge(env, caplog):
    """场景：同一笔付款重复提交 —— 返回首次结果，余额只扣一次。"""
    caplog.set_level(logging.INFO)
    s, sub, bill = _finalized_bill(env)
    out1 = payments.pay_bill(s, bill.id, 300, idempotency_key="dup-key")
    out2 = payments.pay_bill(s, bill.id, 300, idempotency_key="dup-key")
    assert out2.duplicate is True
    assert out2.payment.id == out1.payment.id
    assert s.accounts["c1"].balance_cents == 1_000_000 - 300
    assert s.get_bill(bill.id).paid_cents == 300


def test_concurrent_payments_never_overcollect(env, caplog):
    """场景：并发发起多笔付款 —— 合计实收不超过应付，付满后收敛为 PAID。"""
    caplog.set_level(logging.INFO)
    s, sub, bill = _finalized_bill(env)
    total = bill.subtotal_cents
    # 10 个并发各付 1/4 账单：理论上最多 4 笔成功，其余被超付拦截
    amount = total // 4
    outcomes, errors = [], []
    barrier = threading.Barrier(10)

    def worker(i):
        barrier.wait()
        try:
            outcomes.append(payments.pay_bill(s, bill.id, amount, f"pc-{i}"))
        except OverpaymentError as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(worker, range(10)))

    bill = s.get_bill(bill.id)
    accepted = [o for o in outcomes if o.payment is not None]
    settled = [o for o in outcomes if o.already_settled]
    assert bill.paid_cents <= total                      # 硬约束：不多收
    assert bill.paid_cents == len(accepted) * amount
    assert bill.status == BillStatus.PAID                # 4*1/4 恰好付满
    # 付满后的并发请求按已结清处理（或恰好撞上超付拦截），都不会再收钱
    assert len(accepted) + len(settled) + len(errors) == 10
    assert s.accounts["c1"].balance_cents == 1_000_000 - total
    assert reconcile(s, bill.id)["balanced"]


def test_payment_after_settled_is_not_charged_again(env, caplog):
    """场景：账单付满后再发起付款 —— 按已结清处理，不再收钱。"""
    caplog.set_level(logging.INFO)
    s, sub, bill = _finalized_bill(env)
    payments.pay_bill(s, bill.id, bill.subtotal_cents, idempotency_key="full")
    assert s.get_bill(bill.id).status == BillStatus.PAID
    bal = s.accounts["c1"].balance_cents

    out = payments.pay_bill(s, bill.id, 100, idempotency_key="late")
    assert out.already_settled is True and out.payment is None
    assert s.accounts["c1"].balance_cents == bal         # 没再扣钱
    assert s.get_bill(bill.id).paid_cents == bill.subtotal_cents


def test_overpayment_rejected_and_remaining_visible(env, caplog):
    """场景：单笔金额超过剩余应付 —— 拒绝且账目不变，剩余金额可查。"""
    caplog.set_level(logging.INFO)
    s, sub, bill = _finalized_bill(env)
    payments.pay_bill(s, bill.id, 400, idempotency_key="p1")
    remaining = bill.subtotal_cents - 400
    with pytest.raises(OverpaymentError):
        payments.pay_bill(s, bill.id, remaining + 1, idempotency_key="p2")
    bill = s.get_bill(bill.id)
    assert bill.paid_cents == 400
    assert bill.remaining_cents == remaining
    assert s.accounts["c1"].balance_cents == 1_000_000 - 400


def test_open_bill_rejects_payment(env):
    """场景：未出账账单不能付款。"""
    s = env["store"]
    sub, _ = subscriptions.create_subscription(
        s, "c9", "basic", anchor_day=1, idempotency_key="sub-c9")
    payments.ensure_account(s, "c9", balance_cents=1000)
    bill = billing.ensure_open_bill(s, sub.id)
    with pytest.raises(StateError):
        payments.pay_bill(s, bill.id, 100, idempotency_key="too-early")


def test_partial_then_auto_charge_collects_only_remainder(env, caplog):
    """场景：先分次付一部分，再触发自动扣款 —— 只扣剩余，不重复收钱。"""
    caplog.set_level(logging.INFO)
    s, sub, bill = _finalized_bill(env)
    payments.pay_bill(s, bill.id, 400, idempotency_key="part")
    attempt = payments.charge_bill(s, bill.id)
    assert attempt.amount_cents == bill.subtotal_cents - 400
    bill = s.get_bill(bill.id)
    assert bill.status == BillStatus.PAID
    assert s.accounts["c1"].balance_cents == 1_000_000 - bill.subtotal_cents
    assert reconcile(s, bill.id)["balanced"]
