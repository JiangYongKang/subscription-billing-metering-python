"""按量计量：幂等上报、账期累计与超额统计。

幂等约定：(订阅, 事件 ID) 唯一。重复上报（含不同数量/日期）一律返回
首次记录、不二次累计；因此延迟、乱序上报都不会重复计费或漏计。
用量归属账期按事件发生日（occurred_on）划分，而非上报时间。
"""
from __future__ import annotations

import itertools
import logging
from datetime import date

from .models import BillingError, Subscription, UsageRecord
from .pricing import period_bounds
from .store import Store

logger = logging.getLogger("billing.metering")

_usage_seq = itertools.count(1)


def _period_map(store: Store, sub_id: str,
                period_start: date) -> dict[str, UsageRecord]:
    return store.usage.setdefault(sub_id, {}).setdefault(
        period_start.isoformat(), {})


def report_usage(store: Store, sub: Subscription, event_id: str,
                 quantity: int, occurred_on: date) -> UsageRecord:
    """幂等上报用量。

    - 同一 event_id 再次上报：直接返回原记录（忽略数量/日期差异）。
    - 事件归属 occurred_on 所在账期；该账期已出账（冻结）则拒绝，
      避免把晚到事件塞进历史账目。
    """
    if quantity <= 0:
        raise BillingError("invalid_quantity", "用量必须为正数")
    period_start, period_end = period_bounds(occurred_on)

    with store.lock:
        existing = store.usage.get(sub.id, {}).get(
            period_start.isoformat(), {}).get(event_id)
        if existing is not None:
            logger.info(
                "usage-duplicate sub=%s period=%s event=%s 既有数量=%s "
                "重复请求数量=%s -> 忽略",
                sub.id, period_start, event_id, existing.quantity, quantity)
            return existing

        invoice = store.get_invoice(sub.id, period_start)
        if invoice is not None and invoice.finalized:
            logger.warning(
                "usage-rejected sub=%s period=%s event=%s 账期已出账(%s)",
                sub.id, period_start, event_id, invoice.status.value)
            raise BillingError(
                "period_closed",
                f"账期 {period_start} 已出账，事件 {event_id} 不能补记到历史账期",
            )

        record = UsageRecord(
            id=f"u-{next(_usage_seq)}",
            subscription_id=sub.id,
            period_start=period_start,
            event_id=event_id,
            quantity=quantity,
            occurred_on=occurred_on,
        )
        _period_map(store, sub.id, period_start)[event_id] = record
        logger.info(
            "usage-accepted sub=%s period=%s event=%s qty=%s occurred_on=%s",
            sub.id, period_start, event_id, quantity, occurred_on)
        return record


def period_records(store: Store, sub_id: str,
                   period_start: date) -> list[UsageRecord]:
    """账期内去重后的全部事件（按 event_id 排序，保证累计确定可解释）。"""
    with store.lock:
        events = store.usage.get(sub_id, {}).get(period_start.isoformat(), {})
        return [events[k] for k in sorted(events)]


def period_usage(store: Store, sub_id: str, period_start: date) -> int:
    """账期内去重后的累计用量。"""
    return sum(r.quantity for r in period_records(store, sub_id, period_start))
