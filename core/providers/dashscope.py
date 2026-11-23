# -*- coding: utf-8 -*-
"""阿里云百炼 DashScope 文生图适配器（异步任务 + 轮询）。

接口：POST /services/aigc/text2image/image-synthesis（X-DashScope-Async: enable）
      GET  /tasks/{task_id}
适用模型：qwen-image / qwen-image-plus / wanx2.x 系列等。
"""
from __future__ import annotations

import asyncio
import time

from .base import ImageProvider, ImageResult, ProviderError, request_json

_TERMINAL_OK = {"SUCCEEDED", "SUCCESS"}
_TERMINAL_FAIL = {"FAILED", "CANCELED", "CANCELLED", "UNKNOWN"}


class DashScopeProvider(ImageProvider):
    kind = "dashscope"
    supports_negative = True

    poll_interval = 3.0

    def _create_endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/text2image/image-synthesis"):
            return base
        return f"{base}/services/aigc/text2image/image-synthesis"

    def _task_endpoint(self, task_id: str) -> str:
        base = self.base_url.rstrip("/")
        return f"{base}/tasks/{task_id}"

    async def generate(self, session, *, prompt, negative, n, ratio) -> list[ImageResult]:
        size = self.size_for(ratio)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        input_payload: dict = {"prompt": prompt}
        if negative:
            input_payload["negative_prompt"] = negative
        payload = {
            "model": self.model,
            "input": input_payload,
            "parameters": {"size": size.value, "n": max(1, int(n))},
        }

        data = await request_json(
            session,
            "POST",
            self._create_endpoint(),
            headers=headers,
            payload=payload,
            timeout=self.request_timeout,
            retries=self.max_retries,
            backoff=self.backoff,
            secret=self.api_key,
            retry_on_timeout=False,
        )
        output = data.get("output") if isinstance(data.get("output"), dict) else {}

        # 少数模型/代理会同步返回结果
        results = self._extract_results(output)
        if results:
            return await self._download_all(session, results, size)

        task_id = output.get("task_id") or output.get("taskId")
        if not task_id:
            raise ProviderError("未获取到任务 ID，请检查模型是否支持文生图", retryable=False)

        deadline = time.monotonic() + self.request_timeout
        last_status = "PENDING"
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval)
            remaining = max(5.0, deadline - time.monotonic())
            poll = await request_json(
                session,
                "GET",
                self._task_endpoint(str(task_id)),
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=min(30.0, remaining),
                retries=1,
                backoff=self.backoff,
                secret=self.api_key,
            )
            out = poll.get("output") if isinstance(poll.get("output"), dict) else {}
            status = str(out.get("task_status") or out.get("taskStatus") or "").upper()
            last_status = status or last_status
            if status in _TERMINAL_OK:
                results = self._extract_results(out)
                if not results:
                    raise ProviderError("任务成功但没有返回结果图", retryable=False)
                return await self._download_all(session, results, size)
            if status in _TERMINAL_FAIL:
                message = out.get("message") or out.get("code") or status
                raise ProviderError(f"生图任务失败：{message}", retryable=False)

        raise ProviderError(f"生图任务超时未完成（最后状态 {last_status}）", retryable=True)

    async def _download_all(self, session, items: list[dict], size) -> list[ImageResult]:
        results: list[ImageResult] = []
        errors: list[ProviderError] = []
        for item in items:
            try:
                results.append(await self._as_result(session, item, size))
            except ProviderError as exc:
                errors.append(exc)
        if not results:
            raise errors[0] if errors else ProviderError("结果图解析失败", retryable=False)
        return results

    @staticmethod
    def _extract_results(output: dict) -> list[dict]:
        raw = output.get("results")
        if not isinstance(raw, list):
            task_result = output.get("task_result")
            if isinstance(task_result, dict):
                raw = task_result.get("images") or task_result.get("results")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict) and (item.get("url") or item.get("b64_json"))]
