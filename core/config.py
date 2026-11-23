# -*- coding: utf-8 -*-
"""运行时配置：把 WebUI 的松散配置收敛成稳定、带校验的对象。

设计原则：
- 任何一项配置写错或缺失都不应让插件崩溃，只回退到默认值；
- 数值统一做区间裁剪，避免 ``--n 999`` 之类的输入把服务打爆；
- 密钥支持环境变量兜底，方便 Docker / 面板部署。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

PROVIDER_KINDS = ("openai", "gemini", "dashscope", "mock")

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "dashscope": "https://dashscope.aliyuncs.com/api/v1",
    "mock": "mock://local",
}

DEFAULT_MODELS = {
    "openai": "gpt-image-1",
    "gemini": "gemini-2.5-flash-image",
    "dashscope": "qwen-image-plus",
    "mock": "mock-1",
}

# 自动触发前缀：刻意避开单个“画”字，防止把正常聊天当成命令
DEFAULT_TRIGGER_PREFIXES = (
    "画一张", "画一幅", "画个", "画一个", "画张", "画只",
    "帮我画", "给我画", "帮我生成", "给我生成",
    "生成一张", "生成一幅", "生成图片", "生成图", "生图",
    "绘制一张", "绘制一个",
)

_TRUE_WORDS = {"1", "true", "yes", "on", "y", "是", "开", "开启", "启用"}
_FALSE_WORDS = {"0", "false", "no", "off", "n", "否", "关", "关闭", "禁用"}


def _get(mapping, key, default=None):
    """容错读取：mapping 可能是 AstrBotConfig、dict 或 None。"""
    if mapping is None:
        return default
    try:
        value = mapping.get(key, default)
    except Exception:  # noqa: BLE001 - 配置对象异常时一律回退
        return default
    return default if value is None else value


def as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    return default


def as_int(value, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return default
    if lo is not None and number < lo:
        number = lo
    if hi is not None and number > hi:
        number = hi
    return number


def as_float(value, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None and number < lo:
        number = lo
    if hi is not None and number > hi:
        number = hi
    return number


def as_str(value, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return ",".join(as_str(x) for x in value if as_str(x))
    return str(value).strip()


def as_list(value) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        items = [as_str(x) for x in value]
    elif isinstance(value, str):
        items = [x.strip() for x in re.split(r"[,，;；\n]+", value)]
    else:
        items = []
    return [x for x in items if x]


def _norm_kind(value, default: str = "openai") -> str:
    kind = as_str(value, default).lower()
    return kind if kind in PROVIDER_KINDS else default


@dataclass
class ProviderProfile:
    """单个服务商的连接配置。"""

    kind: str = "openai"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    quality: str = "auto"

    def resolved_base_url(self) -> str:
        return (self.base_url or DEFAULT_BASE_URLS.get(self.kind, "")).rstrip("/")

    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.kind, "")

    def ready(self) -> bool:
        """是否可以发起生成。mock 永远可用，其余必须有密钥。"""
        if self.kind == "mock":
            return True
        return bool(self.api_key and self.resolved_base_url() and self.resolved_model())

    def describe(self) -> str:
        base = self.resolved_base_url()
        return f"{self.kind} · {self.resolved_model() or '(未配置模型)'} · {base or '(未配置地址)'}"


@dataclass
class RuntimeSettings:
    """插件整体运行时设置（已校验、已裁剪）。"""

    primary: ProviderProfile = field(default_factory=ProviderProfile)
    backup: ProviderProfile | None = None
    fallback_on_error: bool = True

    default_style: str = "general"
    default_ratio: str = "1:1"
    size_mode: str = "auto"
    custom_size: str = "1024x1024"
    openai_response_format: str = "auto"
    default_images: int = 1
    max_images: int = 4
    max_prompt_chars: int = 800
    negative_as_hint: bool = True

    auto_trigger: bool = True
    trigger_prefixes: tuple[str, ...] = DEFAULT_TRIGGER_PREFIXES
    allow_group: bool = True
    allow_private: bool = True
    admin_only: bool = False
    blocked_words: tuple[str, ...] = ()

    cooldown_seconds: float = 15.0
    daily_limit: int = 30
    max_concurrency: int = 2
    queue_timeout: float = 120.0
    max_queue: int = 8
    request_timeout: float = 180.0
    max_retries: int = 2
    max_download_bytes: int = 20 * 1024 * 1024

    show_progress: bool = True
    show_caption: bool = True
    show_prompt: bool = False

    keep_days: int = 7
    max_files: int = 2000

    tool_enabled: bool = True

    # ---------- 构造 ----------

    @classmethod
    def from_mapping(cls, config) -> "RuntimeSettings":
        primary = ProviderProfile(
            kind=_norm_kind(_get(config, "provider", "openai")),
            api_key=as_str(_get(config, "api_key", "")) or as_str(os.environ.get("IMGGEN_API_KEY", "")),
            base_url=as_str(_get(config, "base_url", "")) or as_str(os.environ.get("IMGGEN_BASE_URL", "")),
            model=as_str(_get(config, "model", "")) or as_str(os.environ.get("IMGGEN_MODEL", "")),
            quality=as_str(_get(config, "quality", "auto")) or "auto",
        )

        backup = None
        if as_bool(_get(config, "backup_enabled", False), False):
            backup_kind = _norm_kind(_get(config, "backup_provider", "mock"), "mock")
            candidate = ProviderProfile(
                kind=backup_kind,
                api_key=as_str(_get(config, "backup_api_key", ""))
                or as_str(os.environ.get("IMGGEN_BACKUP_API_KEY", "")),
                base_url=as_str(_get(config, "backup_base_url", ""))
                or as_str(os.environ.get("IMGGEN_BACKUP_BASE_URL", "")),
                model=as_str(_get(config, "backup_model", ""))
                or as_str(os.environ.get("IMGGEN_BACKUP_MODEL", "")),
                quality="auto",
            )
            # 与主服务完全相同的连接再挂一份没有意义
            if candidate.kind != primary.kind or candidate.resolved_base_url() != primary.resolved_base_url():
                backup = candidate

        response_format = as_str(_get(config, "openai_response_format", "auto")).lower()
        if response_format not in ("auto", "url", "b64"):
            response_format = "auto"

        prefixes = tuple(as_list(_get(config, "trigger_prefixes", ""))) or DEFAULT_TRIGGER_PREFIXES
        prefixes = tuple(p for p in prefixes if p and p != "画")

        return cls(
            primary=primary,
            backup=backup,
            fallback_on_error=as_bool(_get(config, "fallback_on_error", True), True),
            default_style=as_str(_get(config, "default_style", "general")) or "general",
            default_ratio=as_str(_get(config, "default_ratio", "1:1")) or "1:1",
            size_mode="custom" if as_str(_get(config, "size_mode", "auto")).lower() == "custom" else "auto",
            custom_size=as_str(_get(config, "custom_size", "1024x1024")) or "1024x1024",
            openai_response_format=response_format,
            default_images=as_int(_get(config, "default_images", 1), 1, 1, 4),
            max_images=as_int(_get(config, "max_images", 4), 4, 1, 10),
            max_prompt_chars=as_int(_get(config, "max_prompt_chars", 800), 800, 50, 4000),
            negative_as_hint=as_bool(_get(config, "negative_as_hint", True), True),
            auto_trigger=as_bool(_get(config, "auto_trigger", True), True),
            trigger_prefixes=prefixes,
            allow_group=as_bool(_get(config, "allow_group", True), True),
            allow_private=as_bool(_get(config, "allow_private", True), True),
            admin_only=as_bool(_get(config, "admin_only", False), False),
            blocked_words=tuple(as_list(_get(config, "blocked_words", ""))),
            cooldown_seconds=as_float(_get(config, "cooldown_seconds", 15), 15.0, 0.0, 86400.0),
            daily_limit=as_int(_get(config, "daily_limit", 30), 30, 0, 100000),
            max_concurrency=as_int(_get(config, "max_concurrency", 2), 2, 1, 16),
            queue_timeout=as_float(_get(config, "queue_timeout", 120), 120.0, 5.0, 3600.0),
            max_queue=as_int(_get(config, "max_queue", 8), 8, 0, 200),
            request_timeout=as_float(_get(config, "request_timeout", 180), 180.0, 5.0, 1800.0),
            max_retries=as_int(_get(config, "max_retries", 2), 2, 0, 5),
            max_download_bytes=as_int(_get(config, "max_download_mb", 20), 20, 1, 200) * 1024 * 1024,
            show_progress=as_bool(_get(config, "show_progress", True), True),
            show_caption=as_bool(_get(config, "show_caption", True), True),
            show_prompt=as_bool(_get(config, "show_prompt", False), False),
            keep_days=as_int(_get(config, "keep_days", 7), 7, 0, 3650),
            max_files=as_int(_get(config, "max_files", 2000), 2000, 10, 200000),
            tool_enabled=as_bool(_get(config, "tool_enabled", True), True),
        )

    # ---------- 查询 ----------

    def primary_ready(self) -> bool:
        return self.primary.ready()

    def chain_ready(self) -> bool:
        return self.primary.ready() or bool(self.backup and self.backup.ready())

    def allowed_images(self, requested: int | None) -> int:
        count = requested if requested else self.default_images
        return max(1, min(int(count), self.max_images))
