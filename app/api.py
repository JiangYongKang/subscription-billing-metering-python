"""FastAPI 路由装配。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import billing, grace, ledger, metering, payments, subscriptions
from app.clock import Clock
from app.errors import BillingError
from app.schemas import (
    AccountIn,
    CancelIn,
    ChargeIn,
    ClockIn,
    PlanChangeIn,
    PlanIn,
    SubscriptionIn,
    UsageIn,
)
from app.serializers import bill_out, change_out, plan_out, sub_out
from app.store import Store

logger = logging.getLogger("billing")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Subscription Billing & Metering (local)")
    app.state.clock = Clock()
    app.state.store = Store(app.state.clock)

    @app.exception_handler(BillingError)
    async def _billing_error(_: Request, exc: BillingError):
        logger.warning("request rejected code=%s msg=%s", exc.code, exc)
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.code, "message": str(exc)},
        )

    store = lambda: app.state.store  # noqa: E731

    # ---- 管理/测试设施 ----------------------------------------------
    @app.post("/admin/clock")
    def set_clock(body: ClockIn):
        """冻结/设置当前时间（仅本地与测试用）。"""
        app.state.clock.set(body.now)
        logger.info("clock set -> %s", body.now.isoformat())
        return {"now": app.state.clock.now().isoformat()}

    @app.post("/admin/clock/advance")
    def advance_clock(seconds: int = 0, days: int = 0):
        ts = app.state.clock.advance(seconds=seconds, days=days)
        return {"now": ts.isoformat()}

    @app.get("/health")
    def health():
        return {"status": "ok", "now": app.state.clock.now().isoformat()}

    @app.post("/plans")
    def create_plan(body: PlanIn):
        from app.models import Plan

        existing = app.state.store.plans.get(body.code)
        version = (existing.version + 1) if existing else 1
        plan = Plan(
            code=body.code, name=body.name,
            base_price_cents=body.base_price_cents,
            included_units=body.included_units,
            overage_unit_price_cents=body.overage_unit_price_cents,
            currency=body.currency, version=version,
            active=True, updated_at=app.state.clock.now(),
        )
        app.state.store.put_plan(plan)
        logger.info("plan upsert code=%s v%d price=%d included=%d over_price=%d",
                    plan.code, version, plan.base_price_cents,
                    plan.included_units, plan.overage_unit_price_cents)
        return plan_out(plan)

    @app.get("/plans/{code}")
    def get_plan(code: str, version: int | None = None):
        return plan_out(app.state.store.get_plan(code, version))

    @app.post("/accounts")
    def upsert_account(body: AccountIn):
        acct = payments.ensure_account(
            app.state.store, body.customer_id, body.balance_cents, body.card_active
        )
        return {"customer_id": acct.customer_id, "balance_cents": acct.balance_cents,
                "card_active": acct.card_active}

    # ---- 订阅 --------------------------------------------------------
    @app.post("/subscriptions", status_code=201)
    def create_subscription(body: SubscriptionIn):
        sub, change = subscriptions.create_subscription(
            store(), body.customer_id, body.plan_code, body.anchor_day,
            body.idempotency_key, created_at=body.created_at,
        )
        return {"subscription": sub_out(sub), "change": change_out(change)}

    @app.get("/subscriptions/{sub_id}")
    def get_subscription(sub_id: str):
        sub = app.state.store.get_subscription(sub_id)
        out = sub_out(sub)
        out["changes_current_period"] = [
            change_out(c) for c in subscriptions.list_changes(
                app.state.store, sub_id,
                _current_period_index(app.state.store, sub),
            )
        ]
        return out

    @app.post("/subscriptions/{sub_id}/change")
    def change_plan(sub_id: str, body: PlanChangeIn):
        change = subscriptions.change_plan(
            store(), sub_id, body.new_plan_code, body.idempotency_key, body.action
        )
        return change_out(change)

    @app.post("/subscriptions/{sub_id}/cancel")
    def cancel(sub_id: str, body: CancelIn):
        change = subscriptions.cancel_subscription(
            store(), sub_id, body.effective, body.idempotency_key
        )
        return change_out(change)

    # ---- 用量 --------------------------------------------------------
    @app.post("/subscriptions/{sub_id}/usage")
    def report_usage(sub_id: str, body: UsageIn):
        from app.errors import PeriodClosedError

        try:
            ev, created = metering.record_usage(
                store(), sub_id, body.event_id, body.quantity,
                body.occurred_at, body.idempotency_key,
            )
        except PeriodClosedError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": exc.code, "message": str(exc),
                    "hint": "调用 /bills/{closed_bill_id}/adjustments 入账迟到用量",
                },
            )
        total = metering.period_total(store(), sub_id, ev.period_index)
        return {
            "event_id": ev.event_id, "period_index": ev.period_index,
            "quantity": ev.quantity, "duplicate": not created,
            "period_total_units": total,
        }

    # ---- 账单 --------------------------------------------------------
    @app.get("/subscriptions/{sub_id}/bills")
    def list_bills(sub_id: str):
        return [bill_out(b) for b in app.state.store.main_bills(sub_id)]

    @app.post("/subscriptions/{sub_id}/bills/finalize")
    def finalize_current(sub_id: str):
        bill = billing.ensure_open_bill(store(), sub_id)
        bill = billing.finalize_bill(store(), bill.id)
        return bill_out(bill)

    @app.post("/subscriptions/{sub_id}/advance")
    def advance(sub_id: str):
        bill = billing.advance_period(store(), sub_id)
        return bill_out(bill)

    @app.get("/bills/{bill_id}")
    def get_bill(bill_id: str):
        return bill_out(app.state.store.get_bill(bill_id))

    @app.post("/bills/{bill_id}/adjustments")
    def late_adjustment(bill_id: str):
        adj = billing.create_adjustment_for_late_usage(store(), bill_id)
        if adj is None:
            return {"created": False}
        return {"created": True, "bill": bill_out(adj)}

    # ---- 扣款/重试/对账 ----------------------------------------------
    @app.post("/bills/{bill_id}/charge")
    def charge(bill_id: str, body: ChargeIn | None = None):
        attempt = payments.charge_bill(store(), bill_id)
        bill = app.state.store.get_bill(bill_id)
        return {
            "attempt_no": attempt.attempt_no, "result": attempt.result.value,
            "bill_status": bill.status.value,
            "failure_reason": bill.failure_reason.value,
            "idempotency_key": attempt.idempotency_key,
            "duplicate_charge": bool(attempt.dedup_idempotency_key),
        }

    @app.post("/bills/{bill_id}/retries")
    def run_retries(bill_id: str, max_attempts: int = grace.DEFAULT_MAX_ATTEMPTS,
                    base_interval_sec: int = grace.DEFAULT_BASE_INTERVAL_SEC,
                    grace_seconds: int = grace.DEFAULT_GRACE_SECONDS):
        bill = grace.run_retries(
            store(), bill_id, max_attempts=max_attempts,
            base_interval_sec=base_interval_sec, grace_seconds=grace_seconds,
        )
        state = grace.apply_grace_policy(store(), bill_id, grace_seconds)
        return {"bill": bill_out(bill), "subscription_state": state}

    @app.get("/bills/{bill_id}/reconcile")
    def reconcile(bill_id: str):
        return ledger.reconcile(store(), bill_id)

    return app


def _current_period_index(store_obj, sub) -> int:
    from app.period import period_index_for

    return period_index_for(
        sub.current_period_start, store_obj.now(), sub.anchor_day
    )
