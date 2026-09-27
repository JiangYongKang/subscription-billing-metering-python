"""按比例计费：确定性、可解释、多次变更与同日变更。"""
from datetime import date

from app.models import (ChangePlanIn, CreateSubscriptionIn,
                        InvoiceStatus, PlanSegment, PlanTier)
from app.pricing import build_base_line_items
import app.billing as B


def test_single_change_is_deterministic_and_explainable(store, config, gateway):
    sub = B.create_subscription(
        store, CreateSubscriptionIn(tier=PlanTier.BASIC,
                                    start_date=date(2026, 9, 1)),
        as_of=date(2026, 9, 1), config=config)
    B.change_plan(store, sub.id,
                  ChangePlanIn(tier=PlanTier.PRO,
                               effective_date=date(2026, 9, 15)),
                  as_of=date(2026, 9, 15))

    inv = store.get_invoice(sub.id, date(2026, 9, 1))
    assert inv.status == InvoiceStatus.DRAFT
    kinds = [(li.kind.value, li.amount_cents) for li in inv.line_items]
    # basic 1000*14/30=466.67->467；pro 3000*16/30=1600；合计 2067
    assert kinds == [("base", 467), ("base", 1600)]
    assert inv.total_cents == 2067
    # 行内 meta 可解释：单价、天数、账期天数都留痕
    for li, days in zip(inv.line_items, (14, 16)):
        assert li.meta["segment_days"] == days
        assert li.meta["period_days"] == 30


def test_multiple_changes_same_period_segments_cover_each_day_once(store, config):
    """同账期多次变更：所有片段天数之和 == 账期天数，且结果确定。"""
    sub = B.create_subscription(
        store, CreateSubscriptionIn(tier=PlanTier.BASIC,
                                    start_date=date(2026, 9, 1)),
        as_of=date(2026, 9, 1), config=config)
    B.change_plan(store, sub.id,
                  ChangePlanIn(tier=PlanTier.PRO,
                               effective_date=date(2026, 9, 10)),
                  as_of=date(2026, 9, 10))
    B.change_plan(store, sub.id,
                  ChangePlanIn(tier=PlanTier.ENTERPRISE,
                               effective_date=date(2026, 9, 20)),
                  as_of=date(2026, 9, 20))

    inv = store.get_invoice(sub.id, date(2026, 9, 1))
    seg_days = sum(li.meta["segment_days"] for li in inv.line_items)
    assert seg_days == 30  # 每天恰好计一次，无重叠无空洞

    # 独立用分段函数复算，金额必须一致
    segs = [PlanSegment(PlanTier.BASIC, date(2026, 9, 1), date(2026, 9, 10)),
            PlanSegment(PlanTier.PRO, date(2026, 9, 10), date(2026, 9, 20)),
            PlanSegment(PlanTier.ENTERPRISE, date(2026, 9, 20),
                        date(2026, 10, 1))]
    ref = build_base_line_items(segs, date(2026, 9, 1), date(2026, 10, 1))
    assert [li.amount_cents for li in ref] == [li.amount_cents
                                               for li in inv.line_items]
    # 精确：1000*9/30=300, 3000*10/30=1000, 10000*11/30=3666.67->3667
    assert inv.total_cents == 300 + 1000 + 3667


def test_same_day_changes_collapse_to_final_choice(store, config):
    """同一天反复改套餐：等价于当天直接选最终套餐。"""
    sub = B.create_subscription(
        store, CreateSubscriptionIn(tier=PlanTier.BASIC,
                                    start_date=date(2026, 9, 1)),
        as_of=date(2026, 9, 1), config=config)
    B.change_plan(store, sub.id,
                  ChangePlanIn(tier=PlanTier.PRO,
                               effective_date=date(2026, 9, 15)),
                  as_of=date(2026, 9, 15))
    B.change_plan(store, sub.id,
                  ChangePlanIn(tier=PlanTier.ENTERPRISE,
                               effective_date=date(2026, 9, 15)),
                  as_of=date(2026, 9, 15))
    inv = store.get_invoice(sub.id, date(2026, 9, 1))
    assert [li.tier for li in inv.line_items] == [PlanTier.BASIC,
                                                  PlanTier.ENTERPRISE]
    assert inv.total_cents == 467 + 5333  # 1000*14/30 + 10000*16/30


