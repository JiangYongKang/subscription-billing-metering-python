"""HTTP 端到端：分次付款 -> 退款 -> 抵扣额度 -> 下一账单冲抵，全链路对账。"""

import logging


def _make_finalized_bill(client, customer="c1", key_suffix=""):
    client.post("/plans", json={
        "code": "basic", "name": "Basic", "base_price_cents": 1000,
        "included_units": 100, "overage_unit_price_cents": 5})
    client.post("/accounts", json={"customer_id": customer, "balance_cents": 1_000_000})
    r = client.post("/subscriptions", json={
        "customer_id": customer, "plan_code": "basic",
        "anchor_day": 1, "idempotency_key": f"sub-{customer}{key_suffix}"})
    sid = r.json()["subscription"]["id"]
    r = client.post(f"/subscriptions/{sid}/bills/finalize")
    return sid, r.json()


def test_partial_pay_refund_credit_end_to_end(env, caplog):
    """场景：分两次付满 -> 退一部分 -> 额度进账户 -> 下一账单自动冲抵。"""
    caplog.set_level(logging.INFO)
    c = env["client"]
    sid, bill = _make_finalized_bill(c)
    total = bill["subtotal_cents"]

    # 分两次付款：第一次后可见剩余，第二次付满
    r = c.post(f"/bills/{bill['id']}/payments",
               json={"amount_cents": 400, "idempotency_key": "p1"})
    assert r.status_code == 200
    assert r.json()["remaining_cents"] == total - 400
    r = c.post(f"/bills/{bill['id']}/payments",
               json={"amount_cents": total - 400, "idempotency_key": "p2"})
    assert r.json()["bill_status"] == "paid"

    # 重复提交第二笔：幂等，不重复扣
    r = c.post(f"/bills/{bill['id']}/payments",
               json={"amount_cents": total - 400, "idempotency_key": "p2"})
    assert r.json()["duplicate"] is True
    # 付满后再付：按已结清处理
    r = c.post(f"/bills/{bill['id']}/payments",
               json={"amount_cents": 1, "idempotency_key": "p3"})
    assert r.json()["already_settled"] is True

    # 退第一笔的一部分
    payments = c.get(f"/bills/{bill['id']}/payments").json()
    p1 = next(p for p in payments if p["idempotency_key"] == "p1")
    r = c.post(f"/payments/{p1['id']}/refunds",
               json={"amount_cents": 150, "reason": "部分不满意",
                     "idempotency_key": "rf-1"})
    assert r.status_code == 201
    assert r.json()["refund"]["payment_id"] == p1["id"]
    # 超额退款被拒
    r = c.post(f"/payments/{p1['id']}/refunds",
               json={"amount_cents": 251, "idempotency_key": "rf-2"})
    assert r.status_code == 409
    # 账户抵扣额度 = 150
    acct = c.get("/accounts/c1").json()
    assert acct["credit_balance_cents"] == 150

    # 历史账单金额与状态不变
    b = c.get(f"/bills/{bill['id']}").json()
    assert b["status"] == "paid" and b["subtotal_cents"] == total
    rec = c.get(f"/bills/{bill['id']}/reconcile").json()
    assert rec["balanced"] and rec["refunded_cents"] == 150
    assert rec["net_received_cents"] == total - 150

    # 下一账单出账自动抵扣 150，剩余走分次付款
    sid2, bill2 = _make_finalized_bill(c, customer="c1", key_suffix="-2")
    bill2 = c.get(f"/bills/{bill2['id']}").json()
    assert bill2["credit_applied_cents"] == 150
    assert bill2["remaining_cents"] == bill2["subtotal_cents"] - 150
    credits = c.get(f"/bills/{bill2['id']}/credits").json()
    assert len(credits) == 1 and credits[0]["amount_cents"] == 150
    r = c.post(f"/bills/{bill2['id']}/payments",
               json={"amount_cents": bill2["remaining_cents"],
                     "idempotency_key": "rest"})
    assert r.json()["bill_status"] == "paid"
    assert c.get(f"/bills/{bill2['id']}/reconcile").json()["balanced"]
    assert c.get("/accounts/c1").json()["credit_balance_cents"] == 0
