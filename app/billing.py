"""订阅生命周期、周期性出账、失败重试与宽限收敛、资金对账。

时间推进模型：所有出账/重试/宽限判定都由 run_billing_cycle(as_of) 以
显式日期驱动，测试可任意快进时间，结果完全确定。

账单生命周期：
  DRAFT（账期内，可随套餐变更/用量重算）
   ──账期结束（或立即取消）出账──▶ OPEN（issued_on 固化，首次扣款）
        ├─扣款成功──────────────▶ PAID（不可变）
        ├─永久失败(卡片失效)────▶ FAILED
        └─按节奏重试，耗尽──────▶ FAILED（原因 retry_exhausted）
订阅在 OPEN 期间为 past_due 宽限态；宽限期满仍未付清则收敛为 canceled。
"""
from __future__ import annotations

import itertools
import logging
from datetime import date, timedelta

from .config import RetryConfig
from .metering import period_records
from .models import (BillingError, CreateSubscriptionIn, Invoice,
                     InvoiceStatus, PlanSegment, Subscription,
                     SubscriptionStatus)
from .payments import MockGateway, TxnStatus, charge_invoice
from .pricing import next_period, period_bounds, recompute_draft
from .store import Store

logger = logging.getLogger("billing.core")

_sub_seq = itertools.count(1)
_inv_seq = itertools.count(1)


def reset_counters() -> None:
    global _sub_seq, _inv_seq
    _sub_seq = itertools.count(1)
    _inv_seq = itertools.count(1)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _refresh_draft(store: Store, sub: Subscription) -> Invoice | None:
    """按当前 segments 与账期内用量重算 DRAFT 账单。"""
    inv = store.get_invoice(sub.id, sub.current_period_start)
    if inv is None or inv.status != InvoiceStatus.DRAFT:
        return inv
    records = period_records(store, sub.id, sub.current_period_start)
    recompute_draft(inv, sub.segments, records)
    return inv


def _new_draft_invoice(sub: Subscription, period_start: date,
                       period_end: date, segments: list[PlanSegment]) -> Invoice:
    inv = Invoice(
        id=f"inv-{next(_inv_seq)}",
        subscription_id=sub.id,
        period_start=period_start,
        period_end=period_end,
        status=InvoiceStatus.DRAFT,
    )
    recompute_draft(inv, segments, [])
    return inv


def _issue(invoice: Invoice, issued_on: date, config: RetryConfig) -> None:
    """DRAFT → OPEN：固化金额与出账日，安排首次扣款。"""
    if invoice.status != InvoiceStatus.DRAFT:
        raise BillingError("invoice_not_draft",
                           f"账单 {invoice.id} 无法出账: {invoice.status.value}")
    invoice.status = InvoiceStatus.OPEN
    invoice.finalized = True
    invoice.issued_on = issued_on
    invoice.next_attempt_on = config.scheduled_attempt_on(issued_on, 1)
    logger.info("invoice-issued %s period=%s total=%s issued_on=%s",
                invoice.id, invoice.period_start, invoice.total_cents, issued_on)


def _on_failed_payment(store: Store, sub: Subscription, invoice: Invoice,
                       config: RetryConfig) -> None:
    if sub.status != SubscriptionStatus.CANCELED:
        sub.status = SubscriptionStatus.PAST_DUE
    sub.grace_ends_on = (invoice.issued_on or date.today()) + timedelta(
        days=config.grace_days)
    logger.info("sub-past-due %s invoice=%s grace_until=%s reason=%s",
                sub.id, invoice.id, sub.grace_ends_on,
                invoice.last_failure_reason.value)


def _on_paid(store: Store, sub: Subscription) -> None:
    sub.grace_ends_on = None
    if sub.status == SubscriptionStatus.PAST_DUE:
        sub.status = SubscriptionStatus.ACTIVE
    logger.info("sub-active %s", sub.id)


