"""配置：扣款重试节奏与宽限期（均可按测试需要覆盖）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryConfig:
    # 第 1 次扣款在出账当天；失败后距上一次尝试 N 天再扣，依次取值。
    # 默认：第 1 天、第 4 天、第 11 天各重试一次（共 4 次尝试）。
    retry_intervals_days: tuple[int, ...] = (1, 3, 7)
    # 宽限天数：自账单开出之日起 grace_days 天内订阅保留服务。
    grace_days: int = 14

    @property
    def max_attempts(self) -> int:
        """最多尝试次数 = 首次 + 计划内重试次数。"""
        return 1 + len(self.retry_intervals_days)

    def interval_before_attempt(self, attempt: int) -> int | None:
        """第 attempt 次尝试（从 1 开始）距上一次尝试的间隔天数。

        第 1 次为 0（出账即扣）；超出重试计划返回 None。
        """
        if attempt <= 1:
            return 0
        idx = attempt - 2
        if idx >= len(self.retry_intervals_days):
            return None
        return self.retry_intervals_days[idx]

    def scheduled_attempt_on(self, issued_on, attempt: int):
        """根据出账日计算第 attempt 次尝试的计划日期。"""
        from datetime import timedelta
        interval = self.interval_before_attempt(attempt)
        if interval is None:
            return None
        return issued_on + timedelta(days=interval)


DEFAULT_CONFIG = RetryConfig()
