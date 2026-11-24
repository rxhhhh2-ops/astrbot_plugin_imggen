# -*- coding: utf-8 -*-
"""AstrBot 插件：绘影生图（多风格 AI 生图）。

用户在 QQ 等平台发一句话即可出图：
    /画 一只戴墨镜的橘猫
    /画 国风水墨 山间小筑
    /画 赛博朋克 雨夜街景 --比例 16:9 --n 2
    （也支持“画一张……”“帮我画……”开头直接触发）

稳定性设计：
- 全局并发闸门 + 排队上限 + 冷却 + 每日额度，防止被打爆或刷额度；
- 单次请求超时、可重试错误退避重试、备用服务商自动降级；
- 图片字节魔数校验，拒绝把错误页当图片发出；
- 本地图片按“超期 + 超量”自动清理，避免撑满磁盘；
- 配置缺失/写错时给出明确指引，不抛异常刷屏。
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import MessageChain, filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

# 插件页面后端：不同 AstrBot 版本有的提供 astrbot.api.web，有的只有 Quart
try:
    from astrbot.api.web import request as _web_request
except Exception:  # noqa: BLE001 - 旧版本没有该模块
    _web_request = None

try:
    from astrbot.api.web import file_response as _web_file_response
except Exception:  # noqa: BLE001
    _web_file_response = None

try:  # 作为包加载（正常安装方式）
    from .core.breaker import ProviderBreaker
    from .core.config import (
        DEFAULT_BASE_URLS,
        DEFAULT_MODELS,
        PROVIDER_KINDS,
        RuntimeSettings,
        as_bool,
        as_int,
        as_str,
    )
    from .core.limits import LimitError, UsageLimiter
    from .core.prompt import (
        build_prompt,
        join_parts,
        sanitize,
        strip_command,
        strip_prefixes,
        truncate,
    )
    from .core.providers import ProviderError, build_chain
    from .core.providers.base import (
        ImageResult,
        ReferenceImage,
        decode_base64,
        download_image,
        sniff_image,
    )
    from .core.sizes import RATIOS, parse_ratio
    from .core.store import ImageStore
    from .core.styles import Style, StyleBook
except ImportError:  # 作为顶层模块加载（测试/独立运行）
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from core.breaker import ProviderBreaker  # type: ignore
    from core.config import (  # type: ignore
        DEFAULT_BASE_URLS,
        DEFAULT_MODELS,
        PROVIDER_KINDS,
        RuntimeSettings,
        as_bool,
        as_int,
        as_str,
    )
    from core.limits import LimitError, UsageLimiter  # type: ignore
    from core.prompt import (  # type: ignore
        build_prompt,
        join_parts,
        sanitize,
        strip_command,
        strip_prefixes,
        truncate,
    )
    from core.providers import ProviderError, build_chain  # type: ignore
    from core.providers.base import (  # type: ignore
        ImageResult,
        ReferenceImage,
        decode_base64,
        download_image,
        sniff_image,
    )
    from core.sizes import RATIOS, parse_ratio  # type: ignore
    from core.store import ImageStore  # type: ignore
    from core.styles import Style, StyleBook  # type: ignore

PLUGIN_NAME = "astrbot_plugin_imggen"
PLUGIN_VERSION = "1.3.2"
COMMAND_NAMES = ("画", "绘画", "画图", "生图", "draw", "imagine", "绘制")
STYLE_COMMAND_NAMES = ("画风", "风格", "画风列表")
STATUS_COMMAND_NAMES = ("画状态", "生图状态", "绘图状态")

# 页面下载/缩略图只允许访问形如 2026-11-18/img_xxx.png 的文件
_SAFE_IMAGE_NAME = re.compile(
    r"^\d{4}-\d{2}-\d{2}/[\w.\-]+\.(?:png|jpe?g|webp|gif)$", re.IGNORECASE
)
_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

USAGE = (
    "🎨 生图用法：\n"
    "/画 <描述>                如：/画 一只戴墨镜的橘猫\n"
    "/画 <风格> <描述>         如：/画 国风水墨 山间小筑\n"
    "/画 赛博朋克 雨夜街景 --比例 16:9 --n 2\n"
    "/画风 看全部风格 · /画状态 看配额 · /画清理 清理旧图\n"
    "/画任务 看后台任务 · /画取消 <编号|全部> 取消任务"
)

# ---------- 工作台「设置」可编辑项 ----------

_PROVIDER_LABELS = {
    "openai": "OpenAI 兼容（OpenAI / 中转站）",
    "gemini": "Google Gemini",
    "dashscope": "阿里云百炼 DashScope",
    "mock": "离线占位图（mock）",
}
_PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    "openai": (
        "gpt-image-1",
        "dall-e-3",
        "dall-e-2",
        "flux.1-schnell",
        "flux.1-dev",
        "cogview-4",
        "qwen-image",
    ),
    "gemini": (
        "gemini-2.5-flash-image",
        "gemini-2.0-flash-preview-image-generation",
    ),
    "dashscope": (
        "qwen-image-plus",
        "qwen-image",
        "wanx2.1-t2i-turbo",
        "wanx2.1-t2i-plus",
    ),
    "mock": ("mock-1",),
}
_QUALITY_OPTIONS = ("auto", "high", "medium", "low", "standard", "hd")
_RESPONSE_FORMATS = ("auto", "url", "b64")

# 图生图参考图：上限与允许格式（OpenAI 兼容 edits 支持 png/jpeg/webp）
_MAX_REFERENCE_BYTES = 10 * 1024 * 1024
_ALLOWED_REFERENCE_MIMES = {"image/png", "image/jpeg", "image/webp"}
_DEFAULT_EDIT_PROMPT = "在保持原图主体与构图的前提下，重新绘制并提升细节、光影与整体质感"


def _mask_secret(value: str) -> str:
    """密钥只展示掩码；空值返回空串。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}****{text[-4:]}"


def _config_value(mapping, key: str, default=None):
    """容错读取配置对象（AstrBotConfig 可能是 dict 或自定义映射）。"""
    try:
        value = mapping.get(key, default)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value

HELP_TEXT = USAGE + "\n\n也可以在聊天里直接说“画一张……”“帮我画……”来触发。"

_HELP_WORDS = {"help", "帮助", "?", "？", "画帮助", "用法"}

# 参数解析（支持 --n 2 / --比例 16:9 / --风格 国风 与 比例:16:9 两种写法）
_NUM_FLAG = re.compile(r"--(?:n|num|数量)\s*[=:：]?\s*(\d+)", re.I)
_RATIO_FLAG = re.compile(r"--(?:ratio|size|比例|尺寸)\s*[=:：]?\s*([0-9a-zA-Z:：x×*]+)", re.I)
_STYLE_FLAG = re.compile(r"--(?:style|风格)\s*[=:：]?\s*([^\s，,]+)", re.I)
_INLINE_STYLE = re.compile(r"(?:风格|画风)\s*[=:：]\s*([^\s，,]+)")
_INLINE_RATIO = re.compile(r"(?:比例|尺寸)\s*[=:：]\s*([0-9a-zA-Z:：x×*]+)")


@dataclass
class RequestSpec:
    prompt: str = ""
    style: Style | None = None
    ratio: str = "1:1"
    count: int = 1
    error: str = ""    # 非空则直接回复该文本并结束
    notice: str = ""   # 需要随结果一起提示的说明


def _extract(pattern: re.Pattern, text: str) -> tuple[str | None, str]:
    match = pattern.search(text)
    if not match:
        return None, text
    return match.group(1), (text[: match.start()] + " " + text[match.end():])


