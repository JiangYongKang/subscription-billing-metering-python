"""扣款：并发只成功一次、幂等不重复扣、失败原因可区分、重试与宽限收敛。"""
import threading
from datetime import date, timedelta

from app.models import (BillingError, CreateSubscriptionIn, FailureReason,
                        InvoiceStatus, PlanTier, SubscriptionStatus, TxnStatus)
from app.payments import charge_invoice
import app.billing as B


def _open_invoice(store, config, gateway, *, scenario="ok", fail_times=0,
                  start=date(2026, 9, 1), run_on=date(2026, 10, 1)):
    sub = B.create_subscription(
        store,
        CreateSubscriptionIn(tier=PlanTier.BASIC, start_date=start,
                             payment_scenario=scenario,
                             fail_times=fail_times),
        as_of=start, config=config)
    B.run_billing_cycle(store, as_of=run_on, config=config, gateway=gateway)
    inv = store.get_invoice(sub.id, start)
    return sub, inv


def test_concurrent_charges_only_one_succeeds(store, config, gateway):
    """同一张已出账账单并发扣款：恰好一笔成功、只有一条成功流水。"""
    # 场景：账单 OPEN（首次失败），再由多线程同时补扣
    sub, inv = _open_invoice(store, config, gateway,
                             scenario="insufficient_funds", fail_times=1)
    assert inv.status == InvoiceStatus.OPEN

    results = []
    errors = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()  # 尽量放大竞争
        try:
            txn = charge_invoice(store, inv, as_of=date(2026, 10, 2),
                                 config=config, gateway=gateway)
            results.append(txn)
        except BillingError as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    succ_ids = {t.id for t in results if t.status == TxnStatus.SUCCESS}
    assert len(succ_ids) == 1, f"成功扣款流水={succ_ids}，应只有1条"
    assert inv.status == InvoiceStatus.PAID
    # 流水层面也只能有一条成功，金额唯一
    inv_txns = [t for t in store.transactions if t.invoice_id == inv.id]
    assert sum(1 for t in inv_txns if t.status == TxnStatus.SUCCESS) == 1
    assert B.reconcile(store)["ok"] is True


def test_same_idempotency_key_charges_once(store, config, gateway):
    sub, inv = _open_invoice(store, config, gateway,
                             scenario="insufficient_funds", fail_times=2)
    # fail_times=2：第1次（出账，attempt=1）失败；手动同 key 首扣(attempt=2)
    # 仍失败，同 key 重试必须返回同一笔失败流水；再换 key(attempt=3) 成功。
    t1 = charge_invoice(store, inv, as_of=date(2026, 10, 2),
                        config=config, gateway=gateway,
                        idempotency_key="manual-xyz")
    assert t1.status == TxnStatus.FAILED and t1.attempt == 2
    t2 = charge_invoice(store, inv, as_of=date(2026, 10, 2),
                        config=config, gateway=gateway,
                        idempotency_key="manual-xyz")
    assert t2.id == t1.id  # 同 key 返回同一流水（不增加尝试次数）
    assert inv.attempts == 2
    t3 = charge_invoice(store, inv, as_of=date(2026, 10, 5),
                        config=config, gateway=gateway,
                        idempotency_key="manual-ok")
    assert t3.status == TxnStatus.SUCCESS and t3.attempt == 3
    # 已付清后再用任意 key 调用，仍是同一笔成功流水
    t4 = charge_invoice(store, inv, as_of=date(2026, 10, 6),
                        config=config, gateway=gateway,
                        idempotency_key="other")
    assert t4.id == t3.id
    assert inv.status == InvoiceStatus.PAID
    assert B.reconcile(store)["ok"] is True


