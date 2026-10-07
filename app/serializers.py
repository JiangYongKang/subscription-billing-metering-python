"""领域对象 -> JSON。"""


def plan_out(p):
    return {
        "code": p.code, "name": p.name, "base_price_cents": p.base_price_cents,
        "included_units": p.included_units,
        "overage_unit_price_cents": p.overage_unit_price_cents,
        "currency": p.currency, "version": p.version, "active": p.active,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def sub_out(s):
    return {
        "id": s.id, "customer_id": s.customer_id, "plan_code": s.plan_code,
        "plan_version": s.plan_version, "state": s.state.value,
        "anchor_day": s.anchor_day, "created_at": s.created_at.isoformat(),
        "canceled_at": s.canceled_at.isoformat() if s.canceled_at else None,
        "cancel_effective": s.cancel_effective,
        "current_period_start": s.current_period_start.isoformat() if s.current_period_start else None,
        "current_period_end": s.current_period_end.isoformat() if s.current_period_end else None,
        "grace_until": s.grace_until.isoformat() if s.grace_until else None,
        "last_bill_id": s.last_bill_id,
    }


def change_out(c):
    return {
        "id": c.id, "subscription_id": c.subscription_id,
        "period_index": c.period_index, "effective_at": c.effective_at.isoformat(),
        "plan_code": c.plan_code, "plan_version": c.plan_version,
        "action": c.action, "base_price_cents": c.base_price_cents,
        "included_units": c.included_units,
        "overage_unit_price_cents": c.overage_unit_price_cents,
        "idempotency_key": c.idempotency_key,
        "proration_days": c.proration_days, "proration_cents": c.proration_cents,
        "note": c.note,
    }


def bill_out(b):
    return {
        "id": b.id, "subscription_id": b.subscription_id,
        "customer_id": b.customer_id, "period_index": b.period_index,
        "period_start": b.period_start.isoformat(),
        "period_end": b.period_end.isoformat(),
        "currency": b.currency, "status": b.status.value,
        "subtotal_cents": b.subtotal_cents, "total_units": b.total_units,
        "overage_units": b.overage_units,
        "paid_cents": b.paid_cents, "refunded_cents": b.refunded_cents,
        "net_paid_cents": b.paid_cents - b.refunded_cents,
        "due_cents": max(0, b.subtotal_cents - b.paid_cents),
        "is_adjustment": b.is_adjustment,
        "charge_attempts": b.charge_attempts,
        "failure_reason": b.failure_reason.value,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "finalized_at": b.finalized_at.isoformat() if b.finalized_at else None,
        "paid_at": b.paid_at.isoformat() if b.paid_at else None,
        "lines": [
            {"kind": l.kind, "description": l.description,
             "amount_cents": l.amount_cents, "detail": l.detail}
            for l in b.lines
        ],
    }


def payment_out(p):
    return {
        "id": p.id, "bill_id": p.bill_id,
        "subscription_id": p.subscription_id, "customer_id": p.customer_id,
        "requested_status": p.status,
        "amount_cents": p.amount_cents, "credit_cents": p.credit_cents,
        "total_applied_cents": p.amount_cents + p.credit_cents,
        "charge_attempt_id": p.charge_attempt_id,
        "idempotency_key": p.idempotency_key,
        "created_at": p.created_at.isoformat(),
        "result": p.result.value if p.result else None,
        "note": p.note,
    }


def refund_out(r):
    return {
        "id": r.id, "bill_id": r.bill_id,
        "subscription_id": r.subscription_id, "customer_id": r.customer_id,
        "amount_cents": r.amount_cents, "reason": r.reason,
        "idempotency_key": r.idempotency_key,
        "created_at": r.created_at.isoformat(),
        "note": r.note,
        "allocations": [
            {"charge_attempt_id": a.charge_attempt_id,
             "payment_id": a.payment_id, "amount_cents": a.amount_cents}
            for a in r.allocations
        ],
    }
