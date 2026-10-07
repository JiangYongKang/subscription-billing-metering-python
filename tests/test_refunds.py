"""退款：不超实收、整笔/部分退、幂等、不改写历史账单、退回进抵扣额度。"""

import logging

import pytest

from app import billing, payments, refunds, subscriptions
from app.errors import RefundExceededError
from app.ledger import reconcile
from app.models import BillStatus


def _paid_bill(env, balance=1_000_000, customer="c1"):
    s = env["store"]
    sub, _ = subscriptions.create_subscription(
        s, customer, "basic", anchor_day=1, idempotency_key=f"sub-{customer}")
    payments.ensure_account(s, customer, balance_cents=balance)
    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    out = payments.pay_bill(s, bill.id, bill.subtotal_cents, idempotency_key="pay-full")
    assert out.bill_status == BillStatus.PAID
    return s, sub, s.get_bill(bill.id), out.payment


def test_full_refund_returns_money_to_credit_and_keeps_bill_intact(env, caplog):
    """场景：整笔退款 —— 账单金额/明细/状态不变，钱进入账户抵扣额度，流水对得上。"""
    caplog.set_level(logging.INFO)
    s, sub, bill, payment = _paid_bill(env)
    lines_before = [(l.kind, l.amount_cents) for l in bill.lines]

    refund, created = refunds.refund_payment(
        s, payment.id, reason="服务不满意", idempotency_key="rf-1")
    assert created and refund.amount_cents == payment.amount_cents
    assert refund.payment_id == payment.id               # 能说清退的是哪一笔

    bill = s.get_bill(bill.id)
    assert bill.status == BillStatus.PAID                # 历史账单状态不改写
    assert bill.subtotal_cents == payment.amount_cents   # 金额冻结
    assert [(l.kind, l.amount_cents) for l in bill.lines] == lines_before
    assert s.accounts["c1"].credit_balance_cents == payment.amount_cents
    rec = reconcile(s, bill.id)
    assert rec["balanced"]
    assert rec["refunded_cents"] == payment.amount_cents
    assert rec["net_received_cents"] == 0


def test_partial_refunds_accumulate_but_never_exceed_received(env, caplog):
    """场景：分多次部分退款 —— 累计不得超过该笔实收，超额拒绝且账目不变。"""
    caplog.set_level(logging.INFO)
    s, sub, bill, payment = _paid_bill(env)
    total = payment.amount_cents

    r1, _ = refunds.refund_payment(s, payment.id, amount_cents=300,
                                   reason="部分退 1", idempotency_key="rf-1")
    r2, _ = refunds.refund_payment(s, payment.id, amount_cents=200,
                                   reason="部分退 2", idempotency_key="rf-2")
    assert r1.amount_cents == 300 and r2.amount_cents == 200
    assert s.payments[payment.id].refunded_cents == 500
    assert s.accounts["c1"].credit_balance_cents == 500

    with pytest.raises(RefundExceededError):
        refunds.refund_payment(s, payment.id, amount_cents=total - 500 + 1,
                               reason="超额", idempotency_key="rf-3")
    # 拒绝后账目不变
    assert s.payments[payment.id].refunded_cents == 500
    assert s.accounts["c1"].credit_balance_cents == 500
    rec = reconcile(s, bill.id)
    assert rec["balanced"] and rec["refunded_cents"] == 500
    assert rec["net_received_cents"] == total - 500


def test_refund_is_idempotent(env, caplog):
    """场景：同一退款重复提交 —— 返回首次退款，抵扣额度只加一次。"""
    caplog.set_level(logging.INFO)
    s, sub, bill, payment = _paid_bill(env)
    r1, c1 = refunds.refund_payment(s, payment.id, amount_cents=100,
                                    reason="x", idempotency_key="rf-dup")
    r2, c2 = refunds.refund_payment(s, payment.id, amount_cents=100,
                                    reason="x", idempotency_key="rf-dup")
    assert c1 is True and c2 is False
    assert r1.id == r2.id
    assert s.payments[payment.id].refunded_cents == 100
    assert s.accounts["c1"].credit_balance_cents == 100


def test_refund_defaults_to_full_remaining(env):
    """场景：不填金额 = 退掉该笔剩余可退余额。"""
    s, sub, bill, payment = _paid_bill(env)
    refunds.refund_payment(s, payment.id, amount_cents=100,
                           reason="先退一点", idempotency_key="rf-a")
    r, _ = refunds.refund_payment(s, payment.id, reason="退剩余",
                                  idempotency_key="rf-b")
    assert r.amount_cents == payment.amount_cents - 100
    assert s.payments[payment.id].refundable_cents == 0


def test_refund_works_on_auto_charge_payment(env, caplog):
    """场景：自动扣款成功的账单也能退（退款锚定到扣款产生的 Payment）。"""
    caplog.set_level(logging.INFO)
    s = env["store"]
    sub, _ = subscriptions.create_subscription(
        s, "c2", "basic", anchor_day=1, idempotency_key="sub-c2")
    payments.ensure_account(s, "c2", balance_cents=1_000_000)
    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    payments.charge_bill(s, bill.id)
    assert s.get_bill(bill.id).status == BillStatus.PAID

    payment = next(p for p in s.payments.values() if p.bill_id == bill.id)
    assert payment.method == "charge"
    refund, _ = refunds.refund_payment(s, payment.id, amount_cents=100,
                                       reason="扣多了", idempotency_key="rf-c2")
    assert refund.amount_cents == 100
    assert s.accounts["c2"].credit_balance_cents == 100
    assert reconcile(s, bill.id)["balanced"]