def test_retry_succeeds_after_failure_without_double_charge(store, config, gateway):
    """余额不足：按节奏重试，恢复后成功，全程只成功扣一次。"""
    sub, inv = _open_invoice(store, config, gateway,
                             scenario="insufficient_funds", fail_times=3)
    assert inv.last_failure_reason == FailureReason.INSUFFICIENT_FUNDS
    assert sub.status == SubscriptionStatus.PAST_DUE

    # 未到计划重试日：周期任务不提前扣
    early = B.run_billing_cycle(store, as_of=date(2026, 10, 1),
                                config=config, gateway=gateway)
    assert early["attempts"] == []

    # +1 天（第2次）、+4 天（第3次）仍失败；+11 天（第4次）成功
    B.run_billing_cycle(store, as_of=date(2026, 10, 2),
                        config=config, gateway=gateway)
    assert inv.status == InvoiceStatus.OPEN and inv.attempts == 2
    B.run_billing_cycle(store, as_of=date(2026, 10, 5),
                        config=config, gateway=gateway)
    assert inv.status == InvoiceStatus.OPEN and inv.attempts == 3
    B.run_billing_cycle(store, as_of=date(2026, 10, 12),
                        config=config, gateway=gateway)
    assert inv.status == InvoiceStatus.PAID
    assert inv.attempts == 4
    txns = [t for t in store.transactions if t.invoice_id == inv.id]
    assert [t.status for t in txns].count(TxnStatus.SUCCESS) == 1
    assert sub.status == SubscriptionStatus.ACTIVE  # 补交后恢复服务
    assert B.reconcile(store)["ok"] is True


def test_retry_exhaustion_is_distinguishable(store, config, gateway):
    """始终余额不足：4 次尝试耗尽 -> FAILED/retry_exhausted，订阅宽限期后取消。"""
    sub, inv = _open_invoice(store, config, gateway,
                             scenario="insufficient_funds", fail_times=99)
    for as_of in (date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 12)):
        B.run_billing_cycle(store, as_of=as_of, config=config, gateway=gateway)
    assert inv.attempts == 4
    assert inv.status == InvoiceStatus.FAILED
    assert inv.last_failure_reason == FailureReason.RETRY_EXHAUSTED
    # 宽限期（出账日+14天）结束仍未付：状态收敛
    assert sub.status == SubscriptionStatus.PAST_DUE
    B.run_billing_cycle(store, as_of=date(2026, 10, 16),
                        config=config, gateway=gateway)
    assert sub.status == SubscriptionStatus.CANCELED
    # 宽限期最后一天仍保留服务（边界：grace_ends_on 当天不取消）
    rec = B.reconcile(store)
    assert rec["ok"] is True


def test_card_declined_is_permanent_and_distinguishable(store, config, gateway):
    """卡片失效：永久失败，立即 FAILED，原因不被记成 retry_exhausted。"""
    sub, inv = _open_invoice(store, config, gateway,
                             scenario="card_declined")
    assert inv.status == InvoiceStatus.FAILED
    assert inv.attempts == 1
    assert inv.last_failure_reason == FailureReason.CARD_DECLINED
    # 之后即使时间推进也不再尝试扣款
    B.run_billing_cycle(store, as_of=date(2026, 11, 1),
                        config=config, gateway=gateway)
    assert inv.attempts == 1
    txns = [t for t in store.transactions if t.invoice_id == inv.id]
    assert all(t.reason == FailureReason.CARD_DECLINED for t in txns)


def test_grace_period_boundary(store, config, gateway):
    """宽限边界：第14天仍 past_due，第15天 canceled。"""
    sub, inv = _open_invoice(store, config, gateway,
                             scenario="insufficient_funds", fail_times=99)
    issued = inv.issued_on  # 2026-10-01
    grace_last = issued + timedelta(days=14)
    # 先耗尽重试
    B.run_billing_cycle(store, as_of=issued + timedelta(days=11),
                        config=config, gateway=gateway)
    B.run_billing_cycle(store, as_of=grace_last,
                        config=config, gateway=gateway)
    assert sub.status == SubscriptionStatus.PAST_DUE
    B.run_billing_cycle(store, as_of=grace_last + timedelta(days=1),
                        config=config, gateway=gateway)
    assert sub.status == SubscriptionStatus.CANCELED
