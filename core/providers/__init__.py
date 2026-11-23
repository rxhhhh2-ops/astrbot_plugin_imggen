# -*- coding: utf-8 -*-
"""服务商注册表与构建工厂。"""
from __future__ import annotations

from typing import Type

from ..config import ProviderProfile, RuntimeSettings
from .base import ImageProvider, ImageResult, ProviderError
from .dashscope import DashScopeProvider
from .gemini import GeminiProvider
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider

_PROVIDER_CLASSES: dict[str, Type[ImageProvider]] = {
    "openai": OpenAICompatProvider,
    "gemini": GeminiProvider,
    "dashscope": DashScopeProvider,
    "mock": MockProvider,
}


def build_provider(profile: ProviderProfile | None, settings: RuntimeSettings) -> ImageProvider | None:
    """按配置构造适配器；配置不完整（如缺 key）时返回 None。"""
    if profile is None or not profile.ready():
        return None
    cls = _PROVIDER_CLASSES.get(profile.kind)
    if cls is None:
        return None
    return cls(
        profile,
        request_timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        backoff=2.0,
        max_download_bytes=settings.max_download_bytes,
        size_mode=settings.size_mode,
        custom_size=settings.custom_size,
        response_mode=settings.openai_response_format,
    )


def build_chain(settings: RuntimeSettings) -> list[ImageProvider]:
    """主服务在前、备用在后，构建可用链。"""
    chain: list[ImageProvider] = []
    primary = build_provider(settings.primary, settings)
    if primary is not None:
        chain.append(primary)
    if settings.backup is not None:
        backup = build_provider(settings.backup, settings)
        if backup is not None:
            chain.append(backup)
    return chain


__all__ = [
    "ImageProvider",
    "ImageResult",
    "ProviderError",
    "build_provider",
    "build_chain",
    "OpenAICompatProvider",
    "GeminiProvider",
    "DashScopeProvider",
    "MockProvider",
]
