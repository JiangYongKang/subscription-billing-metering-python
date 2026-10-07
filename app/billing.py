"""周期性出账。

关键不变量：
- finalize 是冻结动作：金额、明细、套餐价格快照一旦落定就不可再变；
  重复 finalize 原样返回，绝不重算。
- 已支付/已出账的账单不受后续套餐调价影响。
- 关账后才到的迟到用量进入独立的"账后调整单"，历史账单保持不动。
"""

import logging

from app import credits
from app.clock import as_utc
from app.errors import StateError
from app.models import (
    Bill,
    BillLineItem,
    BillStatus,
    PlanChange,
    SubscriptionState,
)
from app.period import advance_window, period_index_for, window_for_index
from app.pricing import build_proration_lines, build_usage_lines, units_by_segment_dates
from app.subscriptions import list_changes
from app.store import Store

logger = logging.getLogger("billing.billing")


def _billable_changes(all_changes: list[PlanChange]) -> list[PlanChange]:
    """到期取消的零天标记行不参与实际计费；立即取消行本身保留。"""
    return [c for c in all_changes if not (c.action == "cancel" and c.base_price_cents == 0)]


def ensure_open_bill(store: Store, subscription_id: str) -> Bill:
    with store.lock:
        sub = store.get_subscription(subscription_id)
        now = store.now()
        pidx = period_index_for(sub.current_period_start, now, sub.anchor_day)
        return _open_bill_for(store, sub, pidx)


def _open_bill_for(store: Store, sub, pidx: int) -> Bill:
    existing = store.period_bill(sub.id, pidx)
    if existing is not None:
        return existing
    p_start, p_end = window_for_index(sub.current_period_start, sub.anchor_day, pidx)
    bill = Bill(
        id=store.new_id("bill"),
        subscription_id=sub.id,
        customer_id=sub.customer_id,
        period_index=pidx,
        period_start=p_start,
        period_end=p_end,
        currency=store.get_plan(sub.plan_code, sub.plan_version).currency,
        status=BillStatus.OPEN,
        created_at=store.now(),
        idempotency_key=f"bill:{sub.id}:{pidx}",
    )
    store.bills[bill.id] = bill
    store.bill_by_period[(sub.id, pidx)] = bill.id
    sub.last_bill_id = bill.id
    logger.info("open_bill sub=%s period=%d window=[%s,%s)",
                sub.id, pidx, p_start.date(), p_end.date())
    return bill


def _events_for(store: Store, sub, p_start, p_end, cutoff=None):
    cutoff = as_utc(cutoff) if cutoff else None
    out = []
    for ev in store.events.values():
        if ev.subscription_id != sub.id or ev.billed:
            continue
        at = as_utc(ev.occurred_at)
        if p_start <= at < p_end and (cutoff is None or at < cutoff):
            out.append(ev)
    return out


def finalize_bill(store: Store, bill_id: str) -> Bill:
    with store.lock:
        bill = store.get_bill(bill_id)
        if bill.status != BillStatus.OPEN:
            logger.info("finalize skip bill=%s status=%s (金额冻结，不重算)",
                        bill.id, bill.status.value)
            return bill

        sub = store.get_subscription(bill.subscription_id)
        all_changes = list_changes(store, sub.id, bill.period_index)
        changes = _billable_changes(all_changes)
        # 立即取消：取消时刻即为服务终止边界，其后不计任何费用
        cancel_changes = [c for c in all_changes if c.action == "cancel"]
        cutoff = min((c.effective_at for c in cancel_changes), default=bill.period_end)
        cutoff = min(cutoff, bill.period_end)

        events = _events_for(store, sub, bill.period_start, bill.period_end, cutoff)
        total_units = sum(e.quantity for e in events)
        seg_units = units_by_segment_dates(
            events, changes, bill.period_start, bill.period_end, clamp_end=cutoff
        )

        lines = build_proration_lines(
            changes, bill.period_start, bill.period_end, cutoff=cutoff
        )
        lines += build_usage_lines(
            total_units, changes, bill.period_start, bill.period_end,
            units_by_segment=seg_units, cutoff=cutoff,
        )
        bill.lines = lines
        bill.subtotal_cents = sum(l.amount_cents for l in lines)
        bill.total_units = total_units
        bill.overage_units = sum(
            l.detail.get("overage_units", 0) for l in lines if l.kind == "overage"
        )
        bill.status = BillStatus.FINALIZED
        bill.finalized_at = store.now()
        for ev in events:
            ev.billed = True
        logger.info(
            "finalize bill=%s sub=%s period=%d base=%d overage_units=%d total_cents=%d",
            bill.id, sub.id, bill.period_index,
            sum(l.amount_cents for l in lines if l.kind == "proration"),
            bill.overage_units, bill.subtotal_cents,
        )
        # 出账后先用账户抵扣额度冲抵，不足部分再走原支付方式
        credits.apply_credit_to_bill(store, bill.id)
        return bill