# ---------------------------------------------------------------------------
# 订阅创建 / 变更 / 取消
# ---------------------------------------------------------------------------
def create_subscription(store: Store, body: CreateSubscriptionIn, *,
                        as_of: date, config: RetryConfig) -> Subscription:
    from .catalog import get_plan
    get_plan(body.tier)  # 校验套餐存在
    start = body.start_date or as_of
    ps, pe = period_bounds(start)
    if start < ps:
        start = ps

    with store.lock:
        sub = Subscription(
            id=f"sub-{next(_sub_seq)}",
            tier=body.tier,
            status=SubscriptionStatus.ACTIVE,
            created_on=start,
            current_period_start=ps,
            current_period_end=pe,
            segments=[PlanSegment(body.tier, start, pe, note="订阅开通")],
            payment_mode={"scenario": body.payment_scenario,
                          "fail_times": body.fail_times},
        )
        store.subscriptions[sub.id] = sub
        draft = _new_draft_invoice(sub, ps, pe, sub.segments)
        store.put_invoice(draft)
        logger.info("sub-created %s tier=%s start=%s draft=%s amount=%s",
                    sub.id, body.tier.value, start, draft.id, draft.total_cents)
        return sub


def change_plan(store: Store, sub_id: str, body, *, as_of: date) -> Subscription:
    """同一账期内多次升降级：以片段切分保证按比例费用确定、可解释。

    变更只能发生在「当前最后一个片段」内（含其起点），即变更日期必须
    单调不小于上次变更日；不允许回填到已过去的片段中间。
    """
    with store.lock:
        sub = store.require_subscription(sub_id)
        if sub.status == SubscriptionStatus.CANCELED:
            raise BillingError("subscription_canceled",
                               f"订阅 {sub.id} 已取消，不能变更套餐")
        eff = body.effective_date or as_of
        if not (sub.current_period_start <= eff < sub.current_period_end):
            raise BillingError(
                "effective_date_out_of_period",
                f"生效日 {eff} 不在当前账期 "
                f"[{sub.current_period_start}, {sub.current_period_end}) 内")
        last = sub.segments[-1]
        if eff < last.start:
            raise BillingError(
                "change_order_violation",
                f"生效日 {eff} 早于上次变更日 {last.start}，"
                "同一账期内变更必须按时间顺序提交")
        if eff >= last.end:
            raise BillingError("effective_date_out_of_period",
                               f"生效日 {eff} 已超出片段结束日 {last.end}")

        old_tier = sub.tier
        if eff == last.start:
            # 同一天的再次变更：直接改写当天片段，结果与“只提交最终选择”一致。
            last.tier = body.tier
            last.note = f"套餐变更 {old_tier.value} -> {body.tier.value}(同日)"
        else:
            last.end = eff
            sub.segments.append(PlanSegment(
                tier=body.tier, start=eff, end=sub.current_period_end,
                note=f"套餐变更 {old_tier.value} -> {body.tier.value}"))
        sub.tier = body.tier

        draft = _refresh_draft(store, sub)
        logger.info(
            "plan-changed %s %s -> %s eff=%s 片段数=%s draft_total=%s",
            sub.id, old_tier.value, body.tier.value, eff,
            len(sub.segments), draft.total_cents if draft else None)
        return sub


def cancel_subscription(store: Store, sub_id: str, body, *, as_of: date,
                        config: RetryConfig, gateway: MockGateway) -> Subscription:
    with store.lock:
        sub = store.require_subscription(sub_id)
        if sub.status == SubscriptionStatus.CANCELED:
            raise BillingError("subscription_canceled",
                               f"订阅 {sub.id} 已取消")
        eff = body.effective_date or as_of

        if body.at_period_end:
            if eff < sub.current_period_end:
                sub.cancel_effective_period_end = True
                sub.cancel_on = sub.current_period_end
                logger.info("cancel-scheduled %s at_period_end=%s",
                            sub.id, sub.current_period_end)
                return sub
            # 已跨过账期结束（理论上由周期任务先处理），按立即处理。

        # 立即取消：费用只计到取消日当天，账单一经出账立即冻结。
        if not (sub.current_period_start <= eff < sub.current_period_end):
            raise BillingError(
                "effective_date_out_of_period",
                f"取消日 {eff} 不在当前账期内")
        last = sub.segments[-1]
        if eff < last.start:
            raise BillingError("change_order_violation",
                               f"取消日 {eff} 早于上次变更日 {last.start}")
        cutoff = eff + timedelta(days=1)  # 费用含取消日
        last.end = min(last.end, cutoff)
        sub.segments = [s for s in sub.segments if s.end > s.start]
        sub.cancel_on = eff
        sub.cancel_effective_period_end = False

        draft = store.get_invoice(sub.id, sub.current_period_start)
        if draft is not None and draft.status == InvoiceStatus.DRAFT:
            records = [r for r in period_records(
                store, sub.id, sub.current_period_start)
                if r.occurred_on <= eff]
            recompute_draft(draft, sub.segments, records)
            _issue(draft, as_of, config)
            txn = charge_invoice(store, draft, as_of=as_of, config=config,
                                 gateway=gateway)
            if txn.status == TxnStatus.FAILED:
                _on_failed_payment(store, sub, draft, config)

        sub.status = SubscriptionStatus.CANCELED
        logger.info("cancel-immediate %s on=%s invoice=%s status=%s",
                    sub.id, eff, draft.id if draft else None,
                    draft.status.value if draft else None)
        return sub


