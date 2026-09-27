"""订阅生命周期：创建、升级、降级、取消。

所有变更都以 PlanChange 形式落库，且写入时对套餐价做快照；同一账期内
任意多次变更，出账时按变更序列切出确定的按比例分段。变更接口按
idempotency_key 幂等：重复提交返回首次结果，不产生第二条分段。
"""

import logging

from app.clock import as_utc
from app.errors import ConflictError, StateError
from app.models import PlanChange, Subscription, SubscriptionState
from app.period import initial_period, period_index_for
from app.store import Store

logger = logging.getLogger("billing.subscriptions")


def _replay(store: Store, idempotency_key: str) -> PlanChange | None:
    return store.idempotents.get(f"sub:{idempotency_key}")  # type: ignore[return-value]


def _save_change(store: Store, key: str, change: PlanChange) -> None:
    store.changes.append(change)
    store.idempotents[f"sub:{key}"] = change


def create_subscription(
    store: Store,
    customer_id: str,
    plan_code: str,
    anchor_day: int,
    idempotency_key: str,
    created_at=None,
) -> tuple[Subscription, PlanChange]:
    with store.lock:
        existing = _replay(store, idempotency_key)
        if existing is not None:
            ch: PlanChange = existing
            sub = store.get_subscription(ch.subscription_id)
            logger.info("create_subscription replay idem=%s sub=%s", idempotency_key, sub.id)
            return sub, ch

        plan = store.get_plan(plan_code)
        ts = as_utc(created_at) if created_at else store.now()
        p_start, p_end = initial_period(ts, anchor_day)
        sub = Subscription(
            id=store.new_id("sub"),
            customer_id=customer_id,
            plan_code=plan.code,
            plan_version=plan.version,
            state=SubscriptionState.ACTIVE,
            anchor_day=anchor_day,
            created_at=ts,
            current_period_start=p_start,
            current_period_end=p_end,
        )
        store.subscriptions[sub.id] = sub
        change = PlanChange(
            id=store.new_id("chg"),
            subscription_id=sub.id,
            period_index=0,
            effective_at=ts,
            plan_code=plan.code,
            plan_version=plan.version,
            action="create",
            base_price_cents=plan.base_price_cents,
            included_units=plan.included_units,
            overage_unit_price_cents=plan.overage_unit_price_cents,
            idempotency_key=idempotency_key,
        )
        _save_change(store, idempotency_key, change)
        logger.info(
            "create_subscription idem=%s sub=%s plan=%s v%d price=%d period=[%s,%s)",
            idempotency_key, sub.id, plan.code, plan.version, plan.base_price_cents,
            p_start.date(), p_end.date(),
        )
        return sub, change


def _require_mutable(store: Store, sub: Subscription, now) -> None:
    if sub.state in (SubscriptionState.CANCELED, SubscriptionState.EXPIRED):
        raise StateError(f"subscription {sub.id} is {sub.state.value}, change forbidden")
    if now >= as_utc(sub.current_period_end):
        raise StateError(
            f"current period of {sub.id} ended at {sub.current_period_end}; "
            "advance the period first"
        )


