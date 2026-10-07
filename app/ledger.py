"""资金流水记账与对账。

资金口径（金额均为非负整数"分"，方向由 entry_type 表达）：

- ``charge_success`` / ``payment``：网关真实扣款成功，账单的现金收入（+）。
- ``charge_failure``：扣款失败，金额 0，仅保留审计轨迹。
- ``credit_applied``：账户抵扣额度冲抵账单（账单的额度收入，+；额度负债减少）。
- ``credit_released``：伴随的网关扣款失败，本次抵扣整体回滚（额度退回）。
- ``refund``：退款（账单资金流出；默认转为客户账户抵扣额度）。

账单的实收/退款以流水为唯一事实源回算（``refresh_bill_totals``），任何代码
路径写完流水后都回算一次，保证账单字段与流水永不漂移：

    paid_cents     = charge_success + payment + credit_applied - credit_released
    refunded_cents = refund
    net_paid_cents = paid_cents - refunded_cents

账户抵扣额度对账：

    credit_cents == Σrefund(客户) - (Σcredit_applied - Σcredit_released)(客户)
"""

import logging

from app.models import (
    Bill,
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

_CASH_IN = (LedgerEntryType.CHARGE_SUCCESS, LedgerEntryType.PAYMENT)


def _append(
    store: Store,
    entry_type: LedgerEntryType,
    amount_cents: int,
    bill_id: str,
    subscription_id: str,
    customer_id: str = "",
    *,
    charge_attempt_id: str | None = None,
    payment_id: str | None = None,
    refund_id: str | None = None,
    credit_application_id: str | None = None,
    note: str = "",
) -> LedgerEntry:
    bill = store.bills.get(bill_id)
    entry = LedgerEntry(
        id=store.new_id("led"),
        bill_id=bill_id,
        subscription_id=subscription_id,
        customer_id=customer_id,
        entry_type=entry_type,
        amount_cents=amount_cents,
        currency=bill.currency if bill is not None else "CNY",
        created_at=store.now(),
        charge_attempt_id=charge_attempt_id,
        payment_id=payment_id,
        refund_id=refund_id,
        credit_application_id=credit_application_id,
        note=note,
    )
    store.ledger.append(entry)
    logger.info(
        "ledger %s bill=%s cents=%d customer=%s note=%s",
        entry.entry_type.value, bill_id, amount_cents, customer_id, note,
    )
    return entry


def record_attempt(store: Store, attempt: ChargeAttempt) -> LedgerEntry:
    """根据自动扣款尝试写流水；同一 attempt 幂等（调用方需持有 store.lock）。"""
    for entry in store.ledger:
        if entry.charge_attempt_id == attempt.id:
            return entry
    bill = store.get_bill(attempt.bill_id)
    success = attempt.result == ChargeResult.SUCCESS
    entry = _append(
        store,
        LedgerEntryType.CHARGE_SUCCESS if success else LedgerEntryType.CHARGE_FAILURE,
        attempt.amount_cents if success else 0,
        attempt.bill_id,
        attempt.subscription_id,
        bill.customer_id,
        charge_attempt_id=attempt.id,
        note=attempt.reason_detail,
    )
    return entry


def record_payment(store: Store, payment: Payment, attempt: ChargeAttempt) -> LedgerEntry:
    """分次付款网关成功的现金流水（调用方需持有 store.lock）。"""
    for entry in store.ledger:
        if entry.payment_id == payment.id:
            return entry
    return _append(
        store,
        LedgerEntryType.PAYMENT,
        payment.amount_cents,
        payment.bill_id,
        payment.subscription_id,
        payment.customer_id,
        charge_attempt_id=attempt.id,
        payment_id=payment.id,
        note=f"分次付款 {payment.id}",
    )


def record_refund(store: Store, refund: Refund) -> LedgerEntry:
    """退款流水（资金流出，转为账户抵扣额度）。"""
    for entry in store.ledger:
        if entry.refund_id == refund.id:
            return entry
    return _append(
        store,
        LedgerEntryType.REFUND,
        refund.amount_cents,
        refund.bill_id,
        refund.subscription_id,
        refund.customer_id,
        refund_id=refund.id,
        note=refund.reason or f"退款 {refund.id} -> 账户抵扣额度",
    )


def record_credit_applied(store: Store, application: CreditApplication) -> LedgerEntry:
    """额度抵扣冲抵流水（账单的额度收入，账户额度减少）。"""
    for entry in store.ledger:
        if (
            entry.credit_application_id == application.id
            and entry.entry_type == LedgerEntryType.CREDIT_APPLIED
        ):
            return entry
    return _append(
        store,
        LedgerEntryType.CREDIT_APPLIED,
        application.amount_cents,
        application.bill_id,
        application.subscription_id,
        application.customer_id,
        credit_application_id=application.id,
        charge_attempt_id=application.charge_attempt_id,
        note=f"抵扣额度冲抵 {application.source}",
    )


def record_credit_released(store: Store, application: CreditApplication) -> LedgerEntry:
    """抵扣回滚流水（额度退回账户）。"""
    for entry in store.ledger:
        if (
            entry.credit_application_id == application.id
            and entry.entry_type == LedgerEntryType.CREDIT_RELEASED
        ):
            return entry
    return _append(
        store,
        LedgerEntryType.CREDIT_RELEASED,
        application.amount_cents,
        application.bill_id,
        application.subscription_id,
        application.customer_id,
        credit_application_id=application.id,
        charge_attempt_id=application.charge_attempt_id,
        note="网关扣款失败，抵扣额度回滚",
    )


# ---- 以流水为事实源的汇总 ----------------------------------------------

def bill_cash_cents(store: Store, bill_id: str) -> int:
    return sum(
        e.amount_cents for e in store.ledger
        if e.bill_id == bill_id and e.entry_type in _CASH_IN
    )


def bill_credit_net_cents(store: Store, bill_id: str) -> int:
    applied = sum(
        e.amount_cents for e in store.ledger
        if e.bill_id == bill_id and e.entry_type == LedgerEntryType.CREDIT_APPLIED
    )
    released = sum(
        e.amount_cents for e in store.ledger
        if e.bill_id == bill_id and e.entry_type == LedgerEntryType.CREDIT_RELEASED
    )
    return applied - released


def bill_refunded_cents(store: Store, bill_id: str) -> int:
    return sum(
        e.amount_cents for e in store.ledger
        if e.bill_id == bill_id and e.entry_type == LedgerEntryType.REFUND
    )


def refresh_bill_totals(store: Store, bill: Bill) -> Bill:
    """从流水回算账单 paid_cents / refunded_cents（调用方需持有 store.lock）。"""
    bill.paid_cents = (
        bill_cash_cents(store, bill.id) + bill_credit_net_cents(store, bill.id)
    )
    bill.refunded_cents = bill_refunded_cents(store, bill.id)
    return bill


def reconcile(store: Store, bill_id: str) -> dict:
    with store.lock:
        bill = store.get_bill(bill_id)
        cash = bill_cash_cents(store, bill_id)
        credit_net = bill_credit_net_cents(store, bill_id)
        paid = cash + credit_net
        refunded = bill_refunded_cents(store, bill_id)

        totals_ok = bill.paid_cents == paid and bill.refunded_cents == refunded
        if bill.status == BillStatus.PAID:
            status_ok = paid == bill.subtotal_cents
            expected = bill.subtotal_cents
        elif bill.status == BillStatus.FINALIZED:
            status_ok = paid <= bill.subtotal_cents
            expected = 0
        else:
            status_ok = paid == 0
            expected = 0
        # 退款不得超过实收；净收入不得为负
        refund_ok = refunded <= paid and (paid - refunded) >= 0

        result = {
            "bill_id": bill_id,
            "status": bill.status.value,
            "subtotal_cents": bill.subtotal_cents,
            "expected_cents": expected,
            "cash_cents": cash,
            "credit_cents": credit_net,
            "paid_cents": paid,
            "refunded_cents": refunded,
            "net_paid_cents": paid - refunded,
            "due_cents": max(0, bill.subtotal_cents - paid),
            "totals_match_ledger": totals_ok,
            "refund_within_received": refund_ok,
            "balanced": totals_ok and status_ok and refund_ok,
            "entries": [
                {
                    "id": e.id,
                    "type": e.entry_type.value,
                    "amount_cents": e.amount_cents,
                    "attempt_id": e.charge_attempt_id,
                    "payment_id": e.payment_id,
                    "refund_id": e.refund_id,
                    "credit_application_id": e.credit_application_id,
                }
                for e in store.ledger if e.bill_id == bill_id
            ],
        }
        logger.info(
            "reconcile bill=%s status=%s cash=%d credit=%d paid=%d refunded=%d "
            "due=%d balanced=%s",
            bill_id, bill.status.value, cash, credit_net, paid, refunded,
            result["due_cents"], result["balanced"],
        )
        return result


def reconcile_account(store: Store, customer_id: str) -> dict:
    """账户抵扣额度对账：退款入账 - 净抵扣 == 账户当前额度。"""
    with store.lock:
        funded = sum(
            e.amount_cents for e in store.ledger
            if e.customer_id == customer_id and e.entry_type == LedgerEntryType.REFUND
        )
        applied = sum(
            e.amount_cents for e in store.ledger
            if e.customer_id == customer_id
            and e.entry_type == LedgerEntryType.CREDIT_APPLIED
        )
        released = sum(
            e.amount_cents for e in store.ledger
            if e.customer_id == customer_id
            and e.entry_type == LedgerEntryType.CREDIT_RELEASED
        )
        net_consumed = applied - released
        acct = store.accounts.get(customer_id)
        balance = acct.credit_cents if acct is not None else 0
        expected = funded - net_consumed
        result = {
            "customer_id": customer_id,
            "refund_funded_cents": funded,
            "credit_applied_cents": applied,
            "credit_released_cents": released,
            "expected_credit_cents": expected,
            "credit_cents": balance,
            "balanced": balance == expected and balance >= 0,
        }
        logger.info(
            "reconcile_account customer=%s funded=%d applied=%d released=%d "
            "credit=%d balanced=%s",
            customer_id, funded, applied, released, balance, result["balanced"],
        )
        return result