def create_adjustment_for_late_usage(store: Store, bill_id: str) -> Bill | None:
    """为已关账账单的迟到/乱序用量生成调整单；无新增返回 None。"""
    with store.lock:
        closed = store.get_bill(bill_id)
        if closed.status == BillStatus.OPEN:
            raise StateError(f"bill {bill_id} still open; finalize it first")
        sub = store.get_subscription(closed.subscription_id)
        events = _events_for(store, sub, closed.period_start, closed.period_end)
        if not events:
            return None
        total_units = sum(e.quantity for e in events)
        # 调整单：历史包含额度已被主单用尽，迟到用量全部按当前订阅价格的
        # 超额单价计，且单价来源取关闭账单最后一个有效分段以保持口径一致。
        changes = _billable_changes(list_changes(store, sub.id, closed.period_index))
        ref = changes[-1] if changes else None
        unit_price = ref.overage_unit_price_cents if ref else 0
        plan_code = ref.plan_code if ref else sub.plan_code
        plan_version = ref.plan_version if ref else sub.plan_version
        amount = total_units * unit_price
        adj = Bill(
            id=store.new_id("bill"),
            subscription_id=sub.id,
            customer_id=sub.customer_id,
            period_index=closed.period_index,
            period_start=closed.period_start,
            period_end=closed.period_end,
            currency=closed.currency,
            status=BillStatus.FINALIZED,
            created_at=store.now(),
            finalized_at=store.now(),
            is_adjustment=True,
            idempotency_key=f"adj:{closed.id}:{len([b for b in store.bills.values() if b.is_adjustment])}",
        )
        adj.lines = [BillLineItem(
            kind="adjustment",
            description=f"账后迟到用量调整 {total_units} 单位 x {unit_price} 分 "
                        f"(基于主单 {closed.id} 末段 {plan_code} v{plan_version})",
            amount_cents=amount,
            detail={
                "parent_bill_id": closed.id,
                "late_units": total_units,
                "unit_price_cents": unit_price,
                "plan_code": plan_code,
                "plan_version": plan_version,
                "event_ids": sorted(e.event_id for e in events),
            },
        )]
        adj.subtotal_cents = amount
        adj.total_units = total_units
        adj.overage_units = total_units
        for ev in events:
            ev.billed = True
        store.bills[adj.id] = adj
        logger.info("adjustment bill=%s parent=%s late_units=%d cents=%d",
                    adj.id, closed.id, total_units, amount)
        credits.apply_credit_to_bill(store, adj.id)
        return adj


def advance_period(store: Store, subscription_id: str) -> Bill:
    with store.lock:
        sub = store.get_subscription(subscription_id)
        now = store.now()
        pidx = period_index_for(sub.current_period_start, now, sub.anchor_day)
        current = store.period_bill(sub.id, pidx)
        if current is None:
            current = _open_bill_for(store, sub, pidx)
        if current.status == BillStatus.OPEN:
            finalize_bill(store, current.id)

        # 到期取消：本账期出账后即收敛为 canceled，不再开新账期
        if sub.cancel_effective == "end_of_period" and sub.state != SubscriptionState.EXPIRED:
            sub.state = SubscriptionState.CANCELED
            sub.canceled_at = sub.canceled_at or now
            logger.info("end_of_period cancel converged sub=%s", sub.id)
            return current

        new_start, new_end = advance_window(sub.current_period_start, sub.anchor_day)
        # 若已经跨多个账期（长时间未推进），逐期补空账期
        while new_end <= now:
            sub.current_period_start, sub.current_period_end = new_start, new_end
            pidx += 1
            gap = _open_bill_for(store, sub, pidx)
            finalize_bill(store, gap.id)
            new_start, new_end = advance_window(new_start, sub.anchor_day)
        sub.current_period_start, sub.current_period_end = new_start, new_end
        next_bill = _open_bill_for(store, sub, pidx + 1)
        return next_bill
