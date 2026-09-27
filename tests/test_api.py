"""HTTP 端到端：订阅→用量→出账→扣款→重试→对账，含幂等与错误码。"""

import logging
from datetime import timedelta


def _setup_sub(client, customer="cust-api", balance=0, card_active=True):
    r = client.post("/accounts", json={
        "customer_id": customer, "balance_cents": balance,
        "card_active": card_active})
    assert r.status_code == 200
    r = client.post("/subscriptions", json={
        "customer_id": customer, "plan_code": "basic", "anchor_day": 1,
        "idempotency_key": f"sub-{customer}"})
    assert r.status_code == 201
    return r.json()["subscription"]


def test_full_flow_over_http(env, caplog):
    caplog.set_level(logging.INFO)
    c, clock, t0 = env["client"], env["clock"], env["t0"]
    sub = _setup_sub(c, balance=100)
    sid = sub["id"]

    # 上报用量（重复一次）
    for _ in range(2):
        r = c.post(f"/subscriptions/{sid}/usage", json={
            "event_id": "evt-1", "quantity": 130,
            "occurred_at": (t0 + timedelta(days=2)).isoformat(),
            "idempotency_key": "uk-1"})
        assert r.status_code == 200
    body = r.json()
    assert body["duplicate"] is True
    assert body["period_total_units"] == 130

    # 升级
    clock.advance(days=10)
    r = c.post(f"/subscriptions/{sid}/change", json={
        "new_plan_code": "pro", "idempotency_key": "chg-1"})
    assert r.status_code == 200
    assert r.json()["action"] == "upgrade"

    # 出账
    r = c.post(f"/subscriptions/{sid}/bills/finalize")
    assert r.status_code == 200
    bill = r.json()
    assert bill["status"] == "finalized"
    assert bill["overage_units"] > 0
    kinds = [l["kind"] for l in bill["lines"]]
    assert "proration" in kinds and "overage" in kinds

    # 余额不足 -> 失败，可重试
    r = c.post(f"/bills/{bill['id']}/charge")
    assert r.status_code == 200
    assert r.json()["result"] == "insufficient_funds"
    assert r.json()["failure_reason"] == "insufficient_funds"

    # 充值后推进到退避点，重试成功
    c.post("/accounts", json={"customer_id": "cust-api",
                              "balance_cents": 1_000_000, "card_active": True})
    clock.advance(seconds=3601)
    r = c.post(f"/bills/{bill['id']}/retries", params={"base_interval_sec": 3600})
    assert r.status_code == 200
    assert r.json()["bill"]["status"] == "paid"
    assert r.json()["subscription_state"] == "active"

    # 对账平衡
    r = c.get(f"/bills/{bill['id']}/reconcile")
    assert r.json()["balanced"] is True

    # 日志包含关键判定依据
    msgs = [r.getMessage() for r in caplog.records]
    assert any("charge FAIL" in m for m in msgs)
    assert any("charge SUCCESS" in m for m in msgs)


def test_late_usage_via_api_gets_409_then_adjustment(env):
    c, t0 = env["client"], env["t0"]
    sub = _setup_sub(c, customer="cust-late", balance=0)
    sid = sub["id"]
    c.post(f"/subscriptions/{sid}/usage", json={
        "event_id": "e1", "quantity": 10,
        "occurred_at": (t0 + timedelta(days=1)).isoformat(), "idempotency_key": "k1"})
    bill = c.post(f"/subscriptions/{sid}/bills/finalize").json()
    frozen = bill["subtotal_cents"]

    # 迟到事件 -> 409 period_closed
    r = c.post(f"/subscriptions/{sid}/usage", json={
        "event_id": "e-late", "quantity": 5,
        "occurred_at": (t0 + timedelta(days=20)).isoformat(),
        "idempotency_key": "k-late"})
    assert r.status_code == 409
    assert r.json()["error"] == "period_closed"

    # 调整单
    r = c.post(f"/bills/{bill['id']}/adjustments")
    assert r.json()["created"] is True
    adj = r.json()["bill"]
    assert adj["is_adjustment"] is True
    assert adj["subtotal_cents"] == 5 * 5
    # 主账单金额不变
    assert c.get(f"/bills/{bill['id']}").json()["subtotal_cents"] == frozen


def test_cancel_end_of_period_and_advance(env):
    c, clock = env["client"], env["clock"]  # noqa: F841
    sub = _setup_sub(c, customer="cust-eop", balance=0)
    sid = sub["id"]
    r = c.post(f"/subscriptions/{sid}/cancel", json={
        "effective": "end_of_period", "idempotency_key": "cancel-1"})
    assert r.status_code == 200
    assert r.json()["action"] == "cancel"

    # 推进账期：当前账期出账后收敛为 canceled，不再开新账期
    clock.advance(days=40)
    r = c.post(f"/subscriptions/{sid}/advance")
    assert r.status_code == 200
    assert c.get(f"/subscriptions/{sid}").json()["state"] == "canceled"


def test_idempotent_subscription_creation(env):
    c = env["client"]
    payload = {"customer_id": "cust-idem", "plan_code": "basic",
               "anchor_day": 1, "idempotency_key": "same-key"}
    r1 = c.post("/subscriptions", json=payload)
    r2 = c.post("/subscriptions", json=payload)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["subscription"]["id"] == r2.json()["subscription"]["id"]
