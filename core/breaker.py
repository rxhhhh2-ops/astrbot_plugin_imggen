# -*- coding: utf-8 -*-
"""服务商熔断器：连续失败后短暂跳过该线路，避免每次都等满超时。"""

from __future__ import annotations

import time
from typing import Callable


class ProviderBreaker:
    """按 provider key 记录连续失败；达到阈值后冷却一段时间。

    ``key`` 一般由服务商 kind + base_url + model 组成，避免把不同线路混在一起。
    """

    def __init__(
        self,
        threshold: int = 3,
        cooldown: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.threshold = max(1, int(threshold))
        self.cooldown = max(1.0, float(cooldown))
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        return self._open_until.get(key, 0.0) <= self._clock()

    def retry_after(self, key: str) -> float:
        return max(0.0, self._open_until.get(key, 0.0) - self._clock())

    def record_failure(self, key: str) -> None:
        count = self._failures.get(key, 0) + 1
        if count >= self.threshold:
            self._failures[key] = 0
            self._open_until[key] = self._clock() + self.cooldown
            return
        self._failures[key] = count

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._open_until.pop(key, None)

    def reset(self) -> None:
        self._failures.clear()
        self._open_until.clear()

    def snapshot(self) -> dict[str, float]:
        """返回仍在熔断中的 key 与剩余秒数。"""
        now = self._clock()
        return {
            key: round(until - now, 1)
            for key, until in self._open_until.items()
            if until > now
        }
