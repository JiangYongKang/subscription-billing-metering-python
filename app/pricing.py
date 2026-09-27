"""按比例计费与超额计费的纯函数（可解释、确定性）。

规则（账期内多次变更，按自然日分段）：

1. 每次创建/升级/降级/取消形成一个分段 [effective, next_effective)，
   基础费 = floor(套餐价 * 分段天数 / 账期天数)，并在 detail 里给出
   分子分母与单价，使每个数字可追溯。
2. 用量按业务发生日归属分段。每段"按比例包含额度" = floor(额度 * 天数 /
   账期天数)，余量可向后续分段结转（升/降级不浪费已积累额度）；
   超过累计额度的单位按该事件所在分段的超额单价计费。
"""

from app.clock import as_utc
from app.models import BillLineItem, PlanChange
from app.period import billable_days


def proration_amount(base_price_cents: int, segment_days: int, period_days: int) -> int:
    if period_days <= 0:
        raise ValueError("period_days must be positive")
    return base_price_cents * segment_days // period_days


def _segment_spans(changes: list[PlanChange], period_start, period_end,
                   cutoff=None):
    """返回 [(change, start, end_exclusive, days)]。

    取消行（action=cancel）只代表服务终止边界，不作为计费分段；cutoff 为
    服务有效截止时刻，其后的时间不计任何费用。
    """
    period_end = as_utc(period_end)
    if cutoff is not None:
        period_end = min(period_end, as_utc(cutoff))
    ordered = sorted(
        (c for c in changes if c.action != "cancel"),
        key=lambda c: c.effective_at,
    )
    spans = []
    for i, ch in enumerate(ordered):
        nxt = ordered[i + 1].effective_at if i + 1 < len(ordered) else None
        start = max(as_utc(ch.effective_at), as_utc(period_start))
        end = as_utc(nxt) if nxt else period_end
        end = min(end, period_end)
        days = max(1, (end.date() - start.date()).days)
        spans.append((ch, start, end, days))
    return spans


def build_proration_lines(changes: list[PlanChange], period_start, period_end,
                          cutoff=None) -> list[BillLineItem]:
    period_days = billable_days(period_start, period_end)
    lines = []
    for ch, start, _end, days in _segment_spans(
        changes, period_start, period_end, cutoff
    ):
        amount = proration_amount(ch.base_price_cents, days, period_days)
        lines.append(BillLineItem(
            kind="proration",
            description=f"{ch.action} -> {ch.plan_code} v{ch.plan_version} "
                        f"{start.date()} 起 {days} 天按比例基础费",
            amount_cents=amount,
            detail={
                "plan_code": ch.plan_code,
                "plan_version": ch.plan_version,
                "action": ch.action,
                "base_price_cents": ch.base_price_cents,
                "segment_days": days,
                "period_days": period_days,
                "formula": f"floor({ch.base_price_cents} * {days} / {period_days})",
                "change_id": ch.id,
            },
        ))
    return lines


def build_usage_lines(total_units: int, changes: list[PlanChange],
                      period_start, period_end,
                      units_by_segment: list[int] | None = None,
                      cutoff=None) -> list[BillLineItem]:
    """生成本账期的包含额度与超额明细。

    units_by_segment 与 changes（按时间排序后）等长，表示各分段内按业务
    发生日归集到的用量；为 None 时全部用量计入最后一段。
    """
    period_days = billable_days(period_start, period_end)
    spans = _segment_spans(changes, period_start, period_end, cutoff)
    if units_by_segment is None:
        units_by_segment = [0] * (len(spans) - 1) + [total_units]

    lines: list[BillLineItem] = []
    carry_included = 0
    overage_total = 0
    used = 0
    for (ch, start, _end, days), units in zip(spans, units_by_segment):
        included = ch.included_units * days // period_days
        carry_in = carry_included
        available = carry_in + included
        free = min(units, available)
        over = max(0, units - available)
        carry_included = max(0, available - units)
        overage_total += over
        used += units
        lines.append(BillLineItem(
            kind="included",
            description=f"{ch.plan_code} v{ch.plan_version} 分段 {start.date()} "
                        f"可用额度 {available}（本段 {included}+上段结转 {carry_in}）覆盖 {free} 单位",
            amount_cents=0,
            detail={
                "plan_code": ch.plan_code,
                "plan_version": ch.plan_version,
                "segment_days": days,
                "period_days": period_days,
                "included_base": included,
                "carry_in": carry_in,
                "included_available": available,
                "units_in_segment": units,
                "covered_units": free,
            },
        ))
        if over:
            lines.append(BillLineItem(
                kind="overage",
                description=f"{ch.plan_code} v{ch.plan_version} 超额 {over} 单位 "
                            f"x {ch.overage_unit_price_cents} 分",
                amount_cents=over * ch.overage_unit_price_cents,
                detail={
                    "plan_code": ch.plan_code,
                    "plan_version": ch.plan_version,
                    "overage_units": over,
                    "unit_price_cents": ch.overage_unit_price_cents,
                    "formula": f"{over} * {ch.overage_unit_price_cents}",
                },
            ))
    return lines


def units_by_segment_dates(events, changes: list[PlanChange],
                           period_start, period_end, clamp_end=None) -> list[int]:
    """把去重后的用量事件按业务发生日归集到各变更分段。

    :param events: 含 occurred_at 与 quantity 的事件序列
    :param clamp_end: 立即取消时的有效截止时刻；其后事件不归属本账期计费
    """
    import bisect

    effective_cutoff = as_utc(clamp_end) if clamp_end else None
    spans = _segment_spans(changes, period_start, period_end, effective_cutoff)
    bounds = [span[1] for span in spans]          # 各段起点
    ends = [span[2] for span in spans]
    totals = [0] * len(spans)
    cutoff = effective_cutoff
    for ev in events:
        at = as_utc(ev.occurred_at)
        if at < as_utc(period_start) or at >= as_utc(period_end):
            continue
        if cutoff is not None and at >= cutoff:
            continue
        idx = bisect.bisect_right(bounds, at) - 1
        if 0 <= idx < len(totals) and at < ends[idx]:
            totals[idx] += ev.quantity
    return totals
