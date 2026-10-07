"""分次付款、退款、账户抵扣额度的端到端场景测试。

覆盖：
- 一张已出账账单分多笔付到付清，差额可查，付清后唯一收敛 PAID；
- 同一张账单并发多笔付款合计不多收，后来的按已结清处理；
- 同一付款幂等键重复/并发提交不重复扣钱；
- 退款不超过实际收到的钱，历史账单金额/明细不被改动，退款可解释来源；
- 退款转为账户抵扣额度，后续账单先抵额度再走支付方式，抵扣不重复、
  按顺序生效、账目对得上；
- 抵扣后网关失败会回滚额度，不丢钱、不重复。
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from app import billing, installments, metering, payments, refunds, subscriptions
from app.errors import ConflictError, PeriodClosedError
from app.ledger import reconcile, reconcile_account
from app.models import BillStatus, ChargeResult

logger = logging.getLogger("test.partial_refund_credit")


def _record_late_usage(s, sub, parent_bill_id, event_id, units, key):
    """关账后到达的用量：先持久化（period_closed），再生成调整单。"""
    try:
        metering.record_usage(
            s, sub.id, event_id, units,
            datetime(2026, 2, 10, tzinfo=UTC), key)
    except PeriodClosedError:
        pass  # 事件已持久化，符合既有迟到用量语义
    return billing.create_adjustment_for_late_usage(s, parent_bill_id)


def _finalized_bill(env, customer="c1", balance=1_000_000, price_cents=1000):
    s = env["store"]
    sub, _ = subscriptions.create_subscription(
        s, customer, "basic", anchor_day=1, idempotency_key=f"sub-{customer}")
    payments.ensure_account(s, customer, balance_cents=balance)
    bill = billing.ensure_open_bill(s, sub.id)
    billing.finalize_bill(s, bill.id)
    assert bill.subtotal_cents == price_cents
    return s, sub, bill


def test_split_payment_can_pay_off_in_installments(env, caplog):
    """分次付款：一笔一笔付直到付清，未付差额随时可查，付清唯一收敛 PAID。"""
    caplog.set_level(logging.INFO)
    s, _sub, bill = _finalized_bill(env, balance=1_000_000)

    p1 = installments.pay_bill(s, bill.id, 600, "pk-1")
    assert p1.status == "success" and p1.amount_cents == 600
    b = s.get_bill(bill.id)
    assert b.status == BillStatus.FINALIZED and b.paid_cents == 600
    assert installments.amount_due(s, b) == 400
    logger.info("场景[分次付款] 首笔 600 后 status=%s 已付=%d 差额=%d",
                b.status.value, b.paid_cents, installments.amount_due(s, b))

    p2 = installments.pay_bill(s, bill.id, 300, "pk-2")
    assert p2.status == "success"
    assert installments.amount_due(s, s.get_bill(bill.id)) == 100

    # 最后一笔付超过差额也只补差额，不许多收
    p3 = installments.pay_bill(s, bill.id, 999, "pk-3")
    b = s.get_bill(bill.id)
    assert p3.amount_cents == 100
    assert b.status == BillStatus.PAID and b.paid_cents == bill.subtotal_cents
    assert installments.amount_due(s, b) == 0
    logger.info("场景[分次付款] 尾笔多付被截断为 100，账单唯一收敛 %s",
                b.status.value)

    rec = reconcile(s, bill.id)
    assert rec["balanced"] and rec["due_cents"] == 0
    # 资金安全：网关现金收入合计恰好等于应付
    assert rec["cash_cents"] == bill.subtotal_cents


def test_concurrent_payments_never_overcollect(env, caplog):
    """并发付款：12 路同时为一张账单付款，合计实收不超过应付且只收敛一次 PAID。"""
    caplog.set_level(logging.INFO)
    s, _sub, bill = _finalized_bill(env, balance=10_000_000)
    results = []
    barrier = threading.Barrier(12)

    def worker(i):
        barrier.wait()
        results.append(installments.pay_bill(s, bill.id, 300, f"cp-{i}"))

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(worker, range(12)))

    b = s.get_bill(bill.id)
    success = [p for p in results if p.status == "success"]
    settled = [p for p in results if p.status == "already_settled"]
    collected = sum(p.amount_cents for p in success)
    logger.info("场景[并发付款] 成功笔数=%d 已结清短路=%d 合计实收=%d 应付=%d",
                len(success), len(settled), collected, b.subtotal_cents)

    assert b.status == BillStatus.PAID
    assert collected == b.subtotal_cents          # 不多不少
    assert all(p.amount_cents == 0 for p in settled)
    # 账户余额扣减恰好等于应付（硬证据：网关层面没多扣）
    assert s.accounts["c1"].balance_cents == 10_000_000 - b.subtotal_cents
    assert reconcile(s, bill.id)["balanced"]


def test_duplicate_payment_idempotency_key_charges_once(env, caplog):
    """同一付款幂等键重复与并发提交：只产生一次真实扣款。"""
    caplog.set_level(logging.INFO)
    s, _sub, bill = _finalized_bill(env, balance=1_000_000)

    once = installments.pay_bill(s, bill.id, 400, "same-key")
    replay_seq = [installments.pay_bill(s, bill.id, 400, "same-key")
                  for _ in range(5)]

    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        return installments.pay_bill(s, bill.id, 400, "same-key")

    with ThreadPoolExecutor(max_workers=8) as pool:
        replay_conc = list(pool.map(lambda _: worker(), range(8)))

    all_replay = replay_seq + replay_conc
    assert all(p.id == once.id for p in all_replay)
    assert s.accounts["c1"].balance_cents == 1_000_000 - 400
    logger.info("场景[付款防重] 1 次顺序 + 5 次重复 + 8 次并发同键，只扣一次 400")

    # 付清后的新请求走已结清短路，不扣款
    installments.pay_bill(s, bill.id, 600, "close-key")
    b = s.get_bill(bill.id)
    bal = s.accounts["c1"].balance_cents
    tail = installments.pay_bill(s, bill.id, 600, "late-key")
    assert tail.status == "already_settled" and tail.amount_cents == 0
    assert s.accounts["c1"].balance_cents == bal
    assert b.status == BillStatus.PAID


def test_refund_cannot_exceed_received_and_history_is_immutable(env, caplog):
    """退款：累计不超过实收；历史账单金额/明细不变；退款记录解释退的是哪笔。"""
    caplog.set_level(logging.INFO)
    s, _sub, bill = _finalized_bill(env, balance=1_000_000)
    installments.pay_bill(s, bill.id, 400, "p-a")
    installments.pay_bill(s, bill.id, 600, "p-b")

    frozen_subtotal = bill.subtotal_cents
    frozen_lines = [(l.kind, l.amount_cents, l.description) for l in bill.lines]

    r1 = refunds.refund_bill(s, bill.id, 300, "rf-1", reason="部分退款")
    assert r1.amount_cents == 300
    # 全部分摊到第一笔成功付款（attempt_no 升序）
    assert len(r1.allocations) == 1
    assert r1.allocations[0].amount_cents == 300
    logger.info("场景[退款来源] 退款 300 分摊到尝试 %s",
                r1.allocations[0].charge_attempt_id)

    r2 = refunds.refund_bill(s, bill.id, 500, "rf-2")
    assert refunds.refundable_cents(s, bill.id) == 200

    # 再退超过可退 -> 拒绝
    with pytest.raises(ConflictError) as exc_info:
        refunds.refund_bill(s, bill.id, 201, "rf-3")
    logger.info("场景[退款上限] 超过可退 200 的请求被拒绝：%s", exc_info.value)

    # 历史账单金额/明细完全不变，状态不回退
    b = s.get_bill(bill.id)
    assert b.subtotal_cents == frozen_subtotal
    assert [(l.kind, l.amount_cents, l.description) for l in b.lines] == frozen_lines
    assert b.status == BillStatus.PAID and b.refunded_cents == 800
    assert reconcile(s, bill.id)["balanced"]
    # 退款进入账户抵扣额度
    assert s.accounts["c1"].credit_cents == 800
    assert reconcile_account(s, "c1")["balanced"]

    # 同一退款键重复提交 -> 回放，不重复退款
    replay = refunds.refund_bill(s, bill.id, 500, "rf-2")
    assert replay.id == r2.id and s.accounts["c1"].credit_cents == 800


def test_refunded_credit_is_applied_to_later_bills_in_order(env, caplog):
    """抵扣按顺序生效、不重复：后续账单先抵额度，不足再走支付方式。"""
    caplog.set_level(logging.INFO)
    s, sub, bill = _finalized_bill(env, balance=1_000_000)
    installments.pay_bill(s, bill.id, 1000, "pay-full")
    refunds.refund_bill(s, bill.id, 300, "rf")
    assert s.accounts["c1"].credit_cents == 300

    def late_adjustment(event_id, units, key):
        # 关账后到达的用量 -> 独立调整单（新的正数账单），历史主单不动
        return _record_late_usage(s, sub, bill.id, event_id, units, key)

    # 第一张后续账单 50 分：全部用额度
    adj1 = late_adjustment("late-1", 10, "u-late-1")
    assert adj1.subtotal_cents == 50
    a1 = payments.charge_bill(s, adj1.id)
    assert a1.result == ChargeResult.SUCCESS and a1.amount_cents == 0
    rec1 = reconcile(s, adj1.id)
    assert rec1["credit_cents"] == 50 and rec1["cash_cents"] == 0
    assert s.accounts["c1"].credit_cents == 250
    logger.info("场景[抵扣顺序] 后续单 50 全部由额度冲抵，额度余 250")

    # 第二张后续账单 100 分：额度 250 仍足够 -> 全额度
    adj2 = late_adjustment("late-2", 20, "u-late-2")
    a2 = payments.charge_bill(s, adj2.id)
    assert a2.amount_cents == 0
    assert reconcile(s, adj2.id)["credit_cents"] == 100
    assert s.accounts["c1"].credit_cents == 150

    # 第三张后续账单 500 分：额度 150 + 现金 350
    adj3 = late_adjustment("late-3", 100, "u-late-3")
    assert adj3.subtotal_cents == 500
    a3 = payments.charge_bill(s, adj3.id)
    assert a3.amount_cents == 350
    rec3 = reconcile(s, adj3.id)
    assert rec3["credit_cents"] == 150 and rec3["cash_cents"] == 350
    assert rec3["paid_cents"] == 500 and rec3["balanced"]
    assert s.get_bill(adj3.id).status == BillStatus.PAID
    assert s.accounts["c1"].credit_cents == 0
    logger.info("场景[抵扣顺序] 后续单 500 = 额度 150 + 现金 350，额度用尽")

    # 抵扣记录每笔独立、不重复
    active_credits = [c for c in s.credit_applications.values() if not c.reversed]
    assert sorted(c.amount_cents for c in active_credits) == [50, 100, 150]
    assert reconcile_account(s, "c1")["balanced"]
    # 主账单与后续账单各自对账都平衡
    assert reconcile(s, bill.id)["balanced"]


def test_credit_is_released_when_gateway_charge_fails(env, caplog):
    """先抵额度后网关失败：抵扣整体回滚，额度不丢，重试成功后账目一致。"""
    caplog.set_level(logging.INFO)
    s, clock = env["store"], env["clock"]
    s, sub, bill = _finalized_bill(env, balance=1_000_000)
    installments.pay_bill(s, bill.id, 1000, "pay-full")
    refunds.refund_bill(s, bill.id, 300, "rf")
    assert s.accounts["c1"].credit_cents == 300

    # 新账单 500：额度 300 先冲抵，但账户余额被掏空 -> 网关不足，可重试
    s.accounts["c1"].balance_cents = 0
    adj = _record_late_usage(s, sub, bill.id, "late-x", 100, "u-late-x")
    assert adj.subtotal_cents == 500

    att = payments.charge_bill(s, adj.id)
    assert att.result == ChargeResult.INSUFFICIENT_FUNDS
    # 抵扣被回滚：额度退回、账单实收为 0、余额未被网关扣
    assert s.accounts["c1"].credit_cents == 300
    assert s.get_bill(adj.id).paid_cents == 0
    assert reconcile(s, adj.id)["balanced"]
    assert reconcile_account(s, "c1")["balanced"]
    logger.info("场景[抵扣回滚] 网关失败后额度回滚为 300，账单实收 0")

    # 充值并过退避点重试：这次额度 300 + 现金 200 成功
    s.accounts["c1"].balance_cents = 1_000_000
    clock.advance(seconds=3601)
    from app import grace
    grace.run_retries(s, adj.id, base_interval_sec=3600)
    rec = reconcile(s, adj.id)
    assert s.get_bill(adj.id).status == BillStatus.PAID
    assert rec["credit_cents"] == 300 and rec["cash_cents"] == 200
    assert rec["balanced"] and reconcile_account(s, "c1")["balanced"]
    logger.info("场景[抵扣回滚] 重试成功：额度 300 + 现金 200，账目全平衡")


def test_credit_full_cover_marks_bill_paid_without_gateway(env, caplog):
    """额度足够覆盖整张账单：不触网关也能付清，流水仍可对账。"""
    caplog.set_level(logging.INFO)
    s, sub, bill = _finalized_bill(env, balance=1_000_000)
    installments.pay_bill(s, bill.id, 1000, "pay-full")
    refunds.refund_bill(s, bill.id, 1000, "rf-full")
    assert s.accounts["c1"].credit_cents == 1000

    adj = _record_late_usage(s, sub, bill.id, "late-full", 100, "u-late-full")
    bal_before = s.accounts["c1"].balance_cents
    att = payments.charge_bill(s, adj.id)
    assert att.result == ChargeResult.SUCCESS and att.amount_cents == 0
    assert s.get_bill(adj.id).status == BillStatus.PAID
    assert s.accounts["c1"].balance_cents == bal_before       # 网关未扣款
    assert s.accounts["c1"].credit_cents == 500
    rec = reconcile(s, adj.id)
    assert rec["credit_cents"] == 500 and rec["cash_cents"] == 0
    assert rec["balanced"]
    logger.info("场景[全额抵扣] 500 分账单全由额度付清，网关零扣款，额度余 500")
