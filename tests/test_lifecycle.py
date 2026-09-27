"""取消、账期末取消、取消后历史不重算、API 端到端。"""
from datetime import date

from app.models import (CancelIn, CreateSubscriptionIn, InvoiceStatus,
                        PlanTier, SubscriptionStatus)
import app.billing as B


def test_immediate_cancel_prorates_and_freezes(store, config, gateway):
    sub = B.create_subscription(
        store, CreateSubscriptionIn(tier=PlanTier.BASIC,
                                    start_date=date(2026, 9, 1)),
        as_of=date(2026, 9, 1), config=config)
    B.cancel_subscription(
        store, sub.id,
        CancelIn(effective_date=date(2026, 9, 10), at_period_end=False),
        as_of=date(2026, 9, 10), config=config, gateway=gateway)

    inv = store.get_invoice(sub.id, date(2026, 9, 1))
    assert inv.finalized is True
    assert inv.status == InvoiceStatus.PAID
    # 费用只计 9/1~9/10 共 10 天：1000*10/30=333.33->333
    assert inv.total_cents == 333
    assert sub.status == SubscriptionStatus.CANCELED

    # 取消后再跑周期任务：历史账单金额不变，也不产生续费
    before = len(store.invoices)
    B.run_billing_cycle(store, as_of=date(2026, 12, 1),
                        config=config, gateway=gateway)
    assert len(store.invoices) == before
    assert inv.total_cents == 333
    assert B.reconcile(store)["ok"] is True


def test_cancel_at_period_end_keeps_service_then_stops(store, config, gateway):
    sub = B.create_subscription(
        store, CreateSubscriptionIn(tier=PlanTier.PRO,
                                    start_date=date(2026, 9, 1)),
        as_of=date(2026, 9, 1), config=config)
    B.cancel_subscription(store, sub.id, CancelIn(at_period_end=True),
                          as_of=date(2026, 9, 20), config=config,
                          gateway=gateway)
    assert sub.status == SubscriptionStatus.ACTIVE  # 账期结束前仍有效
    assert sub.cancel_effective_period_end is True
    # 9 月整月费照付
    B.run_billing_cycle(store, as_of=date(2026, 10, 1),
                        config=config, gateway=gateway)
    assert sub.status == SubscriptionStatus.CANCELED
    sep = store.get_invoice(sub.id, date(2026, 9, 1))
    assert sep.total_cents == 3000 and sep.status == InvoiceStatus.PAID
    # 不产生 10 月账单
    assert store.get_invoice(sub.id, date(2026, 10, 1)) is None


def test_api_end_to_end_retry_exhaustion_flow(client):
    """HTTP 层完整跑一遍：开通→计量→出账失败→重试耗尽→对账。"""
    r = client.post("/subscriptions", json={
        "tier": "basic", "start_date": "2026-09-01",
        "payment_scenario": "insufficient_funds", "fail_times": 99})
    assert r.status_code == 201
    sid = r.json()["id"]

    for d in ("2026-09-05", "2026-09-20"):
        assert client.post(f"/subscriptions/{sid}/usage",
                           json={"event_id": "u-" + d, "quantity": 30,
                                 "occurred_on": d}).status_code == 201

    for asof in ("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-12"):
        client.post("/billing/run", json={"as_of": asof})

    inv = client.get(f"/subscriptions/{sid}/invoices").json()[0]
    assert inv["status"] == "failed"
    assert inv["last_failure_reason"] == "retry_exhausted"
    assert inv["attempts"] == 4
    assert inv["total_cents"] == 1000  # 用量60未超额

    # 宽限期满订阅收敛
    client.post("/billing/run", json={"as_of": "2026-10-16"})
    sub = client.get(f"/subscriptions/{sid}").json()
    assert sub["status"] == "canceled"

    rec = client.get("/reconcile").json()
    assert rec["ok"] is True


def test_api_card_declined_reason_is_distinct(client):
    r = client.post("/subscriptions", json={
        "tier": "pro", "start_date": "2026-09-01",
        "payment_scenario": "card_declined"})
    sid = r.json()["id"]
    client.post("/billing/run", json={"as_of": "2026-10-01"})
    inv = client.get(f"/subscriptions/{sid}/invoices").json()[0]
    assert inv["status"] == "failed"
    assert inv["last_failure_reason"] == "card_declined"  # 不与重试耗尽混淆
    assert inv["attempts"] == 1
