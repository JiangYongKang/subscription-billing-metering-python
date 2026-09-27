"""账期边界计算（月度锚点）。

账期由订阅创建时刻与每月 ``anchor_day``（1-28）界定，结束于下一个锚点日
00:00 UTC（左闭右开）。用量事件按业务发生时间 ``occurred_at`` 归属账期，
因此延迟/乱序上报仍能落到正确账期。
"""

from calendar import monthrange
from datetime import datetime, timezone

from app.clock import as_utc


def _clamp_day(year: int, month: int, day: int) -> int:
    return min(day, monthrange(year, month)[1])


def add_months(year: int, month: int, n: int) -> tuple[int, int]:
    y = year + (month - 1 + n) // 12
    m = (month - 1 + n) % 12 + 1
    return y, m


def next_anchor(after: datetime, anchor_day: int) -> datetime:
    """after 之后（不含）最近的锚点日 00:00 UTC。"""
    y, m = after.year, after.month
    candidate = datetime(y, m, _clamp_day(y, m, anchor_day), tzinfo=timezone.utc)
    if candidate <= after:
        y, m = add_months(y, m, 1)
        candidate = datetime(y, m, _clamp_day(y, m, anchor_day), tzinfo=timezone.utc)
    return candidate


def initial_period(created_at: datetime, anchor_day: int) -> tuple[datetime, datetime]:
    start = as_utc(created_at)
    return start, next_anchor(start, anchor_day)


def advance_window(start: datetime, anchor_day: int) -> tuple[datetime, datetime]:
    end = next_anchor(start, anchor_day)
    return end, next_anchor(end, anchor_day)


def period_index_for(start: datetime, when: datetime, anchor_day: int) -> int:
    """when 相对首个账期起始 start 的账期序号（从 0 起，可为负，理论上不会）。"""
    when = as_utc(when)
    if when < start:
        return 0
    idx = 0
    win_start = start
    while True:
        nxt = next_anchor(win_start, anchor_day)
        if when < nxt:
            return idx
        win_start = nxt
        idx += 1


def window_for_index(start: datetime, anchor_day: int, index: int) -> tuple[datetime, datetime]:
    win_start = start
    for _ in range(index):
        win_start, _ = advance_window(win_start, anchor_day)
    return win_start, next_anchor(win_start, anchor_day)


def days_between(a: datetime, b: datetime) -> int:
    """自然日数（按 UTC 日期差），用于按天按比例。"""
    a = as_utc(a)
    b = as_utc(b)
    return (b.date() - a.date()).days


def billable_days(start: datetime, end: datetime) -> int:
    """账期计费天数，至少 1 天，避免短账期除零。"""
    return max(1, days_between(start, end))


def segment_days(effective_at: datetime, next_effective_at: datetime | None,
                 period_end: datetime) -> int:
    end = next_effective_at if next_effective_at is not None else period_end
    if end > period_end:
        end = period_end
    return max(1, (as_utc(end).date() - as_utc(effective_at).date()).days)
