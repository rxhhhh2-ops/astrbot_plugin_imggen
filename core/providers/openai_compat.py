# -*- coding: utf-8 -*-
"""OpenAI 兼容 ``/v1/images/generations`` 适配器。

覆盖：OpenAI 官方（gpt-image-1 / dall-e-3）、硅基流动、智谱 CogView、
以及各类中转站。响应解析尽量宽容（b64_json / url / images 列表 / 单个 url）。

返回格式策略：默认 **优先 response_format=url**——URL 的 JSON 只有几百字节，
能绕开中转站/代理截断大 base64 响应的问题；失败或服务商不支持时再退回 b64。
可用配置 ``openai_response_format`` 强制指定 auto/url/b64。
"""

from __future__ import annotations

import aiohttp

from astrbot.api import logger

from .base import (
    ImageProvider,
    ImageResult,
    ProviderError,
    ReferenceImage,
    request_json,
)


class OpenAICompatProvider(ImageProvider):
    kind = "openai"
    supports_negative = False
    supports_edit = True

    def _endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/images/generations"):
            return base
        return f"{base}/images/generations"

    def _edit_endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/images/edits"):
            return base
        if base.endswith("/images/generations"):
            base = base[: -len("/images/generations")]
        return f"{base}/images/edits"

    def _format_plans(self) -> list[str]:
        """返回本次请求的返回格式尝试顺序。"""
        model_lower = self.model.lower()
        mode = (self.response_mode or "auto").lower()
        if model_lower.startswith("gpt-image"):
            # 官方 gpt-image 固定返回 b64，且不接受 response_format；
            # 只有用户显式配成 url 时，才额外试探一次中转站是否支持。
            return ["url", "b64"] if mode == "url" else ["b64"]
        if mode == "b64":
            return ["b64"]
        if mode == "url":
            return ["url", "b64"]
        return ["url", "b64"]

    def _payload(self, prompt: str, count: int, size, fmt: str) -> dict:
        model_lower = self.model.lower()
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "n": count,
            "size": size.value,
        }
        send_format = not (model_lower.startswith("gpt-image") and fmt == "b64")
        if send_format:
            payload["response_format"] = fmt
        if self.quality and self.quality.lower() not in ("", "auto", "默认"):
            payload["quality"] = self.quality
        return payload

    async def _post(self, session, payload: dict, headers: dict) -> dict:
        """发送请求；部分中转站不认 response_format / quality 时去掉再试一次。"""
        payload = dict(payload)

        async def post() -> dict:
            return await request_json(
                session,
                "POST",
                self._endpoint(),
                headers=headers,
                payload=payload,
                timeout=self.request_timeout,
                retries=self.max_retries,
                backoff=self.backoff,
                secret=self.api_key,
                retry_on_timeout=False,
            )

        try:
            return await post()
        except ProviderError as exc:
            lowered = (exc.message or "").lower()
            stripped = False
            if exc.status == 400:
                if "response_format" in payload and "response_format" in lowered:
                    payload.pop("response_format", None)
                    stripped = True
                if "quality" in payload and "quality" in lowered:
                    payload.pop("quality", None)
                    stripped = True
            if not stripped:
                raise
            return await post()

    async def generate(
        self, session, *, prompt, negative, n, ratio
    ) -> list[ImageResult]:
        size = self.size_for(ratio)
        model_lower = self.model.lower()
        # dall-e-3 官方一次只允许 1 张
        count = 1 if model_lower.startswith("dall-e-3") else max(1, int(n))
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        plans = self._format_plans()
        last_error: ProviderError | None = None

        for index, fmt in enumerate(plans):
            payload = self._payload(prompt, count, size, fmt)
            try:
                data = await self._post(session, payload, headers)
                items = self._extract_items(data)
                if not items:
                    raise ProviderError("服务商响应中没有图片数据", retryable=False)
                return await self._results_from_items(session, items, size, count)
            except ProviderError as exc:
                last_error = exc
                has_next = index < len(plans) - 1
                retry_other = (
                    exc.retryable
                    or "不是合法 JSON" in exc.message
                    or (fmt == "url" and exc.status not in (400, 401, 403))
                )
                if has_next and retry_other:
                    logger.warning(
                        "[imggen] openai 以 %s 格式请求失败（%s），改用 %s 重试",
                        fmt,
                        exc.message[:120],
                        plans[index + 1],
                    )
                    continue
                raise

        raise last_error or ProviderError("生成失败（未知原因）", retryable=True)

    async def _results_from_items(
        self, session, items, size, count
    ) -> list[ImageResult]:
        """把响应条目转成图片结果，容忍部分条目失败。"""
        results: list[ImageResult] = []
        errors: list[ProviderError] = []
        for item in items[:count]:
            try:
                results.append(await self._as_result(session, item, size))
            except ProviderError as exc:
                errors.append(exc)
        if results:
            return results
        raise errors[0] if errors else ProviderError("图片解析失败", retryable=False)

    def _edit_form(
        self,
        prompt: str,
        count: int,
        size,
        fmt: str,
        image: ReferenceImage,
        *,
        keep_format: bool = True,
        keep_quality: bool = True,
    ) -> aiohttp.FormData:
        form = aiohttp.FormData()
        form.add_field("model", self.model)
        form.add_field("prompt", prompt)
        form.add_field("n", str(count))
        form.add_field("size", size.value)
        send_format = keep_format and not (
            self.model.lower().startswith("gpt-image") and fmt == "b64"
        )
        if send_format:
            form.add_field("response_format", fmt)
        if (
            keep_quality
            and self.quality
            and self.quality.lower() not in ("", "auto", "默认")
        ):
            form.add_field("quality", self.quality)
        form.add_field(
            "image",
            image.data,
            filename=image.name or "reference.png",
            content_type=image.mime or "image/png",
        )
        return form

    async def _edit_post(
        self, session, prompt: str, image: ReferenceImage, count: int, size, fmt: str
    ) -> dict:
        """multipart 调用 ``/images/edits``；不认 response_format/quality 时去掉重试。"""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        keep_format = True
        keep_quality = True

        async def post() -> dict:
            form = self._edit_form(
                prompt,
                count,
                size,
                fmt,
                image,
                keep_format=keep_format,
                keep_quality=keep_quality,
            )
            return await request_json(
                session,
                "POST",
                self._edit_endpoint(),
                headers=headers,
                form=form,
                timeout=self.request_timeout,
                retries=self.max_retries,
                backoff=self.backoff,
                secret=self.api_key,
                retry_on_timeout=False,
            )

        try:
            return await post()
        except ProviderError as exc:
            if exc.status != 400:
                raise
            lowered = (exc.message or "").lower()
            changed = False
            if keep_format and "response_format" in lowered:
                keep_format = False
                changed = True
            if keep_quality and "quality" in lowered:
                keep_quality = False
                changed = True
            if not changed:
                raise
            return await post()

    async def edit(
        self, session, *, prompt, negative, n, ratio, image: ReferenceImage
    ) -> list[ImageResult]:
        size = self.size_for(ratio)
        model_lower = self.model.lower()
        count = 1 if model_lower.startswith("dall-e-3") else max(1, int(n))

        plans = self._format_plans()
        last_error: ProviderError | None = None

        for index, fmt in enumerate(plans):
            try:
                data = await self._edit_post(session, prompt, image, count, size, fmt)
                items = self._extract_items(data)
                if not items:
                    raise ProviderError("服务商响应中没有图片数据", retryable=False)
                return await self._results_from_items(session, items, size, count)
            except ProviderError as exc:
                last_error = exc
                has_next = index < len(plans) - 1
                retry_other = (
                    exc.retryable
                    or "不是合法 JSON" in exc.message
                    or (fmt == "url" and exc.status not in (400, 401, 403))
                )
                if has_next and retry_other:
                    logger.warning(
                        "[imggen] openai 图生图以 %s 格式请求失败（%s），改用 %s 重试",
                        fmt,
                        exc.message[:120],
                        plans[index + 1],
                    )
                    continue
                raise

        raise last_error or ProviderError("图生图失败（未知原因）", retryable=True)

    @staticmethod
    def _extract_items(data: dict) -> list[dict]:
        """从各种兼容返回结构里挖出图片条目。"""
        items: list[dict] = []

        def absorb(value) -> None:
            if isinstance(value, list):
                items.extend(v for v in value if isinstance(v, dict))
            elif isinstance(value, dict):
                if any(
                    k in value
                    for k in ("b64_json", "b64", "base64", "url", "image", "image_url")
                ):
                    items.append(value)
                for sub_key in ("images", "data", "results", "artifacts"):
                    sub = value.get(sub_key)
                    if isinstance(sub, list):
                        items.extend(v for v in sub if isinstance(v, dict))

        for key in ("data", "images", "output", "result", "results", "artifacts"):
            if key in data:
                absorb(data[key])
                if items:
                    break

        if not items:
            if isinstance(data.get("url"), str):
                items.append({"url": data["url"]})
            elif isinstance(data.get("b64_json"), str):
                items.append({"b64_json": data["b64_json"]})
        return items
