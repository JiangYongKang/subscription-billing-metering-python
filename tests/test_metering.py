"""重复上报、乱序/延迟上报、超额计费、账后迟到用量。"""

import logging
from datetime import timedelta

import pytest

from app.errors import DuplicateEventError, PeriodClosedError


def test_duplicate_event_reported_many_times_counts_once(env, caplog):
    caplog.set_level(logging.INFO)
    s = env["store"]
    from app import metering, subscriptions

    sub, _ = subscriptions.create_subscription(
        s, "c1", "basic", anchor_day=1, idempotency_key="r1")
    t = sub.created_at + timedelta(days=2)
    for _ in range(5):
        ev, created = metering.record_usage(s, sub.id, "evt-1", 30, t, "k1")
    assert metering.period_total(s, sub.id, 0) == 30
    assert not created
    assert any("不重复累计" in r.message for r in caplog.records)


def test_idempotency_key_conflict_rejected(env):
    s = env["store"]
    from app import metering, subscriptions

    sub, _ = subscriptions.create_subscription(
        s, "c1", "basic", anchor_day=1, idempotency_key="r1")
    t = sub.created_at + timedelta(days=1)
    metering.record_usage(s, sub.id, "evt-a", 10, t, "same-key")
    with pytest.raises(DuplicateEventError):
        metering.record_usage(s, sub.id, "evt-b", 10, t, "same-key")
    assert metering.period_total(s, sub.id, 0) == 10


def test_out_of_order_events_total_consistent(env):
    """先报后发生的事件，再补报更早发生的事件，账期累计不受顺序影响。"""
    s = env["store"]
    from app import metering, subscriptions

    sub, _ = subscriptions.create_subscription(
        s, "c1", "basic", anchor_day=1, idempotency_key="r1")
    base = sub.created_at
    ordered = [(f"e{i}", 10 * i, base + timedelta(days=2 + i)) for i in range(1, 6)]
    # 乱序写入：5,3,1,2,4
    for idx in (5, 3, 1, 2, 4):
        eid, qty, at = ordered[idx - 1]
        metering.record_usage(s, sub.id, eid, qty, at)
    total = metering.period_total(s, sub.id, 0)
    assert total == sum(q for _, q, _ in ordered)  # 150


def test_overage_shown_separately_and_deterministic(env):
    """超过包含额度的部分以 overage 行单独体现，重复出账金额不变。"""
    s = env["store"]
    from app import billing, metering, subscriptions

    sub, _ = subscriptions.create_subscription(
        s, "c1", "basic", anchor_day=1, idempotency_key="r1")
    t = sub.created_at + timedelta(days=1)
    metering.record_usage(s, sub.id, "e1", 60, t)
    metering.record_usage(s, sub.id, "e2", 70, t + timedelta(days=2))  # 共 130
    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)

    overage_lines = [l for l in bill.lines if l.kind == "overage"]
    assert bill.total_units == 130
    assert bill.overage_units == 30
    assert sum(l.amount_cents for l in overage_lines) == 30 * 5
    # 二次出账不重算
    billing.finalize_bill(s, bill.id)
    assert bill.overage_units == 30
    assert bill.subtotal_cents == 1000 + 150


def test_late_usage_after_close_goes_to_adjustment_not_recount(env):
    """关账后迟到的用量：主账单不动，生成独立调整单，不漏不重。"""
    s = env["store"]
    from app import billing, metering, subscriptions

    sub, _ = subscriptions.create_subscription(
        s, "c1", "basic", anchor_day=1, idempotency_key="r1")
    t = sub.created_at + timedelta(days=3)
    metering.record_usage(s, sub.id, "e1", 50, t)

    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    frozen = bill.subtotal_cents

    # 迟到（网络延迟）+ 乱序事件，occurred_at 仍落在已关账账期
    late = t + timedelta(days=10)
    # 首次：持久化后报 PeriodClosedError 提示走调整单；重复上报：幂等去重、不再报错
    with pytest.raises(PeriodClosedError):
        metering.record_usage(s, sub.id, "e-late", 40, late)
    ev, created = metering.record_usage(s, sub.id, "e-late", 40, late)
    assert created is False
    assert len([e for e in s.events.values() if e.event_id == "e-late"]) == 1
    adj = billing.create_adjustment_for_late_usage(s, bill.id)
    assert adj is not None
    assert adj.is_adjustment is True
    assert adj.lines[0].kind == "adjustment"
    assert adj.total_units == 40
    assert adj.subtotal_cents == 40 * 5
    # 历史主账单金额、单位数完全不变
    assert s.get_bill(bill.id).subtotal_cents == frozen
    assert s.get_bill(bill.id).total_units == 50
    # 调整单幂等：没有新的迟到事件时返回 None
    assert billing.create_adjustment_for_late_usage(s, bill.id) is None


def test_concurrent_duplicate_reports_count_once(env):
    """多线程同时上报相同 event_id：只落一条，账期累计只增一次。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    s = env["store"]
    from app import metering, subscriptions

    sub, _ = subscriptions.create_subscription(
        s, "c1", "basic", anchor_day=1, idempotency_key="r1")
    t = sub.created_at + timedelta(days=2)
    created_flags = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        _ev, created = metering.record_usage(s, sub.id, "evt-race", 25, t)
        created_flags.append(created)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda _: worker(), range(10)))

    assert created_flags.count(True) == 1
    assert metering.period_total(s, sub.id, 0) == 25
