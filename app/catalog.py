"""套餐目录：价格只影响尚未出账的费用，历史账单在出账时已固化单价。"""
from __future__ import annotations

from .models import Plan, PlanTier

# 金额单位：分。basic 10 元/月，pro 30 元/月，enterprise 100 元/月。
PLANS: dict[PlanTier, Plan] = {
    PlanTier.BASIC: Plan(
        tier=PlanTier.BASIC,
        name="基础版",
        base_price_cents=1000,
        included_units=100,
        overage_price_cents=20,   # 0.20 元/单位
    ),
    PlanTier.PRO: Plan(
        tier=PlanTier.PRO,
        name="专业版",
        base_price_cents=3000,
        included_units=500,
        overage_price_cents=10,
    ),
    PlanTier.ENTERPRISE: Plan(
        tier=PlanTier.ENTERPRISE,
        name="企业版",
        base_price_cents=10000,
        included_units=2000,
        overage_price_cents=5,
    ),
}


def get_plan(tier: PlanTier) -> Plan:
    if tier not in PLANS:
        raise KeyError(f"未知套餐: {tier}")
    return PLANS[tier]


def update_plan_price(tier: PlanTier, *, base_price_cents: int,
                      overage_price_cents: int | None = None) -> Plan:
    """调价（运营/管理用途）。

    只影响调价后新出账或仍在 DRAFT 的费用：已出账账单的单价在出账时已
    固化进各行 meta 与金额，重开周期任务也不会变。
    """
    plan = get_plan(tier)
    new_overage = (plan.overage_price_cents if overage_price_cents is None
                   else overage_price_cents)
    PLANS[tier] = Plan(
        tier=plan.tier, name=plan.name,
        base_price_cents=base_price_cents,
        included_units=plan.included_units,
        overage_price_cents=new_overage,
    )
    return PLANS[tier]
