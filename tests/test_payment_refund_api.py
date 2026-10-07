"""HTTP 端到端：分次付款、退款转额度、额度抵扣后续账单。"""

import logging


def _setup(client, customer="cust-pr", balance=1_000_000):
    client.post("/accounts", json={
        "customer_id": customer, "balance_cents": balance,
        "card_active": True})
    r = client.post("/subscriptions", json={
        "customer_id": customer, "plan_code": "basic", "anchor_day": 1,
        "idempotency_key": f"sub-{customer}"})
    return r.json()["subscription"]


def _open_bill(env, client, sub):
    from app import billing
    return billing.ensure_open_bill(env["store"], sub["id"])


def test_pay_refund_credit_flow_over_http(env, caplog):
    """验：分次付满 -> 重复付款被幂等短路 -> 退款 -> 额度对账。"""
    caplog.set_level(logging.INFO)
    c = env["client"]
    logger = logging.getLogger("test.http")
    sub = _setup(c)
    bid = c.post(f"/subscriptions/{sub['id']}/bills/finalize").json()["id"]

    # 首笔 600
    r = c.post(f"/bills/{bid}/payments", json={"amount_cents": 600,
                                               "idempotency_key": "http-p1"})
    assert r.status_code == 200
    body = r.json()
    assert body["amount_cents"] == 600 and body["bill_due_cents"] == 400
    logger.info("场景[HTTP 分次] 首笔后差额=%s", body["bill_due_cents"])

    # 同键重复 -> 同一笔，不重复扣
    r2 = c.post(f"/bills/{bid}/payments", json={"amount_cents": 600,
                                                "idempotency_key": "http-p1"})
    assert r2.json()["id"] == body["id"]

    # 尾笔付清（多付被截断到 400）
    r3 = c.post(f"/bills/{bid}/payments", json={"amount_cents": 999,
                                                "idempotency_key": "http-p2"})
    assert r3.json()["amount_cents"] == 400
    assert r3.json()["bill_status"] == "paid"

    # 已结清再来一笔 -> 200 但不再扣款
    r4 = c.post(f"/bills/{bid}/payments", json={"amount_cents": 200,
                                                "idempotency_key": "http-p3"})
    assert r4.json()["requested_status"] == "already_settled"
    assert r4.json()["amount_cents"] == 0

    # 部分退款 300
    r5 = c.post(f"/bills/{bid}/refunds", json={"amount_cents": 300,
                                               "idempotency_key": "http-r1",
                                               "reason": "http 退款"})
    assert r5.status_code == 200
    rb = r5.json()
    assert rb["amount_cents"] == 300 and rb["bill_status"] == "paid"
    assert rb["account_credit_cents"] == 300
    assert rb["allocations"][0]["amount_cents"] == 300
    logger.info("场景[HTTP 退款] 退款 300 入额度，账单仍 paid")

    # 超额退款 409
    r6 = c.post(f"/bills/{bid}/refunds", json={"amount_cents": 100000,
                                               "idempotency_key": "http-r2"})
    assert r6.status_code == 409
    assert r6.json()["error"] == "conflict"

    # 账单与账户额度对账
    r7 = c.get(f"/bills/{bid}/reconcile")
    assert r7.json()["balanced"] is True
    assert r7.json()["refunded_cents"] == 300
    r8 = c.get("/accounts/cust-pr/credit")
    assert r8.json()["balanced"] is True
    assert r8.json()["credit_cents"] == 300
    logger.info("场景[HTTP 对账] 账单与账户额度均平衡")


def test_payment_requires_finalized_bill(env):
    """验：OPEN（未出账）账单不能付款，返回 409 状态约束。"""
    c = env["client"]
    sub = _setup(c, customer="cust-open")
    open_bill = _open_bill(env, c, sub)
    r = c.post(f"/bills/{open_bill.id}/payments",
               json={"amount_cents": 100, "idempotency_key": "open-pay"})
    assert r.status_code == 409
    assert r.json()["error"] == "invalid_state"
