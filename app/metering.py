"""按量计量：幂等上报、乱序/延迟归集、账期累计。

幂等约定：
- ``event_id`` 是业务事件的唯一键：相同 event_id 重复上报直接返回既有
  事件，不重复累计。
- ``idempotency_key`` 是请求键：同键不同载荷视为冲突（DuplicateEventError），
  防止上游用错键覆盖数据。
- 归属账期由 ``occurred_at``（业务发生时间）决定，而非上报顺序，所以乱序
  上报累计结果不变。
- 事件落在已关账账期时抛 PeriodClosedError，由出账调整单处理，绝不漏计。
"""

import logging

from app.clock import as_utc
from app.errors import DuplicateEventError, PeriodClosedError, StateError
from app.models import BillStatus, SubscriptionState, UsageEvent
from app.period import period_index_for
from app.store import Store

logger = logging.getLogger("billing.metering")


def _load_snapshot(store: Store, sub, at) -> tuple[int, object | None]:
    pidx = period_index_for(sub.current_period_start, at, sub.anchor_day)
    return pidx, store.period_bill(sub.id, pidx)


def record_usage(
    store: Store,
    subscription_id: str,
    event_id: str,
    quantity: int,
    occurred_at,
    idempotency_key: str = "",
) -> tuple[UsageEvent, bool]:
    with store.lock:
        at = as_utc(occurred_at)
        existing = store.events.get(event_id)
        if existing is not None:
            logger.info("usage dedup event_id=%s qty=%d period=%d -> 已存在，不重复累计",
                        event_id, existing.quantity, existing.period_index)
            return existing, False

        if idempotency_key:
            prior_id = store.events_by_key.get(idempotency_key)
            if prior_id and prior_id != event_id:
                raise DuplicateEventError(
                    f"idempotency_key={idempotency_key} already used by event {prior_id}"
                )

        sub = store.get_subscription(subscription_id)
        if sub.state == SubscriptionState.EXPIRED:
            raise StateError(f"subscription {sub.id} expired")
        if at < as_utc(sub.created_at):
            raise StateError(f"event occurred_at {at} before subscription creation")

        pidx, bill = _load_snapshot(store, sub, at)
        closed_bill_id = bill.id if bill is not None and bill.status != BillStatus.OPEN else None

        # 即使账期已关也先持久化，保证延迟/乱序事件不丢；随后提示走调整单
        event = UsageEvent(
            event_id=event_id,
            subscription_id=subscription_id,
            period_index=pidx,
            quantity=quantity,
            occurred_at=at,
            recorded_at=store.now(),
            idempotency_key=idempotency_key,
        )
        store.events[event_id] = event
        if idempotency_key:
            store.events_by_key[idempotency_key] = event_id
        logger.info(
            "usage recorded event_id=%s sub=%s qty=%d occurred_at=%s period=%d closed=%s",
            event_id, subscription_id, quantity, at.isoformat(), pidx,
            closed_bill_id is not None,
        )
        if closed_bill_id is not None:
            logger.warning(
                "usage late event_id=%s period=%d bill=%s -> 走调整单",
                event_id, pidx, closed_bill_id,
            )
            raise PeriodClosedError(
                f"period {pidx} already billed on {closed_bill_id}; "
                "POST /bills/{bill_id}/adjustments to bill late usage"
            )
        return event, True


def period_total(store: Store, subscription_id: str, period_index: int) -> int:
    with store.lock:
        return sum(
            e.quantity for e in store.events.values()
            if e.subscription_id == subscription_id and e.period_index == period_index
        )


def unbilled_total(store: Store, subscription_id: str, period_index: int) -> int:
    with store.lock:
        return sum(
            e.quantity for e in store.events.values()
            if e.subscription_id == subscription_id
            and e.period_index == period_index and not e.billed
        )