def change_plan(
    store: Store,
    subscription_id: str,
    new_plan_code: str,
    idempotency_key: str,
    action: str | None = None,
) -> PlanChange:
    with store.lock:
        existing = _replay(store, idempotency_key)
        if existing is not None:
            logger.info("change_plan replay idem=%s", idempotency_key)
            return existing  # type: ignore[return-value]

        sub = store.get_subscription(subscription_id)
        now = store.now()
        _require_mutable(store, sub, now)

        plan = store.get_plan(new_plan_code)
        if not plan.active:
            raise StateError(f"plan {new_plan_code} is inactive")
        if action is None:
            action = "upgrade" if plan.base_price_cents >= _current_price(store, sub) else "downgrade"

        pidx = period_index_for(sub.current_period_start, now, sub.anchor_day)
        change = PlanChange(
            id=store.new_id("chg"),
            subscription_id=sub.id,
            period_index=pidx,
            effective_at=now,
            plan_code=plan.code,
            plan_version=plan.version,
            action=action,
            base_price_cents=plan.base_price_cents,
            included_units=plan.included_units,
            overage_unit_price_cents=plan.overage_unit_price_cents,
            idempotency_key=idempotency_key,
            note="同一账期内按比例分段",
        )
        _save_change(store, idempotency_key, change)
        sub.plan_code = plan.code
        sub.plan_version = plan.version
        logger.info(
            "change_plan sub=%s -> %s v%d action=%s at=%s idem=%s",
            sub.id, plan.code, plan.version, action, now.isoformat(), idempotency_key,
        )
        return change


def _current_price(store: Store, sub: Subscription) -> int:
    return store.get_plan(sub.plan_code, sub.plan_version).base_price_cents


def cancel_subscription(
    store: Store,
    subscription_id: str,
    effective: str,
    idempotency_key: str,
) -> PlanChange:
    with store.lock:
        existing = _replay(store, idempotency_key)
        if existing is not None:
            logger.info("cancel_subscription replay idem=%s", idempotency_key)
            return existing  # type: ignore[return-value]
        if effective not in ("immediate", "end_of_period"):
            raise ConflictError("effective must be 'immediate' or 'end_of_period'")

        sub = store.get_subscription(subscription_id)
        now = store.now()
        if sub.state in (SubscriptionState.CANCELED, SubscriptionState.EXPIRED):
            raise StateError(f"subscription {sub.id} already {sub.state.value}")

        pidx = period_index_for(sub.current_period_start, now, sub.anchor_day)
        plan = store.get_plan(sub.plan_code, sub.plan_version)
        if effective == "immediate":
            eff = now if now < as_utc(sub.current_period_end) else as_utc(sub.current_period_end)
            change = PlanChange(
                id=store.new_id("chg"),
                subscription_id=sub.id,
                period_index=pidx,
                effective_at=eff,
                plan_code=plan.code,
                plan_version=plan.version,
                action="cancel",
                base_price_cents=plan.base_price_cents,
                included_units=plan.included_units,
                overage_unit_price_cents=plan.overage_unit_price_cents,
                idempotency_key=idempotency_key,
                note="立即取消，当前账期按天按比例结至取消时刻",
            )
            _save_change(store, idempotency_key, change)
            sub.state = SubscriptionState.CANCELED
            sub.canceled_at = now
            sub.cancel_effective = "immediate"
            logger.info("cancel_subscription sub=%s immediate at=%s idem=%s",
                        sub.id, now.isoformat(), idempotency_key)
            return change

        # 到期取消：不追加零天分段，当前账期出账后在 advance_period 收敛状态
        sub.cancel_effective = "end_of_period"
        sub.canceled_at = now
        marker = PlanChange(
            id=store.new_id("chg"),
            subscription_id=sub.id,
            period_index=pidx,
            effective_at=as_utc(sub.current_period_end),
            plan_code=plan.code,
            plan_version=plan.version,
            action="cancel",
            base_price_cents=0,
            included_units=0,
            overage_unit_price_cents=0,
            idempotency_key=idempotency_key,
            note="到期取消标记：当前账期按整期出账，不产生零天分段，出账时跳过本行",
        )
        _save_change(store, idempotency_key, marker)
        logger.info("cancel_subscription sub=%s end_of_period marker_at=%s idem=%s",
                    sub.id, sub.current_period_end, idempotency_key)
        return marker


def list_changes(store: Store, subscription_id: str, period_index: int) -> list[PlanChange]:
    with store.lock:
        return sorted(
            (c for c in store.changes
             if c.subscription_id == subscription_id and c.period_index == period_index),
            key=lambda c: c.effective_at,
        )
