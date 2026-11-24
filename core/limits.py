# -*- coding: utf-8 -*-
"""并发与配额控制：冷却、每日额度、全局并发闸门、排队上限。

全部在进程内存中维护：AstrBot 重启后计数清零（可接受，且不会误伤用户）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date
from pathlib import Path

_log = logging.getLogger("astrbot.plugin.imggen")


class LimitError(Exception):
    """限流/配额类的可展示错误。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UsageLimiter:
    def __init__(
        self,
        cooldown_seconds: float = 15.0,
        daily_limit: int = 30,
        max_concurrency: int = 2,
        queue_timeout: float = 120.0,
        max_queue: int = 8,
        state_path: str | Path | None = None,
    ):
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.daily_limit = max(0, int(daily_limit))
        self.max_concurrency = max(1, int(max_concurrency))
        self.queue_timeout = max(1.0, float(queue_timeout))
        self.max_queue = max(0, int(max_queue))
        self._state_path = Path(state_path) if state_path else None

        self._sem: asyncio.Semaphore | None = None
        self._inflight: set[str] = set()
        self._last_start: dict[str, float] = {}
        self._last_start_wall: dict[str, float] = {}
        self._daily: dict[str, list] = {}  # user -> [date_str, count]
        self._waiting = 0
        self._total_today: list = [date.today().isoformat(), 0]
        self._load_state()

    # ---------- 内部 ----------

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrency)
        return self._sem

    def _today_bucket(self, user_key: str) -> list:
        today = date.today().isoformat()
        bucket = self._daily.get(user_key)
        if not bucket or bucket[0] != today:
            bucket = [today, 0]
            self._daily[user_key] = bucket
        return bucket

    def _remaining_cooldown(self, user_key: str) -> float:
        if self.cooldown_seconds <= 0:
            return 0.0
        last = self._last_start.get(user_key)
        if last is None:
            # 不能用 0.0 当哨兵：Linux 容器/新机器上 time.monotonic() 可能很小，
            # 会把“从未请求过”误判成刚请求过，导致首次请求被冷却拦截。
            return 0.0
        return max(0.0, self.cooldown_seconds - (time.monotonic() - last))

    # ---------- 持久化 ----------

    def _load_state(self) -> None:
        if self._state_path is None:
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            _log.warning(f"[imggen] 读取额度状态失败：{exc!r}")
            return
        if not isinstance(data, dict):
            return

        today = date.today().isoformat()
        daily = data.get("daily")
        if isinstance(daily, dict):
            for user, bucket in daily.items():
                if (
                    isinstance(bucket, list)
                    and len(bucket) == 2
                    and str(bucket[0]) == today
                ):
                    try:
                        self._daily[str(user)] = [today, max(0, int(bucket[1]))]
                    except (TypeError, ValueError):
                        continue
        total = data.get("total_today")
        if isinstance(total, list) and len(total) == 2 and str(total[0]) == today:
            try:
                self._total_today = [today, max(0, int(total[1]))]
            except (TypeError, ValueError):
                pass

        starts = data.get("last_start")
        if isinstance(starts, dict):
            now_wall = time.time()
            now_mono = time.monotonic()
            keep = max(self.cooldown_seconds * 2, 300.0)
            for user, value in starts.items():
                try:
                    wall = float(value)
                except (TypeError, ValueError):
                    continue
                if now_wall - wall > keep:
                    continue
                key = str(user)
                self._last_start_wall[key] = wall
                self._last_start[key] = now_mono - max(0.0, now_wall - wall)

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        today = date.today().isoformat()
        now_wall = time.time()
        keep = max(self.cooldown_seconds * 2, 300.0)
        data = {
            "daily": {
                user: bucket
                for user, bucket in self._daily.items()
                if isinstance(bucket, list) and bucket and bucket[0] == today
            },
            "total_today": self._total_today,
            "last_start": {
                user: wall
                for user, wall in self._last_start_wall.items()
                if now_wall - wall <= keep
            },
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_name(self._state_path.name + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_path)
        except Exception as exc:  # noqa: BLE001
            _log.warning(f"[imggen] 保存额度状态失败：{exc!r}")

    # ---------- 对外 ----------

    async def acquire_slot(self) -> None:
        """只占用全局并发名额（WebUI 工作台用，不计用户冷却/额度）。"""
        sem = self._semaphore()
        if sem.locked() and self._waiting >= self.max_queue:
            raise LimitError("当前排队人数较多，请稍后再试")
        self._waiting += 1
        try:
            await asyncio.wait_for(sem.acquire(), timeout=self.queue_timeout)
        except asyncio.TimeoutError:
            raise LimitError(f"排队超过 {self.queue_timeout:.0f} 秒仍未轮到，请稍后再试") from None
        finally:
            self._waiting = max(0, self._waiting - 1)

    def release_slot(self) -> None:
        try:
            self._semaphore().release()
        except Exception:  # noqa: BLE001
            pass

    def inflight_count(self) -> int:
        return len(self._inflight)

    def precheck(self, user_key: str) -> str:
        """快速检查是否可生成，返回错误文案；可生成时返回空串（不占用名额）。"""
        if user_key in self._inflight:
            return "你上一张还在绘制中，等它出来再试吧～"
        remain = self._remaining_cooldown(user_key)
        if remain > 0:
            return f"冷却中，请 {remain:.0f} 秒后再试"
        bucket = self._today_bucket(user_key)
        if self.daily_limit > 0 and bucket[1] >= self.daily_limit:
            return f"今天的 {self.daily_limit} 次生图额度已用完，明天再来吧～"
        sem = self._semaphore()
        if sem.locked() and self._waiting >= self.max_queue:
            return "当前排队人数较多，请稍后再试"
        return ""

    async def acquire(self, user_key: str) -> None:
        """占用一个生成名额；失败抛 LimitError（消息可直接回给用户）。"""
        if user_key in self._inflight:
            raise LimitError("你上一张还在绘制中，等它出来再试吧～")

        remain = self._remaining_cooldown(user_key)
        if remain > 0:
            raise LimitError(f"冷却中，请 {remain:.0f} 秒后再试")

        bucket = self._today_bucket(user_key)
        if self.daily_limit > 0 and bucket[1] >= self.daily_limit:
            raise LimitError(f"今天的 {self.daily_limit} 次生图额度已用完，明天再来吧～")

        sem = self._semaphore()
        if sem.locked() and self._waiting >= self.max_queue:
            raise LimitError("当前排队人数较多，请稍后再试")

        self._waiting += 1
        try:
            await asyncio.wait_for(sem.acquire(), timeout=self.queue_timeout)
        except asyncio.TimeoutError:
            raise LimitError(f"排队超过 {self.queue_timeout:.0f} 秒仍未轮到，请稍后再试") from None
        finally:
            self._waiting = max(0, self._waiting - 1)

        self._inflight.add(user_key)
        self._last_start[user_key] = time.monotonic()
        self._last_start_wall[user_key] = time.time()
        bucket[1] += 1

        today = date.today().isoformat()
        if self._total_today[0] != today:
            self._total_today = [today, 0]
        self._total_today[1] += 1
        self._save_state()

    def release(self, user_key: str, success: bool = True) -> None:
        """释放名额。失败的请求不计入每日额度。"""
        self._inflight.discard(user_key)
        try:
            self._semaphore().release()
        except Exception:  # noqa: BLE001 - 防御性：重复释放不应崩溃
            pass
        if not success:
            bucket = self._daily.get(user_key)
            if bucket and bucket[0] == date.today().isoformat() and bucket[1] > 0:
                bucket[1] -= 1
            if self._total_today[0] == date.today().isoformat() and self._total_today[1] > 0:
                self._total_today[1] -= 1
        self._save_state()

    def snapshot(self, user_key: str | None = None) -> dict:
        info = {
            "inflight": len(self._inflight),
            "waiting": self._waiting,
            "max_concurrency": self.max_concurrency,
            "total_today": self._total_today[1] if self._total_today[0] == date.today().isoformat() else 0,
        }
        if user_key:
            bucket = self._today_bucket(user_key)
            info["user_today"] = bucket[1]
            info["user_cooldown"] = round(self._remaining_cooldown(user_key), 1)
        return info
