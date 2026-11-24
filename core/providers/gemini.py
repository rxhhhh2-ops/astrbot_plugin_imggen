# -*- coding: utf-8 -*-
"""Google Gemini 原生 ``generateContent`` 适配器。

Gemini 原生接口没有 n / negative_prompt 参数：
- n > 1 时按张数顺序请求（避免并发触发限流）；
- 比例通过 ``generationConfig.imageConfig.aspectRatio`` 传递，
  个别模型不支持该字段时自动去掉再重试一次。
"""

from __future__ import annotations

from .base import ImageProvider, ImageResult, ProviderError, request_json


class GeminiProvider(ImageProvider):
    kind = "gemini"
    supports_negative = False

    def _endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith(":generateContent"):
            return base
        return f"{base}/models/{self.model}:generateContent"

    async def generate(
        self, session, *, prompt, negative, n, ratio
    ) -> list[ImageResult]:
        size = self.size_for(ratio)
        want = max(1, int(n))
        results: list[ImageResult] = []
        last_error: ProviderError | None = None

        for index in range(want):
            try:
                results.extend(await self._generate_once(session, prompt, size))
            except ProviderError as exc:
                last_error = exc
                # 第一张就失败则直接上抛；后续失败保留已成功的结果
                if not results:
                    raise
                break

        if not results:
            raise last_error or ProviderError("模型未返回图片", retryable=False)
        return results

    async def _generate_once(self, session, prompt: str, size) -> list[ImageResult]:
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        generation_config: dict = {"responseModalities": ["TEXT", "IMAGE"]}
        if self.size_mode != "custom":
            generation_config["imageConfig"] = {"aspectRatio": size.value}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }

        try:
            data = await request_json(
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
        except ProviderError as exc:
            # 部分模型/中转不认 imageConfig：去掉后重试一次
            if exc.status == 400 and "imageConfig" in generation_config:
                generation_config.pop("imageConfig", None)
                data = await request_json(
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
            else:
                raise

        items, texts = self._extract_parts(data)
        if not items:
            finish_reason = ""
            candidates = data.get("candidates") or []
            if candidates:
                finish_reason = str(candidates[0].get("finishReason") or "")
            hint = ("；".join(texts))[:200]
            reason = f"（{finish_reason}）" if finish_reason else ""
            detail = f"：{hint}" if hint else ""
            raise ProviderError(
                f"模型没有返回图片{reason}{detail}（可能被安全策略拦截）",
                retryable=False,
            )

        results: list[ImageResult] = []
        for item in items:
            item.setdefault("width", size.width)
            item.setdefault("height", size.height)
            results.append(await self._as_result(session, item, size))
        return results

    @staticmethod
    def _extract_parts(data: dict) -> tuple[list[dict], list[str]]:
        items: list[dict] = []
        texts: list[str] = []
        for candidate in data.get("candidates") or []:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                inline = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline, dict) and inline.get("data"):
                    items.append(
                        {
                            "b64_json": inline["data"],
                            "mime_type": inline.get("mimeType")
                            or inline.get("mime_type"),
                        }
                    )
                elif isinstance(part.get("text"), str):
                    texts.append(part["text"])
        # 响应被截断时，公共层可能从破损 JSON 里抢救出 data 条目
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("b64_json"):
                items.append(item)
        return items, texts
