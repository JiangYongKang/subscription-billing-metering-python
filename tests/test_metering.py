"""用量计量：重复/延迟/乱序上报的幂等性，以及超额单独成行。"""
from datetime import date

from app.metering import period_usage, report_usage
from app.models import (BillingError, CreateSubscriptionIn, PlanTier)
import app.billing as B


def _make_sub(store, config, tier=PlanTier.BASIC, start=date(2026, 9, 1)):
    return B.create_subscription(
        store, CreateSubscriptionIn(tier=tier, start_date=start),
        as_of=start, config=config)


def test_duplicate_events_counted_once(store, config):
    sub = _make_sub(store, config)
    r1 = report_usage(store, sub, "evt-1", 10, date(2026, 9, 2))
    r2 = report_usage(store, sub, "evt-1", 10, date(2026, 9, 3))
    # 重复上报即便数量不同也不二次累计
    r3 = report_usage(store, sub, "evt-1", 999, date(2026, 9, 2))
    assert r1.id == r2.id == r3.id
    assert period_usage(store, sub.id, date(2026, 9, 1)) == 10


def test_out_of_order_and_late_events_total_is_stable(store, config):
    """先报月底、再报月初、延迟补报：去重累计与顺序无关。"""
    sub = _make_sub(store, config)
    report_usage(store, sub, "c", 30, date(2026, 9, 28))
    report_usage(store, sub, "a", 10, date(2026, 9, 1))
    report_usage(store, sub, "b", 20, date(2026, 9, 15))
    # 延迟补报（账期未出账前允许）
    report_usage(store, sub, "a-late", 5, date(2026, 9, 2))
    assert period_usage(store, sub.id, date(2026, 9, 1)) == 65
    # 再整体重复一遍，累计不变
    for eid, qty, d in [("c", 30, date(2026, 9, 28)),
                        ("a", 10, date(2026, 9, 1)),
                        ("b", 20, date(2026, 9, 15)),
                        ("a-late", 5, date(2026, 9, 2))]:
        report_usage(store, sub, eid, qty, d)
    assert period_usage(store, sub.id, date(2026, 9, 1)) == 65


def test_overage_shown_as_separate_line(store, config, gateway):
    sub = _make_sub(store, config, tier=PlanTier.BASIC)  # 含100，超0.20元
    report_usage(store, sub, "u1", 80, date(2026, 9, 5))
    report_usage(store, sub, "u2", 50, date(2026, 9, 20))  # 累计130
    B.run_billing_cycle(store, as_of=date(2026, 10, 1),
                        config=config, gateway=gateway)
    inv = store.get_invoice(sub.id, date(2026, 9, 1))
    overage = [li for li in inv.line_items if li.kind.value == "overage"]
    base = [li for li in inv.line_items if li.kind.value == "base"]
    assert len(overage) == 1
    assert overage[0].units == 30
    assert overage[0].amount_cents == 30 * 20  # 600 分
    assert base[0].amount_cents == 1000
    assert inv.total_cents == 1600


def test_overage_is_not_double_charged_after_duplicate_report(store, config, gateway):
    sub = _make_sub(store, config, tier=PlanTier.BASIC)
    for _ in range(5):  # 同一事件重复报 5 次
        report_usage(store, sub, "burst", 120, date(2026, 9, 9))
    B.run_billing_cycle(store, as_of=date(2026, 10, 1),
                        config=config, gateway=gateway)
    inv = store.get_invoice(sub.id, date(2026, 9, 1))
    overage = [li for li in inv.line_items if li.kind.value == "overage"]
    assert overage[0].units == 20          # 只按一次 120 计算
    assert overage[0].amount_cents == 400
    assert inv.total_cents == 1400


def test_late_event_into_closed_period_rejected(store, config, gateway):
    """账期出账后晚到事件不能改写历史，要明确拒绝。"""
    sub = _make_sub(store, config)
    B.run_billing_cycle(store, as_of=date(2026, 10, 1),
                        config=config, gateway=gateway)
    try:
        report_usage(store, sub, "late", 10, date(2026, 9, 15))
    except BillingError as e:
        assert e.code == "period_closed"
    else:
        raise AssertionError("历史账期已出账，晚到事件应被拒绝")


def test_usage_api_duplicate_flag(client):
    r = client.post("/subscriptions",
                    json={"tier": "basic", "start_date": "2026-09-01"})
    sid = r.json()["id"]
    payload = {"event_id": "e1", "quantity": 10, "occurred_on": "2026-09-03"}
    first = client.post(f"/subscriptions/{sid}/usage", json=payload)
    second = client.post(f"/subscriptions/{sid}/usage", json=payload)
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is True
    assert first.json()["period_total"] == second.json()["period_total"] == 10
