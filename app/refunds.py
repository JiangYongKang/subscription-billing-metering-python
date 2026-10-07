"""账单退款：全额 / 部分退款，退款转为账户抵扣额度。

硬约束：
- 退款**不改动**已出账历史账单的金额与明细，也不触发冻结账目重算
  （Bill.subtotal_cents / lines 完全不动）。
- 累计退款不超过账单当前**净实收**：
      net_paid = 网关成功扣款(charge_success + payment)
               + 生效额度抵扣(credit_applied - credit_released)
      refundable = net_paid - 已退(refund)
- 每笔退款按成功付款尝试（自动扣款/分次付款）依次（attempt_no 升序）分摊，
  生成 RefundAllocation，能解释"退的是哪一笔"。
- 退款默认留作该客户账户上的抵扣额度（credit_cents += amount），并写 refund
  流水，供后续账单冲抵；账单状态不回退（PAID 不退回未付）。
"""

import logging

from app.errors import ConflictError, StateError
from app.ledger import record_refund, refresh_bill_totals
from app.models import (
    BillStatus,
    ChargeAttempt,
    ChargeResult,
    LedgerEntryType,
    Refund,
    RefundAllocation,
)
from app.store import Store

logger = logging.getLogger("billing.refunds")


def refundable_cents(store: Store, bill_id: str) -> int:
    """账单当前还能退多少钱（净实收 - 已退，不小于 0）。"""
    bill = store.get_bill(bill_id)
    net = bill.paid_cents - bill.refunded_cents
    return max(0, net)


def _successful_attempts(store: Store, bill_id: str) -> list[ChargeAttempt]:
    return sorted(
        (
            a for a in store.attempts.values()
            if a.bill_id == bill_id
            and a.result == ChargeResult.SUCCESS
            and not a.reversed
        ),
        key=lambda a: a.attempt_no,
    )


def _attempt_cash(store: Store, attempt: ChargeAttempt) -> int:
    """该成功尝试的网关现金金额（charge_success 或 payment 流水）。"""
    return sum(
        e.amount_cents for e in store.ledger
        if e.charge_attempt_id == attempt.id
        and e.entry_type in (
            LedgerEntryType.CHARGE_SUCCESS, LedgerEntryType.PAYMENT,
        )
    )


def _attempt_refunded(store: Store, attempt_id: str) -> int:
    total = 0
    for r in store.refunds.values():
        for alloc in r.allocations:
            if alloc.charge_attempt_id == attempt_id:
                total += alloc.amount_cents
    return total


def _credit_refundable(store: Store, bill_id: str) -> int:
    """账单生效额度抵扣中尚未被退走的部分（也可作为退款来源）。"""
    credited = sum(
        e.amount_cents for e in store.ledger
        if e.bill_id == bill_id and e.entry_type == LedgerEntryType.CREDIT_APPLIED
    )
    released = sum(
        e.amount_cents for e in store.ledger
        if e.bill_id == bill_id and e.entry_type == LedgerEntryType.CREDIT_RELEASED
    )
    return max(0, credited - released)


def refund_bill(
    store: Store,
    bill_id: str,
    amount_cents: int,
    idempotency_key: str,
    *,
    reason: str = "",
) -> Refund:
    if amount_cents <= 0:
        raise ConflictError("refund amount_cents must be positive")
    if not idempotency_key:
        raise ConflictError("idempotency_key is required for refunds")

    with store.lock:
        bill = store.get_bill(bill_id)
        idem_full = f"ref:{idempotency_key}"

        replay_id = store.refunds_by_key.get(idem_full)
        if replay_id:
            replay = store.refunds[replay_id]
            if replay.bill_id != bill.id:
                raise ConflictError(
                    f"idempotency_key {idempotency_key} already used on another bill"
                )
            logger.info("refund replay bill=%s key=%s", bill.id, idempotency_key)
            return replay

        if bill.status not in (BillStatus.FINALIZED, BillStatus.PAID):
            raise StateError(
                f"bill {bill.id} is {bill.status.value}; no received funds to refund"
            )

        refresh_bill_totals(store, bill)
        available = refundable_cents(store, bill.id)
        if amount_cents > available:
            raise ConflictError(
                f"refund {amount_cents} exceeds refundable {available} "
                f"on bill {bill.id}"
            )

        # 按成功付款尝试依次分摊（现金部分），解释退的是哪一笔
        remaining = amount_cents
        allocations: list[RefundAllocation] = []
        for attempt in _successful_attempts(store, bill.id):
            if remaining <= 0:
                break
            cash = _attempt_cash(store, attempt)
            left_on_attempt = cash - _attempt_refunded(store, attempt.id)
            if left_on_attempt <= 0:
                continue
            take = min(remaining, left_on_attempt)
            allocations.append(RefundAllocation(
                charge_attempt_id=attempt.id,
                payment_id=attempt.payment_id,
                amount_cents=take,
            ))
            remaining -= take

        # 现金成功尝试不足的部分来自生效额度抵扣（无需指定尝试，记录在 note 口径）
        from_credit = remaining
        if from_credit > 0:
            if from_credit > _credit_refundable(store, bill.id):
                # 防御性：理论上 amount<=net_paid 时不会发生
                raise ConflictError("refund allocation failed: credit source short")
            logger.info("refund bill=%s from credit pool cents=%d",
                        bill.id, from_credit)

        refund = Refund(
            id=store.new_id("rfd"),
            bill_id=bill.id,
            subscription_id=bill.subscription_id,
            customer_id=bill.customer_id,
            amount_cents=amount_cents,
            idempotency_key=idempotency_key,
            created_at=store.now(),
            reason=reason,
            allocations=allocations,
        )
        if from_credit > 0:
            refund.note = f"其中 {from_credit} 分来自账户抵扣额度冲抵部分"
        store.refunds[refund.id] = refund
        store.refunds_by_key[idem_full] = refund.id

        # 退款转为账户抵扣额度（负债），并写 refund 流水
        acct = store.accounts.get(bill.customer_id)
        if acct is None:
            from app.payments import ensure_account

            acct = ensure_account(store, bill.customer_id, 0, card_active=True)
        acct.credit_cents += amount_cents
        entry = record_refund(store, refund)
        refund.ledger_entry_id = entry.id
        refresh_bill_totals(store, bill)

        # 历史账单金额/明细不动，账单状态不回退
        logger.info(
            "refund bill=%s cents=%d allocs=%d from_credit=%d -> credit_balance=%d "
            "bill_status_unchanged=%s",
            bill.id, amount_cents, len(allocations), from_credit,
            acct.credit_cents, bill.status.value,
        )
        return refund