# ---------------------------------------------------------------------------
# 周期任务：出账 / 扣款 / 重试 / 宽限收敛 / 续费
# ---------------------------------------------------------------------------
def _rollover_subscription(store: Store, sub: Subscription, *, as_of: date,
                           config: RetryConfig, gateway: MockGateway) -> None:
    """账期结束：固化当前 DRAFT、首次扣款、决定续费或终止。"""
    draft = store.get_invoice(sub.id, sub.current_period_start)
    if draft is not None and draft.status == InvoiceStatus.DRAFT:
        _refresh_draft(store, sub)
        _issue(draft, sub.current_period_end, config)
        txn = charge_invoice(store, draft, as_of=as_of, config=config,
                             gateway=gateway,
                             respect_schedule=True)
        if txn.status == TxnStatus.FAILED:
            _on_failed_payment(store, sub, draft, config)
        else:
            _on_paid(store, sub)

    terminate = sub.cancel_effective_period_end
    if terminate:
        sub.status = SubscriptionStatus.CANCELED
        sub.cancel_on = sub.current_period_end
        sub.cancel_effective_period_end = False
        logger.info("sub-canceled-at-period-end %s on=%s",
                    sub.id, sub.current_period_end)
        return

    # 续费：按最新套餐开通下一账期 DRAFT。
    ps2, pe2 = next_period(sub.current_period_start)
    sub.current_period_start, sub.current_period_end = ps2, pe2
    sub.segments = [PlanSegment(sub.tier, ps2, pe2, note="周期续费")]
    new_draft = _new_draft_invoice(sub, ps2, pe2, sub.segments)
    store.put_invoice(new_draft)
    logger.info("sub-renewed %s period=%s tier=%s draft=%s",
                sub.id, ps2, sub.tier.value, new_draft.id)


def _retry_open_invoices(store: Store, *, as_of: date,
                         config: RetryConfig, gateway: MockGateway) -> list[str]:
    """对所有到期待重试账单执行扣款；返回本次产生的扣款流水 id。"""
    acted: list[str] = []
    for inv in list(store.invoices.values()):
        if inv.status != InvoiceStatus.OPEN:
            continue
        sub = store.subscriptions.get(inv.subscription_id)
        if sub is None:
            continue
        if inv.next_attempt_on is None or as_of < inv.next_attempt_on:
            continue
        txn = charge_invoice(store, inv, as_of=as_of, config=config,
                             gateway=gateway, respect_schedule=True)
        acted.append(txn.id)
        if txn.status == TxnStatus.SUCCESS:
            _on_paid(store, sub)
        else:
            _on_failed_payment(store, sub, inv, config)
    return acted


