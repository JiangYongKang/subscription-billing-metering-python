"""账户抵扣：退款沉淀额度、出账自动冲抵、按序生效、不重复、可对账。"""

import logging

from app import billing, credits, payments, refunds, subscriptions
from app.ledger import reconcile
from app.models import BillStatus


def _paid_bill_with_credit(env, customer="c1", balance=1_000_000):
    """付清第一张账单并整笔退款，让账户沉淀抵扣额度；返回 (store, sub, bill, credit)。"""
    s = env["store"]
    sub, _ = subscriptions.create_subscription(
        s, customer, "basic", anchor_day=1, idempotency_key=f"sub-{customer}")
    payments.ensure_account(s, customer, balance_cents=balance)
    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    out = payments.pay_bill(s, bill.id, bill.subtotal_cents, idempotency_key="pay-1")
    refunds.refund_payment(s, out.payment.id, reason="整笔退", idempotency_key="rf-1")
    credit = s.accounts[customer].credit_balance_cents
    assert credit == bill.subtotal_cents
    return s, sub, s.get_bill(bill.id), credit


def _new_bill(s, customer, key):
    """同一客户再开一张新账单（新订阅首单）并出账，返回该账单。"""
    sub, _ = subscriptions.create_subscription(
        s, customer, "basic", anchor_day=1, idempotency_key=key)
    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    bill = s.get_bill(bill.id)
    assert bill.subtotal_cents > 0
    return bill


def test_credit_auto_applies_on_finalize_and_covers_full_bill(env, caplog):
    """场景：退款额度 >= 新账单 —— 出账即全额冲抵，账单直接 PAID，不再扣款。"""
    caplog.set_level(logging.INFO)
    s, sub, bill1, credit = _paid_bill_with_credit(env)
    bal = s.accounts["c1"].balance_cents

    bill2 = _new_bill(s, "c1", "sub-2")
    assert bill2.status == BillStatus.PAID               # 抵扣付满，唯一收敛
    assert bill2.credit_applied_cents == bill2.subtotal_cents
    assert bill2.remaining_cents == 0
    assert s.accounts["c1"].credit_balance_cents == credit - bill2.subtotal_cents
    assert s.accounts["c1"].balance_cents == bal         # 没动支付方式
    rec = reconcile(s, bill2.id)
    assert rec["balanced"] and rec["credit_applied_cents"] == bill2.subtotal_cents
    apps = [c for c in s.credit_apps.values() if c.bill_id == bill2.id]
    assert len(apps) == 1                                # 独立可对账的抵扣记录


def test_partial_credit_then_remaining_goes_to_payment(env, caplog):
    """场景：额度不够抵整单 —— 先抵一部分，剩余走原支付方式，合计正好付清。"""
    caplog.set_level(logging.INFO)
    s, sub, bill1, credit = _paid_bill_with_credit(env)
    s.accounts["c1"].credit_balance_cents = 400          # 只留 400 额度

    bill2 = _new_bill(s, "c1", "sub-2")
    assert bill2.status == BillStatus.FINALIZED
    assert bill2.credit_applied_cents == 400
    assert bill2.remaining_cents == bill2.subtotal_cents - 400
    assert s.accounts["c1"].credit_balance_cents == 0

    # 剩余部分走原支付方式（分次付款）
    out = payments.pay_bill(s, bill2.id, bill2.remaining_cents, idempotency_key="rest")
    assert out.bill_status == BillStatus.PAID
    bill2 = s.get_bill(bill2.id)
    assert bill2.paid_cents + bill2.credit_applied_cents == bill2.subtotal_cents
    rec = reconcile(s, bill2.id)
    assert rec["balanced"]


def test_credit_applies_in_bill_order_and_never_twice(env, caplog):
    """场景：额度按出账顺序冲抵；同一账单重复触发不会重复抵扣。"""
    caplog.set_level(logging.INFO)
    s, sub, bill1, credit = _paid_bill_with_credit(env)

    bill2 = _new_bill(s, "c1", "sub-2")
    left_after_bill2 = s.accounts["c1"].credit_balance_cents
    assert bill2.credit_applied_cents == bill2.subtotal_cents   # 先抵满 bill2
    # 重复触发：一张账单只应用一次
    again = credits.apply_credit_to_bill(s, bill2.id)
    assert again is None
    assert s.accounts["c1"].credit_balance_cents == left_after_bill2
    assert s.get_bill(bill2.id).credit_applied_cents == bill2.subtotal_cents

    # 第三张账单按顺序使用剩余额度
    bill3 = _new_bill(s, "c1", "sub-3")
    total_applied = bill2.credit_applied_cents + bill3.credit_applied_cents
    assert total_applied == credit                       # 额度恰好按序用完
    assert s.accounts["c1"].credit_balance_cents == 0
    # 账户不变量：抵扣额度 = 累计退款 - 累计抵扣
    refunded = sum(r.amount_cents for r in s.refunds.values())
    applied = sum(c.amount_cents for c in s.credit_apps.values())
    assert refunded - applied == s.accounts["c1"].credit_balance_cents


def test_no_credit_no_application(env):
    """场景：账户无抵扣额度 —— 出账不产生抵扣记录，走原支付流程。"""
    s = env["store"]
    sub, _ = subscriptions.create_subscription(
        s, "c3", "basic", anchor_day=1, idempotency_key="sub-c3")
    payments.ensure_account(s, "c3", balance_cents=1_000_000)
    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    bill = s.get_bill(bill.id)
    assert bill.credit_applied_cents == 0
    assert not [c for c in s.credit_apps.values() if c.bill_id == bill.id]
    assert bill.status == BillStatus.FINALIZED
