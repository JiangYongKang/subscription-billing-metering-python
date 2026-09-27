"""账期工具与确定性按比例计费。

规则（详见 README）：
- 账期 = 自然月 [每月 1 日, 下月 1 日)。
- 一个账期内套餐被切成若干互不重叠、首尾相接的时间片段；
  每个自然日按当天有效套餐「恰好」计费一次，因此升降级天然不需要
  负向冲抵行，绝不会对同一天重复计费。
- 片段费用 = 套餐月费 * 片段天数 / 账期天数。
- 全部金额以整数「分」表示：先求各片段的精确分摊，再用
  「最大余数法」把不足 1 分的舍入差一次性、确定性地分到具体行，
  保证各行金额之和严格等于账单总额，任何人都可复算。
"""
from __future__ import annotations

from datetime import date, timedelta

from .catalog import get_plan
from .models import (Invoice, LineItem, LineKind, PlanSegment, UsageRecord)

# 内部精确比例放大倍数（仅用于中间计算与排序）。
PRORATION_SCALE = 1_000_000


def period_bounds(any_day: date) -> tuple[date, date]:
    """any_day 所在自然月账期 [start, end)。"""
    start = any_day.replace(day=1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start, end


def next_period(start: date) -> tuple[date, date]:
    """下一账期 [start, end)，入参为本账期起始日。"""
    if start.month == 12:
        nxt = date(start.year + 1, 1, 1)
    else:
        nxt = date(start.year, start.month + 1, 1)
    _, end = period_bounds(nxt)
    return nxt, end


def days_between(a: date, b: date) -> int:
    return (b - a).days


def _round_half_up(numerator: int, denominator: int) -> int:
    """正整数除法，四舍五入（0.5 入）。"""
    return (numerator + denominator // 2) // denominator


def _clip_segments(segments: list[PlanSegment],
                   period_start: date, period_end: date) -> list[PlanSegment]:
    """把片段裁剪到账期范围内，丢弃零长度片段。"""
    out: list[PlanSegment] = []
    for seg in segments:
        s = max(seg.start, period_start)
        e = min(seg.end, period_end)
        if e > s:
            out.append(PlanSegment(tier=seg.tier, start=s, end=e, note=seg.note))
    return out


def build_base_line_items(segments: list[PlanSegment],
                          period_start: date, period_end: date) -> list[LineItem]:
    """把套餐片段折算为确定性的按比例基础费行（每行整数分，合计精确）。"""
    clipped = _clip_segments(segments, period_start, period_end)
    period_days = days_between(period_start, period_end)
    if period_days <= 0:
        return []

    # 1) 每行先向下取整到分，并记录余数（分子意义：price*天数）。
    floors: list[int] = []
    fractions: list[int] = []
    for seg in clipped:
        plan = get_plan(seg.tier)
        seg_days = days_between(seg.start, seg.end)
        raw = plan.base_price_cents * seg_days
        floors.append(raw // period_days)
        fractions.append(raw % period_days)

    # 2) 总额四舍五入；差额用最大余数法分配（余数相同则靠前的片段先得）。
    exact_total = _round_half_up(
        sum(get_plan(s.tier).base_price_cents * days_between(s.start, s.end)
            for s in clipped),
        period_days,
    )
    remainder = exact_total - sum(floors)
    order = sorted(range(len(clipped)), key=lambda i: (-fractions[i], i))
    amounts = floors[:]
    for i in order:
        if remainder <= 0:
            break
        amounts[i] += 1
        remainder -= 1

    lines: list[LineItem] = []
    for seg, amount in zip(clipped, amounts):
        plan = get_plan(seg.tier)
        seg_days = days_between(seg.start, seg.end)
        lines.append(LineItem(
            kind=LineKind.BASE,
            tier=seg.tier,
            description=(
                f"{plan.name} 按比例基础费 {seg.start.isoformat()}~"
                f"{(seg.end - timedelta(days=1)).isoformat()} "
                f"({seg_days}/{period_days}天)"
            ),
            amount_cents=amount,
            meta={
                "unit_price_cents": plan.base_price_cents,
                "segment_days": seg_days,
                "period_days": period_days,
                "note": seg.note,
            },
        ))
    return lines


def segment_index_for_date(segments: list[PlanSegment], day: date) -> int | None:
    for i, seg in enumerate(segments):
        if seg.start <= day < seg.end:
            return i
    return None


def build_overage_line_items(segments: list[PlanSegment],
                             period_start: date, period_end: date,
                             records: list[UsageRecord]) -> list[LineItem]:
    """按片段统计用量与超额。

    每片段额度 = 套餐包含额度按片段天数比例折算（四舍五入到整单位）；
    落在片段内的用量超出该片段额度的部分，按该套餐超额单价单独成行。
    """
    clipped = _clip_segments(segments, period_start, period_end)
    period_days = days_between(period_start, period_end)
    usage_by_seg = [0] * len(clipped)
    for rec in records:
        idx = segment_index_for_date(clipped, rec.occurred_on)
        if idx is not None:
            usage_by_seg[idx] += rec.quantity

    lines: list[LineItem] = []
    for seg, used in zip(clipped, usage_by_seg):
        plan = get_plan(seg.tier)
        seg_days = days_between(seg.start, seg.end)
        allowance = _round_half_up(plan.included_units * seg_days, period_days)
        over = max(0, used - allowance)
        if over <= 0:
            continue
        amount = over * plan.overage_price_cents
        lines.append(LineItem(
            kind=LineKind.OVERAGE,
            tier=seg.tier,
            description=(
                f"{plan.name} 超额用量 {over} 单位 x {plan.overage_price_cents}分 "
                f"(片段用量{used}/额度{allowance})"
            ),
            amount_cents=amount,
            units=over,
            meta={
                "segment_usage": used,
                "segment_allowance": allowance,
                "overage_price_cents": plan.overage_price_cents,
                "segment_days": seg_days,
                "period_days": period_days,
            },
        ))
    return lines


def recompute_draft(invoice: Invoice, segments: list[PlanSegment],
                    records: list[UsageRecord]) -> None:
    """重算 DRAFT 账单：按比例基础费 + 分片段超额费。

    仅允许对 DRAFT（账期未出账）调用；已出账账单重算会直接报错，
    以保证历史账目不可变。
    """
    from .models import BillingError, InvoiceStatus
    if invoice.status != InvoiceStatus.DRAFT:
        raise BillingError(
            "invoice_finalized",
            f"账单 {invoice.id} 已出账（{invoice.status.value}），历史账期不可重算",
        )
    base = build_base_line_items(segments, invoice.period_start, invoice.period_end)
    overage = build_overage_line_items(
        segments, invoice.period_start, invoice.period_end, records)
    invoice.line_items = base + overage
    invoice.total_cents = sum(item.amount_cents for item in invoice.line_items)
