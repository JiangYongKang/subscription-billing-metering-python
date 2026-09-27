"""订阅变更按比例计费、历史不可变、调价不影响历史。"""

import logging


def test_multiple_changes_same_period_deterministic(env, caplog):
    """同账期内升级+降级，无论调用顺序，按时间切分出的金额确定可解释。"""
    caplog.set_level(logging.INFO)
    s, clock = env["store"], env["clock"]
    from app import billing, subscriptions

    sub, _ = subscriptions.create_subscription(
        s, "c1", "basic", anchor_day=1, idempotency_key="r1")
    # day 5 升级
    clock.advance(days=4)
    subscriptions.change_plan(s, sub.id, "pro", idempotency_key="r2")
    # day 15 降级
    clock.advance(days=10)
    subscriptions.change_plan(s, sub.id, "basic", idempotency_key="r3")

    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)

    proration = [l for l in bill.lines if l.kind == "proration"]
    assert [l.detail["plan_code"] for l in proration] == ["basic", "pro", "basic"]
    assert [l.detail["segment_days"] for l in proration] == [4, 10, 14]
    # floor(1000*4/28) + floor(3000*10/28) + floor(1000*14/28)
    assert bill.subtotal_cents == 142 + 1071 + 500
    # 每行明细给出公式，判定依据可解释
    assert all("formula" in l.detail for l in proration)


def test_repeat_change_request_is_idempotent(env):
    s = env["store"]
    from app import subscriptions

    sub, _ = subscriptions.create_subscription(
        s, "c1", "basic", anchor_day=1, idempotency_key="r1")
    c1 = subscriptions.change_plan(s, sub.id, "pro", idempotency_key="r2")
    c2 = subscriptions.change_plan(s, sub.id, "pro", idempotency_key="r2")
    assert c1.id == c2.id
    assert len([c for c in s.changes if c.subscription_id == sub.id]) == 2


def test_cancel_immediate_proration_and_history_immutable(env):
    """立即取消按天结到取消日；之后重复出账/推进都不重算已冻结金额。"""
    s, clock = env["store"], env["clock"]
    from app import billing, subscriptions

    sub, _ = subscriptions.create_subscription(
        s, "c1", "basic", anchor_day=1, idempotency_key="r1")
    clock.advance(days=9)  # 2/10 取消
    subscriptions.cancel_subscription(s, sub.id, "immediate", idempotency_key="r2")

    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    # 2/1 -> 2/10 共 9 天
    lines = bill.lines
    create_line = lines[0]
    assert create_line.detail["segment_days"] == 9
    frozen = bill.subtotal_cents

    # 再次 finalize 原样返回；改套餐价也不影响
    billing.finalize_bill(s, bill.id)
    assert bill.subtotal_cents == frozen
    from app.models import Plan
    s.put_plan(Plan(code="basic", name="Basic", base_price_cents=9999,
                    included_units=100, overage_unit_price_cents=5, version=2))
    billing.finalize_bill(s, bill.id)
    assert bill.subtotal_cents == frozen
    assert lines[0].detail["plan_version"] == 1


def test_price_change_does_not_rewrite_paid_bill(env):
    s = env["store"]
    from app import billing, payments, subscriptions

    sub, _ = subscriptions.create_subscription(
        s, "c1", "basic", anchor_day=1, idempotency_key="r1")
    payments.ensure_account(s, "c1", balance_cents=1_000_000)
    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    assert bill.lines[0].detail["base_price_cents"] == 1000
    payments.charge_bill(s, bill.id)
    assert bill.status.value == "paid"
    paid_amount = bill.subtotal_cents

    from app.models import Plan
    s.put_plan(Plan(code="basic", name="Basic", base_price_cents=2000,
                    included_units=100, overage_unit_price_cents=5, version=2))
    # 已付账单金额与明细不变
    s_bill = s.get_bill(bill.id)
    assert s_bill.subtotal_cents == paid_amount
    assert s_bill.lines[0].detail["base_price_cents"] == 1000
