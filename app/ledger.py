"""资金流水记账与对账。

资金流水类型：
- charge_success / charge_failure：自动扣款尝试（成功记正收入，失败记 0 保留审计轨迹）；
- payment：分次付款实收；
- refund：退款（记负数，资金流出到账户抵扣额度）；
- credit_apply：账户抵扣额度冲抵账单（记正，抵减应收）。

对账 = 账单应付金额与（实收 + 抵扣）比对；退款只影响资金净流出与账户
抵扣额度，不改写已出账账单的金额与明细。
"""

import logging

from app.models import (
    BillStatus,
    ChargeAttempt,
    ChargeResult,
    CreditApplication,
    LedgerEntry,
    LedgerEntryType,
    Payment,
    Refund,
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


def record_payment(store: Store, payment: Payment) -> LedgerEntry:
    """分次付款实收流水；同一 payment 幂等（调用方需持有 store.lock）。"""
    for entry in store.ledger:
        if entry.ref_id == payment.id and entry.entry_type == LedgerEntryType.PAYMENT:
            return entry
    entry = LedgerEntry(
        id=store.new_id("led"),
        bill_id=payment.bill_id,
        subscription_id=payment.subscription_id,
        entry_type=LedgerEntryType.PAYMENT,
        amount_cents=payment.amount_cents,
        currency="CNY",
        created_at=store.now(),
        charge_attempt_id="",
        ref_id=payment.id,
        note=f"manual payment {payment.id}",
    )
    store.ledger.append(entry)
    logger.info("ledger payment bill=%s payment=%s cents=%d",
                payment.bill_id, payment.id, payment.amount_cents)
    return entry


def record_refund(store: Store, refund: Refund) -> LedgerEntry:
    """退款流水（负数，资金流出）；同一 refund 幂等。"""
    for entry in store.ledger:
        if entry.ref_id == refund.id and entry.entry_type == LedgerEntryType.REFUND:
            return entry
    entry = LedgerEntry(
        id=store.new_id("led"),
        bill_id=refund.bill_id,
        subscription_id=refund.subscription_id,
        entry_type=LedgerEntryType.REFUND,
        amount_cents=-refund.amount_cents,
        currency="CNY",
        created_at=store.now(),
        charge_attempt_id="",
        ref_id=refund.id,
        note=f"refund of payment {refund.payment_id}: {refund.reason}",
    )
    store.ledger.append(entry)
    logger.info("ledger refund bill=%s refund=%s payment=%s cents=-%d",
                refund.bill_id, refund.id, refund.payment_id, refund.amount_cents)
    return entry


def record_credit_apply(store: Store, app_record: CreditApplication) -> LedgerEntry:
    """抵扣冲抵流水；同一抵扣记录幂等。"""
    for entry in store.ledger:
        if entry.ref_id == app_record.id and entry.entry_type == LedgerEntryType.CREDIT_APPLY:
            return entry
    entry = LedgerEntry(
        id=store.new_id("led"),
        bill_id=app_record.bill_id,
        subscription_id=app_record.subscription_id,
        entry_type=LedgerEntryType.CREDIT_APPLY,
        amount_cents=app_record.amount_cents,
        currency="CNY",
        created_at=store.now(),
        charge_attempt_id="",
        ref_id=app_record.id,
        note=f"credit applied from account {app_record.customer_id}",
    )
    store.ledger.append(entry)
    logger.info("ledger credit_apply bill=%s credit=%s cents=%d",
                app_record.bill_id, app_record.id, app_record.amount_cents)
    return entry


def reconcile(store: Store, bill_id: str) -> dict:
    with store.lock:
        bill = store.get_bill(bill_id)
        entries = [e for e in store.ledger if e.bill_id == bill_id]
        charged = sum(
            e.amount_cents for e in entries
            if e.entry_type == LedgerEntryType.CHARGE_SUCCESS
        )
        paid = sum(
            e.amount_cents for e in entries
            if e.entry_type == LedgerEntryType.PAYMENT
        )
        credited = sum(
            e.amount_cents for e in entries
            if e.entry_type == LedgerEntryType.CREDIT_APPLY
        )
        refunded = -sum(
            e.amount_cents for e in entries
            if e.entry_type == LedgerEntryType.REFUND
        )
        received = charged + paid                 # 实收合计（毛额，含已退）
        settled = received + credited             # 已结清 = 实收 + 抵扣
        expected = bill.subtotal_cents if bill.status == BillStatus.PAID else 0
        if bill.status == BillStatus.PAID:
            balanced = settled == bill.subtotal_cents and refunded <= received
        else:
            balanced = settled <= bill.subtotal_cents and refunded <= received
        result = {
            "bill_id": bill_id,
            "status": bill.status.value,
            "expected_cents": expected,
            "paid_cents": received,
            "charged_cents": charged,
            "manual_paid_cents": paid,
            "credit_applied_cents": credited,
            "refunded_cents": refunded,
            "net_received_cents": received - refunded,
            "remaining_cents": bill.remaining_cents,
            "balanced": balanced,
            "entries": [
                {
                    "id": e.id,
                    "type": e.entry_type.value,
                    "amount_cents": e.amount_cents,
                    "attempt_id": e.charge_attempt_id,
                    "ref_id": e.ref_id,
                }
                for e in entries
            ],
        }
        logger.info("reconcile bill=%s expected=%d settled=%d refunded=%d balanced=%s",
                    bill_id, expected, settled, refunded, result["balanced"])
        return result