def test_changes_must_be_in_order(store, config):
    from app.models import BillingError
    sub = B.create_subscription(
        store, CreateSubscriptionIn(tier=PlanTier.BASIC,
                                    start_date=date(2026, 9, 1)),
        as_of=date(2026, 9, 1), config=config)
    B.change_plan(store, sub.id,
                  ChangePlanIn(tier=PlanTier.PRO,
                               effective_date=date(2026, 9, 20)),
                  as_of=date(2026, 9, 20))
    # 乱序回填被拒绝
    try:
        B.change_plan(store, sub.id,
                      ChangePlanIn(tier=PlanTier.ENTERPRISE,
                                   effective_date=date(2026, 9, 10)),
                      as_of=date(2026, 9, 21))
    except BillingError as e:
        assert e.code == "change_order_violation"
    else:
        raise AssertionError("乱序变更应被拒绝")


def test_finalized_history_not_recomputed(store, config, gateway):
    """出账后金额固化：即使后续改套餐/再次跑周期，历史账单不变。"""
    sub = B.create_subscription(
        store, CreateSubscriptionIn(tier=PlanTier.BASIC,
                                    start_date=date(2026, 9, 1)),
        as_of=date(2026, 9, 1), config=config)
    B.change_plan(store, sub.id,
                  ChangePlanIn(tier=PlanTier.PRO,
                               effective_date=date(2026, 9, 15)),
                  as_of=date(2026, 9, 15))
    B.run_billing_cycle(store, as_of=date(2026, 10, 1),
                        config=config, gateway=gateway)
    sep = store.get_invoice(sub.id, date(2026, 9, 1))
    assert sep.status == InvoiceStatus.PAID
    frozen = sep.total_cents
    # 10 月内再做任何操作
    B.change_plan(store, sub.id,
                  ChangePlanIn(tier=PlanTier.ENTERPRISE,
                               effective_date=date(2026, 10, 10)),
                  as_of=date(2026, 10, 10))
    B.run_billing_cycle(store, as_of=date(2026, 11, 1),
                        config=config, gateway=gateway)
    sep2 = store.get_invoice(sub.id, date(2026, 9, 1))
    assert sep2.total_cents == frozen
    assert sep2.status == InvoiceStatus.PAID


def test_price_change_does_not_rewrite_finalized_history(store, config, gateway):
    """套餐调价：已出账历史账单金额不变，新账期按新价出账。"""
    from app.catalog import update_plan_price

    sub = B.create_subscription(
        store, CreateSubscriptionIn(tier=PlanTier.BASIC,
                                    start_date=date(2026, 9, 1)),
        as_of=date(2026, 9, 1), config=config)
    B.run_billing_cycle(store, as_of=date(2026, 10, 1),
                        config=config, gateway=gateway)
    sep = store.get_invoice(sub.id, date(2026, 9, 1))
    assert sep.status == InvoiceStatus.PAID and sep.total_cents == 1000

    update_plan_price(PlanTier.BASIC, base_price_cents=1500)  # 10->15元
    try:
        B.run_billing_cycle(store, as_of=date(2026, 11, 1),
                            config=config, gateway=gateway)
        # 历史不变
        assert sep.total_cents == 1000
        assert sep.line_items[0].meta["unit_price_cents"] == 1000
        # 新账期按新价
        oct_inv = store.get_invoice(sub.id, date(2026, 10, 1))
        assert oct_inv.total_cents == 1500
        assert oct_inv.line_items[0].meta["unit_price_cents"] == 1500
    finally:
        update_plan_price(PlanTier.BASIC, base_price_cents=1000,
                          overage_price_cents=20)