def _enforce_grace(store: Store, *, as_of: date) -> None:
    """宽限期结束仍未付清：订阅状态收敛为 canceled（账单继续保留账目）。

    宽限期自账单出账日起算，与重试是否耗尽无关：OPEN（仍在重试）或
    FAILED（重试耗尽/卡片失效）的未付清账单，超过宽限日都触发收敛。
    """
    for inv in store.invoices.values():
        if inv.status not in (InvoiceStatus.OPEN, InvoiceStatus.FAILED):
            continue
        sub = store.subscriptions.get(inv.subscription_id)
        if sub is None or sub.status == SubscriptionStatus.CANCELED:
            continue
        if sub.grace_ends_on is not None and as_of > sub.grace_ends_on:
            sub.status = SubscriptionStatus.CANCELED
            sub.cancel_on = as_of
            logger.warning(
                "grace-expired %s invoice=%s issued_on=%s grace_until=%s "
                "as_of=%s -> canceled（欠款账单保留，状态=%s 原因=%s）",
                sub.id, inv.id, inv.issued_on, sub.grace_ends_on, as_of,
                inv.status.value, inv.last_failure_reason.value)


def run_billing_cycle(store: Store, *, as_of: date,
                      config: RetryConfig, gateway: MockGateway) -> dict:
    """推进全部订阅到 as_of：续费出账 → 重试 → 宽限收敛。"""
    with store.lock:
        rolled: list[str] = []
        for sub in list(store.subscriptions.values()):
            if sub.status == SubscriptionStatus.CANCELED and \
                    not sub.cancel_effective_period_end:
                continue
            as_of_ps, _ = period_bounds(as_of)
            while as_of >= sub.current_period_end and \
                    sub.status != SubscriptionStatus.CANCELED:
                _rollover_subscription(
                    store, sub, as_of=as_of, config=config, gateway=gateway)
                rolled.append(sub.id)
                if sub.status == SubscriptionStatus.CANCELED:
                    break
            # 账期内：让 DRAFT 反映最新用量（金额仍可变，尚未出账）。
            if sub.status != SubscriptionStatus.CANCELED:
                _refresh_draft(store, sub)

        acted = _retry_open_invoices(
            store, as_of=as_of, config=config, gateway=gateway)
        _enforce_grace(store, as_of=as_of)
        return {"as_of": as_of.isoformat(), "rolled_over": rolled,
                "attempts": acted}


# ---------------------------------------------------------------------------
# 对账：资金流水必须与账单账目对得上
# ---------------------------------------------------------------------------
def reconcile(store: Store) -> dict:
    """校验：
    1. 每张 PAID 账单恰好一条成功流水，金额相等，且无重复扣款；
    2. 成功流水总额 == 所有 PAID 账单总额；
    3. 失败流水次数与账单 attempts 记录一致；
    4. 不存在无主流水。
    """
    with store.lock:
        errors: list[str] = []
        success_total = 0
        success_count = 0
        for inv in store.invoices.values():
            txns = [t for t in store.transactions if t.invoice_id == inv.id]
            succ = [t for t in txns if t.status == TxnStatus.SUCCESS]
            if inv.status == InvoiceStatus.PAID:
                if len(succ) != 1:
                    errors.append(
                        f"账单 {inv.id} PAID 但成功流水数={len(succ)}（应为1）")
                elif succ[0].amount_cents != inv.total_cents:
                    errors.append(
                        f"账单 {inv.id} 扣款金额 {succ[0].amount_cents} "
                        f"!= 账单金额 {inv.total_cents}")
                else:
                    success_total += succ[0].amount_cents
                    success_count += 1
            else:
                if succ:
                    errors.append(
                        f"账单 {inv.id} 状态 {inv.status.value} 却存在成功流水")
            if len(txns) != inv.attempts:
                errors.append(
                    f"账单 {inv.id} 流水数 {len(txns)} != attempts {inv.attempts}")

        txn_total = sum(t.amount_cents for t in store.transactions
                        if t.status == TxnStatus.SUCCESS)
        if txn_total != success_total:
            errors.append(
                f"成功流水总额 {txn_total} != PAID 账单总额 {success_total}")
        for t in store.transactions:
            if t.invoice_id not in store.invoices:
                errors.append(f"流水 {t.id} 无对应账单")

        paid_total = sum(i.total_cents for i in store.invoices.values()
                         if i.status == InvoiceStatus.PAID)
        result = {
            "ok": not errors,
            "errors": errors,
            "paid_invoices": success_count,
            "paid_total_cents": paid_total,
            "success_txn_total_cents": txn_total,
            "transactions": len(store.transactions),
        }
        if not errors:
            logger.info("reconcile-ok %s", result)
        else:
            logger.error("reconcile-failed %s", errors)
        return result
