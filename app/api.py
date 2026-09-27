"""FastAPI 路由：所有状态操作落在全局 STORE，时间默认取当天，可用 as_of 注入。"""
from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import billing
from .config import DEFAULT_CONFIG, RetryConfig
from .metering import period_usage, report_usage
from .models import (BillingError, CancelIn, ChargeIn, ChangePlanIn,
                     CreateSubscriptionIn, UsageIn)
from .payments import MockGateway
from .pricing import period_bounds
from .serializers import invoice_dict, sub_dict, txn_dict, usage_dict
from .store import STORE

logger = logging.getLogger("billing.api")

router = APIRouter()


def get_config(request: Request) -> RetryConfig:
    return getattr(request.app.state, "retry_config", DEFAULT_CONFIG)


def get_gateway(request: Request) -> MockGateway:
    return getattr(request.app.state, "gateway", MockGateway())


def billing_error_handler(request: Request, exc: BillingError) -> JSONResponse:
    logger.warning("billing-error code=%s msg=%s path=%s",
                   exc.code, exc.args[0], request.url.path)
    status = 404 if exc.code in ("subscription_not_found",
                                 "invoice_not_found") else 409
    return JSONResponse(status_code=status,
                        content={"error": exc.code, "message": exc.args[0]})


def _as_of(value: date | None) -> date:
    return value or date.today()


@router.post("/subscriptions", status_code=201)
def create_subscription(body: CreateSubscriptionIn, request: Request,
                        as_of: date | None = None):
    sub = billing.create_subscription(
        STORE, body, as_of=_as_of(as_of), config=get_config(request))
    return sub_dict(sub)


@router.post("/subscriptions/{sub_id}/change")
def change_plan(sub_id: str, body: ChangePlanIn, request: Request,
                as_of: date | None = None):
    sub = billing.change_plan(STORE, sub_id, body, as_of=_as_of(as_of))
    return sub_dict(sub)


@router.post("/subscriptions/{sub_id}/cancel")
def cancel(sub_id: str, body: CancelIn, request: Request,
           as_of: date | None = None):
    sub = billing.cancel_subscription(
        STORE, sub_id, body, as_of=_as_of(as_of),
        config=get_config(request), gateway=get_gateway(request))
    return sub_dict(sub)


@router.get("/subscriptions/{sub_id}")
def get_subscription(sub_id: str):
    sub = STORE.require_subscription(sub_id)
    return sub_dict(sub)


@router.post("/subscriptions/{sub_id}/usage", status_code=201)
def report_usage_endpoint(sub_id: str, body: UsageIn,
                          as_of: date | None = None):
    sub = STORE.require_subscription(sub_id)
    before = period_usage(STORE, sub_id,
                          period_bounds(body.occurred_on or _as_of(as_of))[0])
    rec = report_usage(STORE, sub, body.event_id, body.quantity,
                       body.occurred_on or _as_of(as_of))
    after = period_usage(STORE, sub_id, rec.period_start)
    return {
        "usage": usage_dict(rec),
        "period_total": after,
        "deduplicated": before == after,
    }


@router.get("/subscriptions/{sub_id}/invoices")
def list_invoices(sub_id: str):
    STORE.require_subscription(sub_id)
    invs = [i for i in STORE.invoices.values() if i.subscription_id == sub_id]
    invs.sort(key=lambda i: (i.period_start, i.id))
    return [invoice_dict(i) for i in invs]


@router.post("/invoices/{invoice_id}/charge")
def charge(invoice_id: str, request: Request, body: ChargeIn | None = None):
    body = body or ChargeIn()
    inv = STORE.require_invoice(invoice_id)
    txn = billing_charge(inv, request, body)
    return {"transaction": txn_dict(txn),
            "invoice": invoice_dict(STORE.invoices[inv.id])}


def billing_charge(inv, request: Request, body: ChargeIn):
    from .payments import charge_invoice
    return charge_invoice(
        STORE, inv, as_of=_as_of(body.as_of), config=get_config(request),
        gateway=get_gateway(request), idempotency_key=body.idempotency_key)


@router.post("/billing/run")
def run_all(request: Request, body: ChargeIn | None = None):
    body = body or ChargeIn()
    result = billing.run_billing_cycle(
        STORE, as_of=_as_of(body.as_of), config=get_config(request),
        gateway=get_gateway(request))
    result["reconcile"] = billing.reconcile(STORE)
    return result


@router.post("/subscriptions/{sub_id}/run-billing")
def run_billing(sub_id: str, request: Request, body: ChargeIn | None = None):
    STORE.require_subscription(sub_id)
    body = body or ChargeIn()
    result = billing.run_billing_cycle(
        STORE, as_of=_as_of(body.as_of), config=get_config(request),
        gateway=get_gateway(request))
    return result


@router.get("/reconcile")
def reconcile():
    return billing.reconcile(STORE)


class PlanPriceIn(BaseModel):
    base_price_cents: int
    overage_price_cents: int | None = None


@router.post("/admin/plans/{tier}/price")
def update_plan_price(tier: str, body: PlanPriceIn):
    """调价：只影响新出账/仍在 DRAFT 的费用，历史账单不变。"""
    from .catalog import get_plan, update_plan_price
    from .models import PlanTier
    try:
        pt = PlanTier(tier)
        get_plan(pt)
    except ValueError:
        raise BillingError("unknown_plan", f"未知套餐: {tier}")
    plan = update_plan_price(pt, base_price_cents=body.base_price_cents,
                             overage_price_cents=body.overage_price_cents)
    return {"tier": plan.tier.value,
            "base_price_cents": plan.base_price_cents,
            "overage_price_cents": plan.overage_price_cents}