class ImageGenPlugin(Star):
    """绘影生图插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.settings = RuntimeSettings.from_mapping(config)

        self._styles: StyleBook | None = None
        self._store: ImageStore | None = None
        self._limiter: UsageLimiter | None = None
        self._breaker = ProviderBreaker(threshold=3, cooldown=120.0)
        self._chain: list = []
        self._http: object | None = None
        self._data_dir_path: Path | None = None
        self._last_cleanup = 0.0
        self._bg_tasks: set[asyncio.Task] = set()
        self._tool_queue: asyncio.Queue | None = None
        self._tool_workers: list[asyncio.Task] = []
        self._tool_jobs: dict[str, dict] = {}
        self._stopping = False
        self._worker_restart_at = 0.0
        self._worker_restarts = 0

        if not self.settings.primary_ready():
            logger.warning(
                "[imggen] 生图服务尚未配置完成：请在 WebUI 插件配置中填写 "
                "provider / api_key / model（或把 provider 设为 mock 先试用）"
            )

        self._register_web_apis(context)

    # ==================================================================
    # 初始化
    # ==================================================================

    def _data_dir(self) -> Path:
        if self._data_dir_path is None:
            path: Path | None = None
            try:
                from astrbot.api.star import StarTools

                path = Path(str(StarTools.get_data_dir(PLUGIN_NAME)))
            except Exception:  # noqa: BLE001 - 非 AstrBot 环境或旧版 API
                path = None
            if path is None:
                path = Path(__file__).resolve().parent / "data"
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.error(f"[imggen] 数据目录创建失败，改用插件目录：{exc!r}")
                path = Path(__file__).resolve().parent / "data"
                path.mkdir(parents=True, exist_ok=True)
            self._data_dir_path = path
        return self._data_dir_path

    def _ensure(self) -> None:
        """懒加载风格库/存储/限流器/服务商链。"""
        if self._styles is not None:
            self._ensure_tool_workers()
            return
        data_dir = self._data_dir()
        self._styles = StyleBook(data_dir / "styles.json")
        for note in self._styles.load_notes:
            logger.info(f"[imggen] 用户风格：{note}")
        self._store = ImageStore(
            data_dir / "images",
            keep_days=self.settings.keep_days,
            max_files=self.settings.max_files,
        )
        self._limiter = UsageLimiter(
            cooldown_seconds=self.settings.cooldown_seconds,
            daily_limit=self.settings.daily_limit,
            max_concurrency=self.settings.max_concurrency,
            queue_timeout=self.settings.queue_timeout,
            max_queue=self.settings.max_queue,
            state_path=data_dir / "usage.json",
        )
        self._chain = build_chain(self.settings)
        if not self._chain:
            logger.warning("[imggen] 没有可用的生图服务商（配置不完整），/画 将返回配置指引")
        try:
            self._ensure_tool_workers()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[imggen] 工具后台 worker 启动失败：{exc!r}")

    async def _http_session(self):
        if self._http is None or getattr(self._http, "closed", True):
            try:
                import aiohttp
            except ImportError as exc:  # pragma: no cover - AstrBot 自带
                raise ProviderError(f"缺少 aiohttp 依赖：{exc}", retryable=False) from None
            self._http = aiohttp.ClientSession()
        return self._http

    # ==================================================================
    # 上下文判断
    # ==================================================================

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _is_group(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.get_group_id())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _user_key(event: AstrMessageEvent) -> str:
        platform = ""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            platform = ""
        sender = ""
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sender = ""
        if not sender:
            try:
                sender = str(event.get_session_id() or "")
            except Exception:  # noqa: BLE001
                sender = str(getattr(event, "unified_msg_origin", "") or "anonymous")
        return f"{platform}:{sender}"

    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        try:
            return str(event.message_str or "")
        except Exception:  # noqa: BLE001
            return ""

    def _precheck(self, event: AstrMessageEvent) -> str:
        settings = self.settings
        if settings.admin_only and not self._is_admin(event):
            return "🔒 生图功能当前仅管理员可用。"
        is_group = self._is_group(event)
        if is_group and not settings.allow_group:
            return "🚫 生图功能暂未开放群聊使用。"
        if not is_group and not settings.allow_private:
            return "🚫 生图功能暂未开放私聊使用。"
        return ""

    @staticmethod
    def _claim(event: AstrMessageEvent) -> bool:
        """同一事件只处理一次，避免命令与关键词触发重复出图。"""
        try:
            if getattr(event, "_imggen_claimed", False):
                return False
            setattr(event, "_imggen_claimed", True)
        except Exception:  # noqa: BLE001 - 部分平台事件禁用属性写入
            return True
        return True

    def _check_prompt(self, prompt: str) -> str:
        lowered = prompt.lower()
        for word in self.settings.blocked_words:
            if word and word.lower() in lowered:
                return "🚫 提示词包含被禁止的内容，请换个描述。"
        return ""

    # ==================================================================
    # 请求解析
    # ==================================================================

    def _stylebook(self) -> StyleBook:
        self._ensure()
        assert self._styles is not None
        return self._styles

    def _parse_request(self, text: str) -> RequestSpec:
        book = self._stylebook()
        settings = self.settings
        raw = (text or "").strip()
        if not raw:
            return RequestSpec(error=USAGE)
        if raw.lower() in _HELP_WORDS:
            return RequestSpec(error=HELP_TEXT)

        style_name = ""
        count: int | None = None
        ratio_raw = ""
        notice = ""

        value, raw = _extract(_NUM_FLAG, raw)
        if value:
            try:
                count = int(value)
            except ValueError:
                count = None
        value, raw = _extract(_STYLE_FLAG, raw)
        if value:
            style_name = value
        value, raw = _extract(_RATIO_FLAG, raw)
        if value:
            ratio_raw = value

        if not style_name:
            value, raw = _extract(_INLINE_STYLE, raw)
            if value:
                style_name = value
        if not ratio_raw:
            value, raw = _extract(_INLINE_RATIO, raw)
            if value:
                ratio_raw = value

        # 首个词是风格名时视为显式指定风格（整词匹配，避免误吞句子）
        trimmed = raw.strip()
        tokens = trimmed.split()
        if tokens and not style_name:
            candidate = tokens[0].strip("，,。.！!？?：:")
            found = book.resolve_token(candidate)
            if found is not None:
                style_name = candidate
                trimmed = trimmed[len(tokens[0]):]

        prompt = sanitize(" ".join(trimmed.split()), settings.max_prompt_chars)
        if not prompt:
            return RequestSpec(error=USAGE)

        style: Style | None = None
        if style_name:
            style = book.resolve(style_name)
            if style is None:
                notice = f"未找到风格「{style_name}」，已按自动风格处理。"
        if style is None:
            style = book.detect(prompt)
        if style is None:
            style = book.get(settings.default_style)

        ratio = parse_ratio(ratio_raw or style.ratio or settings.default_ratio, settings.default_ratio)
        return RequestSpec(
            prompt=prompt,
            style=style,
            ratio=ratio,
            count=settings.allowed_images(count),
            notice=notice,
        )

    # ==================================================================
    # 生成
    # ==================================================================

    def _config_hint(self) -> str:
        return (
            "⚠️ 还没有可用的生图服务。请到 AstrBot WebUI → 插件 → 绘影生图 中填写：\n"
            "provider（openai / gemini / dashscope）、api_key、model；base_url 可留空用默认。\n"
            "想先体验可把 provider 设为 mock（离线占位图，不联网）。"
        )

    # ---------- 图生图参考图 ----------

    @staticmethod
    def _reference_name(mime: str) -> str:
        ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
        return f"reference.{ext}"

    @classmethod
    def _validate_reference(cls, data: bytes) -> tuple[bytes, str] | None:
        if not data or len(data) > _MAX_REFERENCE_BYTES:
            return None
        detected = sniff_image(data)
        if not detected:
            return None
        _ext, mime = detected
        if mime not in _ALLOWED_REFERENCE_MIMES:
            return None
        return data, mime

    async def _load_reference_source(self, value: str) -> ReferenceImage:
        """把 QQ 图片组件里的路径 / URL / base64 读成参考图。"""
        text = str(value or "").strip()
        if not text:
            raise ProviderError("参考图地址为空", retryable=False)
        if text.startswith("base64://"):
            text = "data:image/png;base64," + text[len("base64://"):]
        if text.startswith("data:"):
            try:
                data = decode_base64(text)
            except ProviderError as exc:
                raise ProviderError(f"参考图解码失败：{exc.message}", retryable=False) from None
            return self._make_reference(data)

        path_text = text[7:] if text.startswith("file://") else text
        if path_text.lower().startswith(("http://", "https://")):
            try:
                session = await self._http_session()
                data = await download_image(
                    session,
                    path_text,
                    max_bytes=_MAX_REFERENCE_BYTES,
                    timeout=self.settings.request_timeout,
                    secret=self.settings.primary.api_key,
                )
            except ProviderError as exc:
                raise ProviderError(f"参考图下载失败：{exc.message}", retryable=False) from None
            return self._make_reference(data)

        try:
            raw_path = Path(path_text)
        except (OSError, ValueError):
            raise ProviderError("参考图路径无效", retryable=False) from None
        candidates = (
            [raw_path]
            if raw_path.is_absolute()
            else [Path.cwd() / raw_path, self._data_dir() / raw_path]
        )
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                if candidate.stat().st_size > _MAX_REFERENCE_BYTES:
                    raise ProviderError("参考图过大（最大 10MB）", retryable=False)
                data = await asyncio.to_thread(candidate.read_bytes)
            except OSError:
                continue
            return self._make_reference(data)
        raise ProviderError("参考图读取失败：文件不存在或无法访问", retryable=False)

    def _make_reference(self, data: bytes) -> ReferenceImage:
        checked = self._validate_reference(data)
        if checked is None:
            raise ProviderError(
                "参考图格式不支持：请发送 PNG / JPEG / WebP，且不超过 10MB",
                retryable=False,
            )
        data, mime = checked
        return ReferenceImage(data=data, mime=mime, name=self._reference_name(mime))

    def _parse_reference_payload(self, value: str) -> ReferenceImage:
        """解析工作台上传的 data URI / base64 参考图。"""
        text = str(value or "").strip()
        if not text:
            raise ProviderError("参考图为空", retryable=False)
        if text.startswith("base64://"):
            text = "data:image/png;base64," + text[len("base64://"):]
        if not text.startswith("data:"):
            text = "data:image/png;base64," + text
        if len(text) > _MAX_REFERENCE_BYTES * 2:
            raise ProviderError("参考图过大（最大 10MB）", retryable=False)
        try:
            data = decode_base64(text)
        except ProviderError as exc:
            raise ProviderError(f"参考图解码失败：{exc.message}", retryable=False) from None
        return self._make_reference(data)

    async def _extract_reference_image(
        self, event: AstrMessageEvent
    ) -> ReferenceImage | None:
        """从消息组件里取第一张图片作为图生图参考。"""
        try:
            message = getattr(getattr(event, "message_obj", None), "message", None)
        except Exception:  # noqa: BLE001
            message = None
        if message is None or isinstance(message, (str, bytes)):
            return None
        try:
            components = list(message)
        except TypeError:
            return None
        for comp in components:
            if comp is None:
                continue
            if not isinstance(comp, Image) and comp.__class__.__name__ != "Image":
                continue
            for attr in ("path", "file", "url"):
                value = getattr(comp, attr, "") or ""
                if value:
                    return await self._load_reference_source(str(value))
        return None

    @staticmethod
    def _provider_key(provider) -> str:
        return "|".join(
            (
                str(getattr(provider, "kind", "")),
                str(getattr(provider, "base_url", "")),
                str(getattr(provider, "model", "")),
            )
        )

    @staticmethod
    def _should_trip_breaker(exc: ProviderError) -> bool:
        """超时虽然不重试（防重复计费），但仍应计入熔断。"""
        if exc.retryable:
            return True
        if exc.status is not None and (exc.status == 429 or exc.status >= 500):
            return True
        return "超时" in (exc.message or "")

    async def _create_images(
        self,
        prompt: str,
        negative: str,
        count: int,
        ratio: str,
        *,
        reference: ReferenceImage | None = None,
    ) -> list[ImageResult]:
        self._ensure()
        if not self._chain:
            raise ProviderError(self._config_hint(), retryable=False)

        session = await self._http_session()
        errors: list[tuple[str, ProviderError]] = []
        for provider in self._chain:
            if reference is not None and not getattr(provider, "supports_edit", False):
                errors.append(
                    (
                        provider.kind,
                        ProviderError(
                            "当前服务商不支持图生图（仅 OpenAI 兼容 / mock）",
                            retryable=False,
                        ),
                    )
                )
                if not self.settings.fallback_on_error:
                    break
                continue
            key = self._provider_key(provider)
            if not self._breaker.allow(key):
                wait = int(self._breaker.retry_after(key)) + 1
                errors.append(
                    (
                        provider.kind,
                        ProviderError(
                            f"该服务商连续失败，已熔断约 {wait} 秒", retryable=False
                        ),
                    )
                )
                if not self.settings.fallback_on_error:
                    break
                continue
            try:
                if reference is not None:
                    results = await provider.edit(
                        session,
                        prompt=prompt,
                        negative=negative,
                        n=count,
                        ratio=ratio,
                        image=reference,
                    )
                else:
                    results = await provider.generate(
                        session, prompt=prompt, negative=negative, n=count, ratio=ratio
                    )
                if results:
                    self._breaker.record_success(key)
                    return results
                errors.append((provider.kind, ProviderError("返回了空结果")))
            except ProviderError as exc:
                if self._should_trip_breaker(exc):
                    self._breaker.record_failure(key)
                logger.warning(f"[imggen] 服务商 {provider.kind} 生成失败：{exc.message}")
                errors.append((provider.kind, exc))
                if not self.settings.fallback_on_error:
                    break
            except Exception as exc:  # noqa: BLE001 - 适配器不应抛出其它异常
                self._breaker.record_failure(key)
                logger.error(f"[imggen] 服务商 {provider.kind} 异常：{exc!r}")
                errors.append((provider.kind, ProviderError(f"内部错误：{exc.__class__.__name__}")))
                if not self.settings.fallback_on_error:
                    break

        if not errors:
            raise ProviderError("生成失败（无可用服务商）", retryable=False)
        tried = " / ".join(kind for kind, _ in errors)
        last = errors[-1][1]
        raise ProviderError(
            f"{last.message}（已尝试：{tried}）",
            retryable=last.retryable,
            status=last.status,
        )

    def _save_results(self, results: list[ImageResult]) -> list[Path]:
        return [path for path, _result in self._save_pairs(results)]

    async def _send_text(self, umo: str, text: str) -> bool:
        """按会话主动发送纯文本（工具后台任务用）。"""
        if not umo or not text:
            return False
        try:
            try:
                chain = MessageChain().message(text)
            except Exception:  # noqa: BLE001 - 老版本退回列表构造
                chain = MessageChain([Plain(text)])
            ok = await asyncio.wait_for(
                self.context.send_message(umo, chain), timeout=20
            )
            if not ok:
                logger.warning(f"[imggen] 主动发送文本返回 False：{umo}")
            return bool(ok)
        except asyncio.TimeoutError:
            logger.warning("[imggen] 主动发送文本超时（20s）")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[imggen] 主动发送文本失败：{exc!r}")
            return False

    async def _send_tool_result(
        self, umo: str, paths: list[Path], style: Style, elapsed: float
    ) -> bool:
        """工具后台完成后的图文回包：说明文字 + 图片。"""
        caption = f"🎨 绘制完成 · {style.name} · {elapsed:.1f}s"
        if len(paths) > 1:
            caption += f" · {len(paths)} 张"
        try:
            try:
                chain = MessageChain().message(caption)
                for path in paths:
                    chain.file_image(str(path))
            except Exception:  # noqa: BLE001
                chain = MessageChain(
                    [Plain(caption)]
                    + [Image.fromFileSystem(str(path)) for path in paths]
                )
            ok = await asyncio.wait_for(
                self.context.send_message(umo, chain), timeout=30
            )
            if not ok:
                logger.warning(f"[imggen] 主动发送结果返回 False：{umo}")
            return bool(ok)
        except asyncio.TimeoutError:
            logger.warning("[imggen] 主动发送结果超时（30s）")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[imggen] 主动发送结果失败：{exc!r}")
            return False

    # ---------- 工具后台 worker 池 ----------

    def _ensure_tool_workers(self) -> asyncio.Queue:
        """启动/补齐常驻 worker 池（大小 = max_concurrency）。"""
        if self._tool_queue is None:
            self._tool_queue = asyncio.Queue()
        if self._stopping:
            return self._tool_queue
        try:
            size = max(1, int(self.settings.max_concurrency))
        except Exception:  # noqa: BLE001
            size = 1
        self._tool_workers = [task for task in self._tool_workers if not task.done()]
        for _ in range(max(0, size - len(self._tool_workers))):
            task = asyncio.create_task(self._tool_worker_loop())
            task.add_done_callback(self._on_tool_worker_done)
            self._tool_workers.append(task)
        return self._tool_queue

    def _on_tool_worker_done(self, task: asyncio.Task) -> None:
        self._tool_workers = [
            item for item in self._tool_workers if item is not task and not item.done()
        ]
        if self._stopping:
            return
        if task.cancelled():
            logger.warning("[imggen] 工具后台 worker 意外被取消")
        else:
            exc = task.exception()
            if exc is not None:
                logger.error(f"[imggen] 工具后台 worker 退出：{exc!r}")
        now = time.monotonic()
        if now - self._worker_restart_at < 5.0:
            self._worker_restarts += 1
        else:
            self._worker_restarts = 1
        self._worker_restart_at = now
        if self._worker_restarts > 3:
            logger.error("[imggen] 工具后台 worker 反复退出，暂停自动重启")
            return
        try:
            self._ensure_tool_workers()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 工具后台 worker 重启失败：{exc!r}")

    async def _tool_worker_loop(self) -> None:
        queue = self._tool_queue
        if queue is None:
            return
        logger.info("[imggen] 工具后台 worker 启动")
        while True:
            job = await queue.get()
            job_id = str(job.get("job_id") or "")
            record = self._tool_jobs.get(job_id)
            if record is not None and record.get("status") == "cancelled":
                queue.task_done()
                continue
            if record is not None:
                record["status"] = "running"
                record["started_at"] = time.time()
                if record.get("cancel_requested"):
                    record["status"] = "cancelled"
                    record["finished_at"] = time.time()
                    queue.task_done()
                    continue
            task = asyncio.create_task(self._run_tool_generation(**job))
            if record is not None:
                record["task"] = task
            task.add_done_callback(
                lambda done, jid=job_id: self._on_job_task_done(jid, done)
            )
            try:
                # asyncio.wait 不会把“任务被取消”的异常抛给 worker
                await asyncio.wait({task})
            except asyncio.CancelledError:
                queue.task_done()
                raise
            queue.task_done()
            self._worker_restarts = 0

    def _on_job_task_done(self, job_id: str, task: asyncio.Task) -> None:
        record = self._tool_jobs.get(job_id)
        if record is None:
            return
        record["finished_at"] = time.time()
        record["task"] = None
        if task.cancelled():
            record["status"] = "cancelled"
            return
        exc = task.exception()
        if exc is not None:
            record["status"] = "failed"
            record["error"] = record.get("error") or str(exc)
            logger.error(f"[imggen] 工具任务 #{job_id} 异常：{exc!r}")
            return
        try:
            ok = bool(task.result())
        except Exception:  # noqa: BLE001
            ok = False
        record["status"] = "success" if ok else "failed"
        if ok:
            record["error"] = ""

    # ---------- 工具任务登记 ----------

    def _new_job_id(self) -> str:
        while True:
            candidate = secrets.token_hex(3)
            if candidate not in self._tool_jobs:
                return candidate

    def _prune_jobs(self) -> None:
        if len(self._tool_jobs) <= 60:
            return
        finished = sorted(
            (
                (job_id, job)
                for job_id, job in self._tool_jobs.items()
                if job.get("status") in ("success", "failed", "cancelled")
                and not job.get("task")
            ),
            key=lambda item: item[1].get("finished_at") or 0.0,
        )
        for job_id, _job in finished[: max(0, len(self._tool_jobs) - 60)]:
            self._tool_jobs.pop(job_id, None)

    def _visible_jobs(self, event: AstrMessageEvent) -> list[dict]:
        user_key = self._user_key(event)
        admin = self._is_admin(event)
        jobs = [
            job
            for job in self._tool_jobs.values()
            if admin or job.get("user_key") == user_key
        ]
        jobs.sort(key=lambda job: job.get("created_at", 0.0), reverse=True)
        return jobs

    @staticmethod
    def _job_status_text(status: str) -> str:
        return {
            "queued": "排队中",
            "running": "生成中",
            "success": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
        }.get(status, status)

    def _format_job(self, job: dict) -> str:
        age = max(0, int(time.time() - float(job.get("created_at") or time.time())))
        line = (
            f"#{job['id']} {self._job_status_text(job['status'])}"
            f" · {job.get('style') or '-'} · {job.get('count') or 1} 张 · {age}s"
        )
        if job.get("error"):
            line += " · " + truncate(str(job["error"]), 60)
        return line

    async def _tool_heartbeat(
        self, umo: str, started: float, done: asyncio.Event
    ) -> None:
        """长时间生成时每 60 秒告知用户仍在处理。"""
        while True:
            try:
                await asyncio.wait_for(done.wait(), timeout=60)
                return
            except asyncio.TimeoutError:
                elapsed = int(time.monotonic() - started)
                logger.info(f"[imggen] 工具生成仍在进行：{elapsed}s")
                await self._send_text(
                    umo, f"🖌️ 图片仍在生成中（已等待 {elapsed} 秒）…"
                )
            except asyncio.CancelledError:
                raise

    async def _run_tool_generation(
        self,
        *,
        job_id: str = "",
        umo: str,
        user_key: str,
        style: Style,
        text: str,
        ratio: str,
        count: int,
    ) -> bool:
        """工具后台生成任务：不受 AstrBot 工具 120s 超时限制，返回是否成功。"""
        record = self._tool_jobs.get(job_id)

        def fail(message: str) -> bool:
            if record is not None:
                record["error"] = message
            return False

        limiter = self._limiter
        if limiter is None:
            message = "❌ 生成失败：插件尚未初始化完成，请稍后重试。"
            await self._send_text(umo, message)
            return fail(message)
        try:
            await limiter.acquire(user_key)
        except LimitError as exc:
            await self._send_text(umo, f"⏳ {exc.message}")
            return fail(exc.message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 工具限流器异常：{exc!r}")
            message = "❌ 生成失败：限流器异常，请查看 AstrBot 日志。"
            await self._send_text(umo, message)
            return fail(message)

        success = False
        started = time.monotonic()
        logger.info(
            f"[imggen] 工具生成开始：#{job_id} {umo} "
            f"style={style.name} count={count} ratio={ratio}"
        )
        done = asyncio.Event()
        heartbeat = asyncio.create_task(self._tool_heartbeat(umo, started, done))
        try:
            settings = self.settings
            negative_hint = settings.negative_as_hint and not (
                self._chain and getattr(self._chain[0], "supports_negative", False)
            )
            final_prompt, negative = build_prompt(
                style, text, negative_as_hint=negative_hint
            )
            budget = max(
                180.0,
                min(
                    420.0,
                    settings.request_timeout * (max(1, len(self._chain)) + 1) + 30,
                ),
            )
            try:
                results = await asyncio.wait_for(
                    self._create_images(final_prompt, negative, count, ratio),
                    timeout=budget,
                )
            except asyncio.TimeoutError:
                raise ProviderError(
                    f"生成超时（超过 {int(budget)} 秒），请稍后重试或换更快的模型/服务商",
                    retryable=False,
                ) from None
            paths = await asyncio.to_thread(self._save_results, results)
            elapsed = time.monotonic() - started
            sent = await self._send_tool_result(umo, paths, style, elapsed)
            if not sent:
                await self._send_text(
                    umo,
                    "图片已生成，但主动发送失败；文件已保存："
                    + "、".join(str(p) for p in paths),
                )
            success = True
            return True
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            if record is not None:
                record["error"] = exc.message
            await self._send_text(umo, self._friendly_error(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 工具生图失败：{exc!r}")
            message = "❌ 生成失败：插件内部错误，请查看 AstrBot 日志。"
            await self._send_text(umo, message)
            return fail(message)
        finally:
            done.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            limiter.release(user_key, success=success)
            logger.info(
                f"[imggen] 工具生成结束：#{job_id} success={success} "
                f"elapsed={time.monotonic() - started:.1f}s"
            )
            if success:
                self._maybe_cleanup()

    def _friendly_error(self, exc: ProviderError) -> str:
        # 配置指引类消息本身就是给用户看的，不再套一层“生成失败”
        if exc.message.startswith(("⚠️", "🔒", "🚫", "⏳")):
            return exc.message
        if "不是合法 JSON" in exc.message or "响应不完整" in exc.message or "下载不完整" in exc.message:
            return (
                "❌ 生成失败：服务商返回的数据不完整或格式异常（常见于中转站截断响应）。"
                "请重试；若持续出现，请检查 base_url 或更换服务商。\n"
                f"详情：{truncate(exc.message, 160)}"
            )
        status = exc.status
        mapping = {
            400: "请求被服务商拒绝（提示词可能触发内容审核，或参数不被该模型支持）",
            401: "鉴权失败：请检查 API Key 是否正确、是否已开通该模型",
            403: "没有权限：该 Key 可能未开通此模型，或来源 IP 受限",
            404: "接口或模型不存在：请检查 base_url 与 model 配置",
            429: "服务商限流或额度不足，请稍后再试",
        }
        head = mapping.get(status or 0, "")
        if not head and status and status >= 500:
            head = "服务商暂时不可用，请稍后重试"
        detail = truncate(exc.message, 300)
        if head and detail and head not in detail:
            return f"❌ 生成失败：{head}\n详情：{truncate(detail, 180)}"
        return f"❌ 生成失败：{detail or head or '未知错误'}"

    def _build_success_result(
        self,
        event: AstrMessageEvent,
        paths: list[Path],
        style: Style,
        results: list[ImageResult],
        final_prompt: str,
        elapsed: float,
        notice: str = "",
        edited: bool = False,
    ):
        first = results[0] if results else None
        dims = f"{first.width}x{first.height}" if first and first.width else ""
        caption = f"🎨 {'图生图完成' if edited else '绘制完成'} · {style.name}"
        if dims:
            caption += f" · {dims}"
        caption += f" · {elapsed:.1f}s"
        if len(paths) > 1:
            caption += f" · {len(paths)} 张"

        chain = []
        if self.settings.show_caption:
            chain.append(Plain(caption))
        if self.settings.show_prompt and final_prompt:
            chain.append(Plain("提示词：" + truncate(final_prompt, 300)))
        if notice:
            chain.append(Plain(notice))
        for path in paths:
            chain.append(Image.fromFileSystem(str(path)))

        try:
            return event.chain_result(chain)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 组装图片消息失败，退回单图发送：{exc!r}")
            try:
                return event.image_result(str(paths[0]))
            except Exception:  # noqa: BLE001
                return event.plain_result(caption)

    def _maybe_cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < 1800:
            return
        self._last_cleanup = now
        try:
            asyncio.get_running_loop().create_task(self._cleanup_async())
        except RuntimeError:  # pragma: no cover - 无运行中循环
            pass

    async def _cleanup_async(self) -> None:
        try:
            if self._store is not None:
                info = await asyncio.to_thread(self._store.cleanup)
                if info.get("deleted"):
                    logger.info(f"[imggen] 自动清理：删除 {info['deleted']} 张旧图")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[imggen] 自动清理失败：{exc!r}")

    async def _stream(self, event: AstrMessageEvent, args: str, trigger: str = ""):
        """命令与关键词触发的共同生成流程（异步生成器，逐条产出结果）。"""
        if trigger:
            logger.info(f"[imggen] 关键词触发：{trigger}")
        precheck = self._precheck(event)
        if precheck:
            yield event.plain_result(precheck)
            return

        try:
            reference = await self._extract_reference_image(event)
        except ProviderError as exc:
            yield event.plain_result(self._friendly_error(exc))
            return
        if reference is not None and not args.strip():
            args = _DEFAULT_EDIT_PROMPT

        spec = self._parse_request(args)
        if spec.error:
            yield event.plain_result(spec.error)
            return

        block = self._check_prompt(spec.prompt)
        if block:
            yield event.plain_result(block)
            return

        style = spec.style or self._stylebook().get(self.settings.default_style)
        self._ensure()
        limiter = self._limiter
        if limiter is None:  # 防御性：理论上 _ensure 后一定存在
            yield event.plain_result("❌ 插件尚未初始化完成，请稍后重试。")
            return
        user_key = self._user_key(event)

        try:
            await limiter.acquire(user_key)
        except LimitError as exc:
            yield event.plain_result(f"⏳ {exc.message}")
            return

        success = False
        started = time.monotonic()
        try:
            if self.settings.show_progress:
                action = "改图" if reference is not None else "绘制"
                yield event.plain_result(
                    f"🎨 正在{action} · {style.name} · {spec.ratio} · {spec.count} 张…"
                )

            negative_hint = self.settings.negative_as_hint and not (
                self._chain and getattr(self._chain[0], "supports_negative", False)
            )
            final_prompt, negative = build_prompt(
                style, spec.prompt, negative_as_hint=negative_hint
            )
            results = await self._create_images(
                final_prompt, negative, spec.count, spec.ratio, reference=reference
            )
            paths = await asyncio.to_thread(self._save_results, results)
            elapsed = time.monotonic() - started
            success = True
            yield self._build_success_result(
                event,
                paths,
                style,
                results,
                final_prompt,
                elapsed,
                spec.notice,
                edited=reference is not None,
            )
        except ProviderError as exc:
            yield event.plain_result(self._friendly_error(exc))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            yield event.plain_result("⏱️ 生成超时，请稍后重试或换用更快的模型。")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 生成流程异常：{exc!r}")
            yield event.plain_result("❌ 生成失败：插件内部错误，已记录日志。")
        finally:
            limiter.release(user_key, success=success)
            if success:
                self._maybe_cleanup()

    # ==================================================================
    # 插件页面（WebUI 工作台）
    # ==================================================================

    def _register_web_apis(self, context) -> None:
        """注册 WebUI 页面后端。旧版 AstrBot 没有该 API 时安全跳过。"""
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            logger.info("[imggen] 当前 AstrBot 不支持插件 Web API，跳过界面后端")
            return
        apis = [
            ("state", self.api_state, ["GET"], "工作台状态"),
            ("settings", self.api_settings, ["GET"], "读取可编辑设置"),
            ("save_settings", self.api_save_settings, ["POST"], "保存服务商与默认设置"),
            ("generate", self.api_generate, ["POST"], "生成图片"),
            ("history", self.api_history, ["GET"], "最近生成"),
            ("thumb", self.api_thumb, ["GET"], "缩略图"),
            ("image", self.api_image, ["GET"], "下载原图"),
            ("cleanup", self.api_cleanup, ["POST"], "清理旧图"),
            ("reload", self.api_reload, ["POST"], "重载配置"),
        ]
        registered = 0
        for endpoint, handler, methods, desc in apis:
            route = f"/{PLUGIN_NAME}/{endpoint}"
            try:
                register(route, handler, methods, desc)
                registered += 1
            except TypeError:
                try:
                    register(route=route, view_handler=handler, methods=methods, desc=desc)
                    registered += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[imggen] 注册接口 {endpoint} 失败：{exc!r}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[imggen] 注册接口 {endpoint} 失败：{exc!r}")
        logger.info(f"[imggen] 已注册 {registered} 个界面接口")

    # ---------- 请求/响应小工具 ----------

    @staticmethod
    def _web_ok(data: dict) -> dict:
        return {"status": "ok", "data": data}

    @staticmethod
    def _web_err(message: str) -> dict:
        return {"status": "error", "message": message}

    @staticmethod
    def _req():
        if _web_request is not None:
            return _web_request
        try:
            from quart import request  # type: ignore

            return request
        except Exception:  # noqa: BLE001
            return None

    async def _json_body(self) -> dict:
        req = self._req()
        if req is None:
            return {}
        try:
            try:
                payload = await req.json(default={})
            except TypeError:
                payload = await req.get_json()
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    def _query(self, name: str, default=None):
        req = self._req()
        if req is None:
            return default
        try:
            source = getattr(req, "query", None)
            if source is None:
                source = getattr(req, "args", None)
        except Exception:  # noqa: BLE001 - 无请求上下文
            return default
        if source is None:
            return default
        try:
            value = source.get(name, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def _query_int(self, name: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(self._query(name, default))
        except (TypeError, ValueError):
            value = default
        return max(lo, min(hi, value))

    def _query_str(self, name: str, default: str = "") -> str:
        return str(self._query(name, default) or default)

    async def _serve_file(self, path: Path):
        if _web_file_response is not None:
            try:
                return await _web_file_response(str(path), filename=path.name)
            except TypeError:
                return await _web_file_response(str(path))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[imggen] file_response 失败，改用 Quart：{exc!r}")
        data = await asyncio.to_thread(path.read_bytes)
        mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
        try:
            from quart import Response  # type: ignore

            return Response(
                data,
                mimetype=mime,
                headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
            )
        except Exception:  # noqa: BLE001
            return self._web_err("当前运行环境不支持文件下载")

    # ---------- 文件与数据小工具 ----------

    def _resolve_image_path(self, name: str) -> Path:
        name = str(name or "").strip()
        if not _SAFE_IMAGE_NAME.match(name):
            raise ValueError("非法的图片名")
        if self._store is None:
            self._ensure()
        store = self._store
        if store is None:
            raise ValueError("本地存储未初始化")
        root = store.root.resolve()
        path = (root / name).resolve()
        if root != path.parent and root not in path.parents:
            raise ValueError("非法的图片路径")
        if not path.is_file():
            raise ValueError("图片不存在或已被清理")
        return path

    def _make_thumb_data_uri(self, path: Path, max_edge: int = 900) -> str:
        """生成页面用缩略图 data URI；Pillow 不可用时退回原图。"""
        try:
            import io

            from PIL import Image as PILImage

            with PILImage.open(path) as source:
                rgba = source.convert("RGBA")
                # 透明背景合成到白底，避免缩略图发黑
                image = PILImage.new("RGB", rgba.size, (255, 255, 255))
                image.paste(rgba, mask=rgba.split()[-1])
            image.thumbnail((max_edge, max_edge))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=85)
            return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
        except Exception:  # noqa: BLE001
            try:
                data = path.read_bytes()
                if len(data) <= 2 * 1024 * 1024:
                    mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "image/png")
                    return f"data:{mime};base64," + base64.b64encode(data).decode()
            except OSError:
                pass
            return ""

    def _list_recent_images(self, limit: int = 12) -> list[dict]:
        store = self._store
        if store is None or not store.root.exists():
            return []
        records: list[tuple[float, Path, int]] = []
        for path in store.root.rglob("*"):
            if not path.is_file() or path.suffix == ".part":
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            records.append((stat.st_mtime, path, stat.st_size))
        records.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "name": f"{path.parent.name}/{path.name}",
                "size": size,
                "time": datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M"),
            }
            for mtime, path, size in records[:limit]
        ]

    def _save_pairs(self, results: list[ImageResult]) -> list[tuple[Path, ImageResult]]:
        """保存图片并保留与 Provider 结果的对应关系。"""
        store = self._store
        if store is None:
            raise ProviderError("本地存储尚未初始化，请稍后重试", retryable=False)
        pairs: list[tuple[Path, ImageResult]] = []
        for result in results:
            try:
                pairs.append((store.save(result.content), result))
            except (ValueError, OSError) as exc:
                logger.warning(f"[imggen] 丢弃一张无效/不可写图片：{exc}")
        if not pairs:
            raise ProviderError("图片校验失败：返回内容不是有效图片", retryable=False)
        return pairs

    def _apply_reload(self) -> None:
        """重读配置与自定义风格（命令与界面共用）。"""
        self.settings = RuntimeSettings.from_mapping(self.config)
        inflight = self._limiter.inflight_count() if self._limiter else 0
        if inflight == 0:
            self._limiter = None
        else:
            logger.info("[imggen] 有请求进行中，限流参数将在空闲后生效")
        self._styles = None
        self._store = None
        self._chain = []
        self._breaker.reset()
        self._ensure()

    # ---------- 接口实现 ----------

    async def api_state(self):
        try:
            self._ensure()
            settings = self.settings
            stats = await asyncio.to_thread(self._store.stats) if self._store else {}
            recent = await asyncio.to_thread(self._list_recent_images, 12)
            styles = (
                [
                    {
                        "id": style.id,
                        "name": style.name,
                        "category": style.category,
                        "summary": style.summary,
                        "ratio": style.ratio,
                        "prefix": style.prefix,
                        "suffix": style.suffix,
                        "negative": style.negative,
                        "aliases": list(style.aliases),
                    }
                    for style in self._styles.styles.values()
                ]
                if self._styles
                else []
            )
            backup = None
            if settings.backup is not None:
                backup = {
                    "kind": settings.backup.kind,
                    "model": settings.backup.resolved_model(),
                    "base_url": settings.backup.resolved_base_url(),
                    "ready": settings.backup.ready(),
                }
            return self._web_ok(
                {
                    "version": PLUGIN_VERSION,
                    "provider": {
                        "kind": settings.primary.kind,
                        "model": settings.primary.resolved_model(),
                        "base_url": settings.primary.resolved_base_url(),
                        "ready": settings.primary.ready(),
                        "edit": any(
                            getattr(provider, "supports_edit", False)
                            for provider in self._chain
                        ),
                    },
                    "backup": backup,
                    "settings": {
                        "default_ratio": settings.default_ratio,
                        "default_style": settings.default_style,
                        "max_images": settings.max_images,
                        "default_images": settings.default_images,
                        "max_prompt_chars": settings.max_prompt_chars,
                        "show_prompt": settings.show_prompt,
                    },
                    "styles": styles,
                    "storage": stats,
                    "history": recent,
                    "queue": self._limiter.snapshot() if self._limiter else {},
                    "breaker": self._breaker.snapshot(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 界面状态读取失败：{exc!r}")
            return self._web_err("状态读取失败，请查看 AstrBot 日志")

    async def api_settings(self):
        """返回工作台可编辑的设置；密钥只返回掩码。"""
        try:
            self._ensure()
            settings = self.settings
            book = self._stylebook()

            raw_provider = as_str(
                _config_value(self.config, "provider", settings.primary.kind)
            ).lower()
            provider = (
                raw_provider if raw_provider in PROVIDER_KINDS else settings.primary.kind
            )
            raw_model = as_str(_config_value(self.config, "model", ""))
            raw_base = as_str(_config_value(self.config, "base_url", ""))

            raw_backup_provider = as_str(
                _config_value(self.config, "backup_provider", "mock")
            ).lower()
            backup_provider = (
                raw_backup_provider
                if raw_backup_provider in PROVIDER_KINDS
                else "mock"
            )
            backup_key = (
                settings.backup.api_key
                if settings.backup is not None
                else as_str(_config_value(self.config, "backup_api_key", ""))
                or as_str(os.environ.get("IMGGEN_BACKUP_API_KEY", ""))
            )

            styles = [
                {"id": style.id, "name": style.name, "category": style.category}
                for style in book.styles.values()
            ]
            providers = [
                {
                    "id": kind,
                    "label": _PROVIDER_LABELS.get(kind, kind),
                    "default_model": DEFAULT_MODELS.get(kind, ""),
                    "default_base_url": (
                        "" if kind == "mock" else DEFAULT_BASE_URLS.get(kind, "")
                    ),
                    "models": list(_PROVIDER_MODELS.get(kind, ())),
                    "needs_key": kind != "mock",
                }
                for kind in PROVIDER_KINDS
            ]
            return self._web_ok(
                {
                    "provider": provider,
                    "model": raw_model or DEFAULT_MODELS.get(provider, ""),
                    "base_url": raw_base
                    or (
                        ""
                        if provider == "mock"
                        else DEFAULT_BASE_URLS.get(provider, "")
                    ),
                    "api_key_masked": _mask_secret(settings.primary.api_key),
                    "has_api_key": bool(settings.primary.api_key),
                    "quality": settings.primary.quality or "auto",
                    "openai_response_format": settings.openai_response_format,
                    "backup_enabled": as_bool(
                        _config_value(self.config, "backup_enabled", False), False
                    ),
                    "backup_provider": backup_provider,
                    "backup_model": as_str(
                        _config_value(self.config, "backup_model", "")
                    )
                    or DEFAULT_MODELS.get(backup_provider, ""),
                    "backup_base_url": as_str(
                        _config_value(self.config, "backup_base_url", "")
                    )
                    or (
                        ""
                        if backup_provider == "mock"
                        else DEFAULT_BASE_URLS.get(backup_provider, "")
                    ),
                    "backup_api_key_masked": _mask_secret(backup_key),
                    "backup_has_api_key": bool(backup_key),
                    "default_style": settings.default_style,
                    "default_ratio": settings.default_ratio,
                    "default_images": settings.default_images,
                    "max_images": settings.max_images,
                    "ratios": list(RATIOS.keys()),
                    "styles": styles,
                    "providers": providers,
                    "quality_options": list(_QUALITY_OPTIONS),
                    "response_formats": list(_RESPONSE_FORMATS),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 设置读取失败：{exc!r}")
            return self._web_err("设置读取失败，请查看 AstrBot 日志")

    async def api_save_settings(self):
        """保存服务商/模型/密钥与生成默认值，并立即重载。"""
        try:
            payload = await self._json_body()
            self._ensure()
            settings = self.settings
            book = self._stylebook()

            provider = as_str(payload.get("provider", settings.primary.kind)).lower()
            if provider not in PROVIDER_KINDS:
                return self._web_err(f"不支持的服务商：{truncate(provider, 40)}")

            model = as_str(payload.get("model", ""))[:200]
            base_url = as_str(payload.get("base_url", ""))[:500]
            if base_url and not base_url.lower().startswith(("http://", "https://")):
                return self._web_err("接口地址必须以 http:// 或 https:// 开头（留空使用官方默认）")

            api_key = as_str(payload.get("api_key", ""))
            if len(api_key) > 500:
                return self._web_err("API Key 过长，请检查是否粘贴了多余内容")
            clear_key = as_bool(payload.get("clear_api_key", False), False)

            quality = as_str(payload.get("quality", settings.primary.quality or "auto")).lower()
            if quality not in _QUALITY_OPTIONS:
                return self._web_err("画质选项不合法")
            response_format = as_str(
                payload.get("openai_response_format", settings.openai_response_format)
            ).lower()
            if response_format not in _RESPONSE_FORMATS:
                return self._web_err("返回格式不合法")

            backup_enabled = as_bool(payload.get("backup_enabled", False), False)
            backup_provider = as_str(payload.get("backup_provider", "mock")).lower()
            if backup_provider not in PROVIDER_KINDS:
                return self._web_err(f"不支持的备用服务商：{truncate(backup_provider, 40)}")
            backup_model = as_str(payload.get("backup_model", ""))[:200]
            backup_base_url = as_str(payload.get("backup_base_url", ""))[:500]
            if backup_base_url and not backup_base_url.lower().startswith(
                ("http://", "https://")
            ):
                return self._web_err("备用接口地址必须以 http:// 或 https:// 开头")
            backup_api_key = as_str(payload.get("backup_api_key", ""))
            if len(backup_api_key) > 500:
                return self._web_err("备用 API Key 过长，请检查是否粘贴了多余内容")
            clear_backup_key = as_bool(payload.get("clear_backup_api_key", False), False)

            raw_style = as_str(payload.get("default_style", settings.default_style))
            style = book.resolve(raw_style) if raw_style else book.get(settings.default_style)
            if style is None:
                return self._web_err(f"未知风格：{truncate(raw_style, 40)}")

            raw_ratio = as_str(payload.get("default_ratio", settings.default_ratio))
            ratio = parse_ratio(raw_ratio, "") if raw_ratio else settings.default_ratio
            if not ratio:
                return self._web_err(f"不支持的比例：{truncate(raw_ratio, 20)}")

            default_images = as_int(
                payload.get("default_images", settings.default_images),
                settings.default_images,
                1,
                settings.max_images,
            )

            updates = {
                "provider": provider,
                "model": model,
                "base_url": base_url,
                "quality": quality,
                "openai_response_format": response_format,
                "backup_enabled": backup_enabled,
                "backup_provider": backup_provider,
                "backup_model": backup_model,
                "backup_base_url": backup_base_url,
                "default_style": style.id,
                "default_ratio": ratio,
                "default_images": default_images,
            }
            if api_key:
                updates["api_key"] = api_key
            elif clear_key:
                updates["api_key"] = ""
            if backup_api_key:
                updates["backup_api_key"] = backup_api_key
            elif clear_backup_key:
                updates["backup_api_key"] = ""

            try:
                for key, value in updates.items():
                    self.config[key] = value
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[imggen] 写入插件配置失败：{exc!r}")
                return self._web_err("写入插件配置失败，请查看 AstrBot 日志")

            persisted = True
            warning = ""
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                try:
                    if asyncio.iscoroutinefunction(saver):
                        await saver()
                    else:
                        await asyncio.to_thread(saver)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[imggen] 配置文件保存失败：{exc!r}")
                    persisted = False
                    warning = "设置已生效，但写回配置文件失败，重启后可能丢失"
            else:
                persisted = False
                warning = "设置已生效，但当前 AstrBot 不支持写回配置，重启后可能恢复原值"

            self._apply_reload()
            ready = self.settings.chain_ready()
            if not ready:
                warning = (
                    (warning + "；" if warning else "")
                    + "服务商尚未配置完整（检查 API Key / 模型）"
                )
            return self._web_ok(
                {
                    "persisted": persisted,
                    "warning": warning,
                    "ready": ready,
                    "provider": self.settings.primary.kind,
                    "model": self.settings.primary.resolved_model(),
                    "base_url": self.settings.primary.resolved_base_url(),
                    "backup": bool(self.settings.backup),
                    "default_style": self.settings.default_style,
                    "default_ratio": self.settings.default_ratio,
                    "default_images": self.settings.default_images,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 保存设置失败：{exc!r}")
            return self._web_err("保存设置失败，请查看 AstrBot 日志")

    async def api_generate(self):
        try:
            payload = await self._json_body()
            self._ensure()
            settings = self.settings
            if not settings.chain_ready():
                return self._web_err("还没有可用的生图服务，请先在插件配置中填写 API Key / 模型")

            prompt = sanitize(str(payload.get("prompt") or ""), settings.max_prompt_chars)
            reference = None
            raw_image = str(payload.get("image") or "").strip()
            if raw_image:
                try:
                    reference = self._parse_reference_payload(raw_image)
                except ProviderError as exc:
                    return self._web_err(exc.message)
            if not prompt:
                if reference is not None:
                    prompt = _DEFAULT_EDIT_PROMPT
                else:
                    return self._web_err("请先填写画面描述")
            block = self._check_prompt(prompt)
            if block:
                return self._web_err(block)

            book = self._stylebook()
            raw_style = str(payload.get("style") or "").strip()
            style: Style | None = None
            if raw_style and raw_style.lower() not in ("auto", "自动"):
                style = book.resolve(raw_style)
            if style is None:
                style = book.detect(prompt) or book.get(settings.default_style)

            ratio = parse_ratio(
                payload.get("ratio") or style.ratio or settings.default_ratio,
                settings.default_ratio,
            )
            try:
                raw_count = int(payload.get("count") or 0)
            except (TypeError, ValueError):
                raw_count = 0
            count = settings.allowed_images(raw_count or None)
            extra_negative = sanitize(str(payload.get("negative") or ""), 200)

            limiter = self._limiter
            if limiter is None:
                return self._web_err("插件尚未初始化完成，请稍后重试")
            try:
                await limiter.acquire_slot()
            except LimitError as exc:
                return self._web_err(exc.message)

            started = time.monotonic()
            try:
                negative_hint = settings.negative_as_hint and not (
                    self._chain and getattr(self._chain[0], "supports_negative", False)
                )
                final_prompt, negative = build_prompt(
                    style, prompt, negative_as_hint=negative_hint
                )
                if extra_negative:
                    negative = join_parts([negative, extra_negative])
                    if negative_hint:
                        final_prompt = join_parts(
                            [final_prompt, f"画面中避免出现：{extra_negative}"]
                        )
                timeout = settings.request_timeout * (len(self._chain) + 1) + 30
                results = await asyncio.wait_for(
                    self._create_images(
                        final_prompt, negative, count, ratio, reference=reference
                    ),
                    timeout=timeout,
                )
                pairs = await asyncio.to_thread(self._save_pairs, results)
                images = []
                for path, result in pairs:
                    thumb = await asyncio.to_thread(self._make_thumb_data_uri, path)
                    images.append(
                        {
                            "name": f"{path.parent.name}/{path.name}",
                            "thumb": thumb,
                            "width": result.width or 0,
                            "height": result.height or 0,
                            "provider": result.provider,
                            "model": result.model,
                        }
                    )
                elapsed = time.monotonic() - started
                self._maybe_cleanup()
                return self._web_ok(
                    {
                        "images": images,
                        "style": {"id": style.id, "name": style.name},
                        "ratio": ratio,
                        "count": len(images),
                        "elapsed": round(elapsed, 1),
                        "prompt": final_prompt,
                        "provider": images[0]["provider"] if images else "",
                        "model": images[0]["model"] if images else "",
                        "edited": reference is not None,
                    }
                )
            except asyncio.TimeoutError:
                return self._web_err("生成超时，请稍后重试或换用更快的模型")
            except ProviderError as exc:
                return self._web_err(self._friendly_error(exc).replace("❌ 生成失败：", ""))
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[imggen] 界面生成失败：{exc!r}")
                return self._web_err("生成失败：插件内部错误，请查看 AstrBot 日志")
            finally:
                limiter.release_slot()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 界面生成请求异常：{exc!r}")
            return self._web_err("请求处理失败，请查看 AstrBot 日志")

    async def api_history(self):
        try:
            self._ensure()
            limit = self._query_int("limit", 12, 1, 30)
            items = await asyncio.to_thread(self._list_recent_images, limit)
            return self._web_ok({"items": items})
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 历史读取失败：{exc!r}")
            return self._web_err("历史读取失败，请查看日志")

    async def api_thumb(self):
        try:
            self._ensure()
            name = self._query_str("name")
            path = self._resolve_image_path(name)
            thumb = await asyncio.to_thread(self._make_thumb_data_uri, path)
            return self._web_ok({"name": name, "thumb": thumb})
        except ValueError as exc:
            return self._web_err(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 缩略图读取失败：{exc!r}")
            return self._web_err("缩略图读取失败")

    async def api_image(self):
        try:
            self._ensure()
            path = self._resolve_image_path(self._query_str("name"))
            return await self._serve_file(path)
        except ValueError as exc:
            return self._web_err(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 原图读取失败：{exc!r}")
            return self._web_err("原图读取失败，请查看日志")

    async def api_cleanup(self):
        try:
            self._ensure()
            if self._store is None:
                return self._web_err("本地存储未初始化")
            info = await asyncio.to_thread(self._store.cleanup)
            return self._web_ok(info)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 清理失败：{exc!r}")
            return self._web_err("清理失败，请查看日志")

    async def api_reload(self):
        try:
            self._apply_reload()
            return self._web_ok(
                {
                    "styles": len(self._styles.styles) if self._styles else 0,
                    "providers": len(self._chain),
                    "ready": self.settings.chain_ready(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 界面重载失败：{exc!r}")
            return self._web_err("重载失败，请查看日志")

    # ==================================================================
    # 命令
    # ==================================================================

    @filter.command("画", alias={"绘画", "画图", "生图", "draw", "imagine", "绘制"})
    async def draw_cmd(self, event: AstrMessageEvent):
        """生图主命令：/画 <描述>（可加 风格、--比例、--n）"""
        if not self._claim(event):
            return
        args = strip_command(self._message_text(event), COMMAND_NAMES)
        async for result in self._stream(event, args):
            yield result

    @filter.command("画风", alias={"风格", "画风列表"})
    async def styles_cmd(self, event: AstrMessageEvent):
        """查看风格列表或某个风格的模板：/画风 [详情 名称]"""
        arg = strip_command(self._message_text(event), STYLE_COMMAND_NAMES).strip()
        book = self._stylebook()

        if arg.startswith(("详情", "查看")):
            name = arg[2:].strip()
            style = book.resolve(name) if name else None
            if style is None:
                yield event.plain_result(f"未找到风格「{name}」。发送 /画风 查看全部风格。")
                return
            yield event.plain_result(self._style_detail(style))
            return

        if arg and arg not in ("列表", "全部", "list", "all"):
            style = book.resolve(arg)
            if style is None:
                yield event.plain_result(f"未找到风格「{arg}」。发送 /画风 查看全部风格。")
                return
            yield event.plain_result(self._style_detail(style))
            return

        lines = [f"🎨 可用风格（{len(book.styles)} 种）"]
        for category, styles in book.grouped().items():
            names = "、".join(f"{s.name}" for s in styles)
            lines.append(f"【{category}】{names}")
        lines.append("用法：/画 国风水墨 山间小筑；/画风 详情 国风水墨 查看模板。")
        yield event.plain_result("\n".join(lines))

    @filter.command("画状态", alias={"生图状态", "绘图状态"})
    async def status_cmd(self, event: AstrMessageEvent):
        """查看当前服务商、队列、额度与本地存储状态。"""
        try:
            self._ensure()
            settings = self.settings
            lines = ["【绘影生图 · 状态】", f"版本：{PLUGIN_VERSION}"]
            lines.append(
                f"服务商：{settings.primary.describe()}"
                f"（{'可用' if settings.primary.ready() else '未配置完整'}）"
            )
            if settings.backup is not None:
                lines.append(
                    f"备用：{settings.backup.describe()}"
                    f"（{'可用' if settings.backup.ready() else '未配置完整'}）"
                )
            lines.append(f"风格数：{len(self._styles.styles) if self._styles else 0}")
            if self._limiter is not None:
                snap = self._limiter.snapshot(self._user_key(event))
                lines.append(
                    f"并发：{snap['inflight']}/{snap['max_concurrency']}，排队 {snap['waiting']}"
                )
                limit = settings.daily_limit or "不限"
                lines.append(
                    f"今日总量：{snap['total_today']} · 你今日：{snap.get('user_today', 0)}/{limit}"
                )
            if self._store is not None:
                stats = await asyncio.to_thread(self._store.stats)
                lines.append(
                    f"本地图片：{stats['count']} 张 / {stats['bytes'] / 1048576:.1f}MB"
                )
                lines.append(f"存储目录：{stats['root']}")
            opened = self._breaker.snapshot()
            if opened:
                lines.append(
                    "熔断："
                    + "、".join(
                        f"{key.split('|', 1)[0]} 约 {int(wait)} 秒"
                        for key, wait in opened.items()
                    )
                )
            active_jobs = [
                job
                for job in self._tool_jobs.values()
                if job.get("status") in ("queued", "running")
            ]
            if active_jobs:
                lines.append(f"后台任务：{len(active_jobs)} 个（/画任务 查看）")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 状态查询失败：{exc!r}")
            yield event.plain_result("❌ 状态查询失败，请查看日志。")

    @filter.command("画任务", alias={"生图任务", "绘图任务"})
    async def tasks_cmd(self, event: AstrMessageEvent):
        """查看自己的 AI 生图后台任务。"""
        try:
            self._ensure()
            jobs = self._visible_jobs(event)
            active = [
                job for job in jobs if job.get("status") in ("queued", "running")
            ]
            finished = [
                job for job in jobs if job.get("status") not in ("queued", "running")
            ][:5]
            lines = ["【绘影生图 · 任务】"]
            if active:
                lines.extend(self._format_job(job) for job in active)
            else:
                lines.append("没有进行中的任务。")
            if finished:
                lines.append("最近：")
                lines.extend(self._format_job(job) for job in finished)
            lines.append("用 /画取消 <编号|全部> 取消任务。")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 任务查询失败：{exc!r}")
            yield event.plain_result("❌ 任务查询失败，请查看日志。")

    @filter.command("画取消", alias={"取消生图", "取消绘图"})
    async def cancel_cmd(self, event: AstrMessageEvent):
        """取消自己的后台生图任务：/画取消 <编号|全部>。"""
        try:
            self._ensure()
            args = strip_command(
                self._message_text(event), ("画取消", "取消生图", "取消绘图")
            ).strip()
            target = args.split()[0].lstrip("#") if args else ""
            jobs = self._visible_jobs(event)
            if target and target.lower() not in ("全部", "all", "*"):
                job = next((item for item in jobs if item.get("id") == target), None)
                if job is None:
                    yield event.plain_result(
                        f"没找到任务 #{target}，可用 /画任务 查看自己的任务。"
                    )
                    return
                targets = [job]
            else:
                targets = [
                    job
                    for job in jobs
                    if job.get("status") in ("queued", "running")
                ]
            if not targets:
                yield event.plain_result("没有可取消的任务。")
                return
            cancelled = 0
            for job in targets:
                if job.get("status") == "queued":
                    job["status"] = "cancelled"
                    job["finished_at"] = time.time()
                    cancelled += 1
                elif job.get("status") == "running":
                    task = job.get("task")
                    if task is not None:
                        task.cancel()
                    else:
                        job["cancel_requested"] = True
                    cancelled += 1
            yield event.plain_result(f"已取消 {cancelled} 个任务。")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 取消任务失败：{exc!r}")
            yield event.plain_result("❌ 取消任务失败，请查看日志。")

    @filter.command("画清理", alias={"清理生图"})
    async def cleanup_cmd(self, event: AstrMessageEvent):
        """管理员：立即按保留策略清理本地图片。"""
        if not self._is_admin(event):
            yield event.plain_result("🔒 仅管理员可执行清理。")
            return
        try:
            self._ensure()
            assert self._store is not None
            info = await asyncio.to_thread(self._store.cleanup)
            yield event.plain_result(
                f"🧹 清理完成：删除 {info['deleted']} 张，"
                f"剩余 {info['remaining']} 张 / {info['bytes'] / 1048576:.1f}MB"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 清理失败：{exc!r}")
            yield event.plain_result("❌ 清理失败，请查看日志。")

    @filter.command("画重载", alias={"重载生图"})
    async def reload_cmd(self, event: AstrMessageEvent):
        """管理员：重读配置与自定义风格（不重启 AstrBot）。"""
        if not self._is_admin(event):
            yield event.plain_result("🔒 仅管理员可重载配置。")
            return
        try:
            self._apply_reload()
            styles = len(self._styles.styles) if self._styles else 0
            yield event.plain_result(
                f"✅ 已重载：{styles} 种风格，{len(self._chain)} 个可用服务商。"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 重载失败：{exc!r}")
            yield event.plain_result("❌ 重载失败，请查看日志。")

    def _style_detail(self, style: Style) -> str:
        lines = [f"【{style.name}】id：{style.id}", f"分类：{style.category}", f"简介：{style.summary}"]
        if style.aliases:
            lines.append("别名：" + "、".join(style.aliases))
        if style.ratio:
            lines.append("默认比例：" + style.ratio)
        if style.prefix:
            lines.append("风格前缀：" + style.prefix)
        if style.suffix:
            lines.append("补充词：" + style.suffix)
        if style.negative:
            lines.append("负面词：" + style.negative)
        return "\n".join(lines)

    # ==================================================================
    # 生命周期钩子
    # ==================================================================

    @filter.on_astrbot_loaded()
    async def _start_tool_worker(self, *args, **kwargs):
        """AstrBot 加载完成后启动常驻工具 worker，避免任务随单次调用被取消。"""
        try:
            self._ensure_tool_workers()
            logger.info("[imggen] 工具后台 worker 已初始化")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[imggen] 工具后台 worker 初始化失败：{exc!r}")

    # ==================================================================
    # 关键词自动触发
    # ==================================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_trigger(self, event: AstrMessageEvent):
        """消息以“画一张/帮我画/生图……”开头时自动生图。"""
        try:  # 每条消息都确保后台 worker 已就绪，早于工具调用
            self._ensure_tool_workers()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[imggen] 工具 worker 预热失败：{exc!r}")
        settings = self.settings
        if not settings.auto_trigger:
            return
        text = self._message_text(event).strip()
        if not text or text.startswith("/"):
            return
        first = re.split(r"\s+", text, maxsplit=1)[0].lower()
        if first in {name.lower() for name in (*COMMAND_NAMES, *STYLE_COMMAND_NAMES, *STATUS_COMMAND_NAMES)}:
            return
        prefix = next((p for p in settings.trigger_prefixes if text.startswith(p)), "")
        if not prefix:
            return
        rest = strip_prefixes(text, settings.trigger_prefixes)
        if not rest:
            return
        if not self._claim(event):
            return
        # 注意：这里不能用 event.stop_event()。AstrBot 的调度器一旦看到事件已停止，
        # 会在第一个 yield 处直接丢弃后续阶段，导致“正在绘制”发不出去、生成器被挂起，
        # 服务商请求根本不会发出。改为禁止默认 LLM 请求，结果仍走正常发送链路；
        # 插件自己已经发过消息后，AstrBot 也会因 _has_send_oper 跳过默认 LLM。
        try:
            blocker = getattr(event, "should_call_llm", None)
            if callable(blocker):
                # AstrBot 的语义：call_llm=True 表示跳过默认 LLM 请求
                blocker(True)
            else:  # 旧版 AstrBot 无该方法时直接写标记
                setattr(event, "call_llm", True)
        except Exception:  # noqa: BLE001
            pass
        async for result in self._stream(event, rest, trigger=prefix):
            yield result

    # ==================================================================
    # AI 工具
    # ==================================================================

    @llm_tool(name="generate_image")
    async def generate_image_tool(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        style: str = "auto",
        aspect_ratio: str = "",
        num_images: int = 0,
        **kwargs,
    ) -> str:
        """根据文字描述生成图片，并把图片直接发送给用户。当用户明确要求画画、作图、配图、生成图片、海报、头像、表情包、壁纸时调用。

        Args:
            prompt(string): 画面描述，写清主体、场景、构图、色彩与氛围，越具体越好。
            style(string): 风格 id 或中文名，例如 anime/二次元、guofeng/国风水墨、photoreal/写实摄影、pixel/像素风；不确定时传 auto。
            aspect_ratio(string): 可选画面比例，例如 16:9、1:1、9:16、3:4；不确定留空。
            num_images(number): 可选生成张数，默认 1，超过上限会被截断。
        """
        if not self.settings.tool_enabled:
            return "生图工具已被管理员关闭。"

        # 大模型常自造参数名：尽量吸收，而不是让 TypeError 冒出去
        if not prompt:
            prompt = str(kwargs.get("description") or kwargs.get("text") or kwargs.get("描述") or "")
        if not style or style == "auto":
            style = str(kwargs.get("风格") or kwargs.get("style_name") or style or "auto")
        if not aspect_ratio:
            aspect_ratio = str(
                kwargs.get("ratio")
                or kwargs.get("size")
                or kwargs.get("比例")
                or ""
            )
        if not num_images:
            raw_count = kwargs.get("n") or kwargs.get("count") or kwargs.get("数量")
            try:
                num_images = int(raw_count) if raw_count is not None else 0
            except (TypeError, ValueError):
                num_images = 0
        if kwargs:
            logger.info(f"[imggen] 工具收到未识别参数，已忽略：{sorted(kwargs.keys())}")

        style = str(style or "auto")
        try:
            count_value = int(num_images) if num_images else 0
        except (TypeError, ValueError):
            count_value = 0

        precheck = self._precheck(event)
        if precheck:
            return precheck

        self._ensure()
        settings = self.settings
        text = sanitize(prompt, settings.max_prompt_chars)
        if not text:
            return "请先给出具体的画面描述。"
        block = self._check_prompt(text)
        if block:
            return block

        book = self._stylebook()
        style_obj: Style | None = None
        if style and style.strip().lower() not in ("auto", "自动", ""):
            style_obj = book.resolve(style)
        if style_obj is None:
            style_obj = book.detect(text)
        if style_obj is None:
            style_obj = book.get(settings.default_style)

        limiter = self._limiter
        if limiter is None:  # 防御性：理论上 _ensure 后一定存在
            return "插件尚未初始化完成，请稍后重试。"
        ratio = parse_ratio(
            aspect_ratio or style_obj.ratio or settings.default_ratio,
            settings.default_ratio,
        )
        count = settings.allowed_images(count_value or None)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return "生成失败：无法确定当前会话，请重试。"
        user_key = self._user_key(event)
        hint = limiter.precheck(user_key)
        if hint:
            return f"暂时无法生成：{hint}"

        # 工具调用在 AstrBot 侧有约 120 秒超时：这里只做参数校验并立即返回，
        # 真正的生成交给常驻 worker 池，完成后主动把图片发到会话。
        queue = self._ensure_tool_workers()
        job_id = self._new_job_id()
        self._tool_jobs[job_id] = {
            "id": job_id,
            "umo": umo,
            "user_key": user_key,
            "style": style_obj.name,
            "count": count,
            "status": "queued",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "error": "",
            "task": None,
        }
        self._prune_jobs()
        queue.put_nowait(
            {
                "job_id": job_id,
                "umo": umo,
                "user_key": user_key,
                "style": style_obj,
                "text": text,
                "ratio": ratio,
                "count": count,
            }
        )
        logger.info(
            f"[imggen] 工具任务 #{job_id} 已入队：{umo} "
            f"style={style_obj.name} count={count}"
        )
        return (
            f"🎨 任务 #{job_id} 已开始生成 {count} 张"
            f"（风格：{style_obj.name}，比例：{ratio}）。"
            "完成后会直接把图片发到这个会话；发送 "
            f"`/画取消 {job_id}` 可取消。"
        )

    @llm_tool(name="list_image_styles")
    async def list_styles_tool(self, event: AstrMessageEvent, **kwargs) -> str:
        """列出当前可用的生图风格及其 id。当用户询问“有哪些画风/风格”“能画什么风格”，或需要为 generate_image 挑选风格时调用。无参数。
        """
        try:
            book = self._stylebook()
            lines = [f"共 {len(book.styles)} 种风格（括号内为 id）："]
            for category, styles in book.grouped().items():
                lines.append(f"【{category}】" + "、".join(f"{s.name}({s.id})" for s in styles))
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[imggen] 列风格失败：{exc!r}")
            return f"读取风格失败：{exc}"

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def terminate(self):
        """插件卸载/重载时取消后台任务并关闭 HTTP 会话。"""
        self._stopping = True
        for job in list(self._tool_jobs.values()):
            task = job.get("task")
            if task is not None and not task.done():
                task.cancel()
            if job.get("status") in ("queued", "running"):
                job["status"] = "cancelled"
                job["finished_at"] = time.time()
        workers = [task for task in self._tool_workers if not task.done()]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._tool_workers.clear()
        queue = self._tool_queue
        if queue is not None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    queue.task_done()
        tasks = [task for task in list(self._bg_tasks) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.clear()
        session = self._http
        self._http = None
        if session is not None and not getattr(session, "closed", True):
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
