"""并发周期任务：多线程同时推进出账，不重复出账/扣款/续费。"""
import threading
from datetime import date

from app.models import (CreateSubscriptionIn, InvoiceStatus, PlanTier,
                        TxnStatus)
import app.billing as B


def test_concurrent_billing_runs_rollover_once(store, config, gateway):
    subs = []
    for _ in range(20):
        subs.append(B.create_subscription(
            store,
            CreateSubscriptionIn(tier=PlanTier.BASIC,
                                start_date=date(2026, 9, 1)),
            as_of=date(2026, 9, 1), config=config))

    errors = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            B.run_billing_cycle(store, as_of=date(2026, 10, 1),
                                config=config, gateway=gateway)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []

    # 每个订阅恰好：1 张 9 月 PAID 账单 + 1 张 10 月 DRAFT；只扣一次款
    for sub in subs:
        invs = [i for i in store.invoices.values() if i.subscription_id == sub.id]
        assert len(invs) == 2
        sep = store.get_invoice(sub.id, date(2026, 9, 1))
        octp = store.get_invoice(sub.id, date(2026, 10, 1))
        assert sep.status == InvoiceStatus.PAID
        assert octp.status == InvoiceStatus.DRAFT
        succ = [t for t in store.transactions
                if t.invoice_id == sep.id and t.status == TxnStatus.SUCCESS]
        assert len(succ) == 1
    rec = B.reconcile(store)
    assert rec["ok"] is True, rec["errors"]
    assert rec["paid_invoices"] == 20
