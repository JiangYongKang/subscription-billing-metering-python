"""可注入、可确定性推进的时钟。

生产默认走真实 UTC；测试可 set() 冻结、advance() 推进，使重试节奏与
宽限期判定可重复、可断言。
"""

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """把时间统一成带时区的 UTC；naive 时间按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Clock:
    def __init__(self) -> None:
        self._fixed: datetime | None = None

    def now(self) -> datetime:
        if self._fixed is not None:
            return self._fixed
        return utcnow()

    def set(self, value: datetime) -> datetime:
        self._fixed = as_utc(value)
        return self._fixed

    def reset(self) -> None:
        self._fixed = None

    def advance(self, *, seconds: float = 0, days: float = 0) -> datetime:
        """在冻结时间上前移；若未冻结则先冻结在当前真实时刻。"""
        if self._fixed is None:
            self._fixed = utcnow()
        self._fixed = self._fixed + timedelta(seconds=seconds, days=days)
        return self._fixed
