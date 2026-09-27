"""资金流水记账与对账。

每次扣款尝试都产生一条流水：成功记正收入，失败记 0 元失败流水（保留
审计轨迹）。对账 = 账单应付金额与成功流水之和比对。
"""

import logging

from app.models import (
    BillStatus,
    ChargeAttempt,
    ChargeResult,
    LedgerEntry,
    LedgerEntryType,
)
from app.store import Store

logger = logging.getLogger("billing.ledger")


def record_attempt(store: Store, attempt: ChargeAttempt) -> LedgerEntry:
    """根据扣款尝试写流水；同一 attempt 幂等（调用方需持有 store.lock）。"""
    for entry in store.ledger:
        if entry.charge_attempt_id == attempt.id:
            return entry
    success = attempt.result == ChargeResult.SUCCESS
    entry = LedgerEntry(
        id=store.new_id("led"),
        bill_id=attempt.bill_id,
        subscription_id=attempt.subscription_id,
        entry_type=LedgerEntryType.CHARGE_SUCCESS if success else LedgerEntryType.CHARGE_FAILURE,
        amount_cents=attempt.amount_cents if success else 0,
        currency="CNY",
        created_at=store.now(),
        charge_attempt_id=attempt.id,
        note=attempt.reason_detail,
    )
    store.ledger.append(entry)
    logger.info("ledger %s bill=%s attempt=%d cents=%d",
                entry.entry_type.value, attempt.bill_id, attempt.attempt_no,
                entry.amount_cents)
    return entry


def reconcile(store: Store, bill_id: str) -> dict:
    with store.lock:
        bill = store.get_bill(bill_id)
        paid_cents = sum(
            e.amount_cents for e in store.ledger
            if e.bill_id == bill_id and e.entry_type == LedgerEntryType.CHARGE_SUCCESS
        )
        expected = bill.subtotal_cents if bill.status == BillStatus.PAID else 0
        result = {
            "bill_id": bill_id,
            "status": bill.status.value,
            "expected_cents": expected,
            "paid_cents": paid_cents,
            "balanced": paid_cents == expected,
            "entries": [
                {
                    "id": e.id,
                    "type": e.entry_type.value,
                    "amount_cents": e.amount_cents,
                    "attempt_id": e.charge_attempt_id,
                }
                for e in store.ledger if e.bill_id == bill_id
            ],
        }
        logger.info("reconcile bill=%s expected=%d paid=%d balanced=%s",
                    bill_id, expected, paid_cents, result["balanced"])
        return result
