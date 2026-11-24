# -*- coding: utf-8 -*-
"""服务商公共层：错误类型、图片字节校验、带重试的 HTTP、下载限流。"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import random
import re
from dataclasses import dataclass, field

import aiohttp

from astrbot.api import logger

from ..config import ProviderProfile
from ..sizes import SizeSpec, resolve_size

_SNIPPET_LIMIT = 240
_MAX_BODY_BYTES = 32 * 1024 * 1024  # 单次响应最多读 32MB，防异常大包拖垮内存

# 截断/破损响应里抢救完整条目的正则
_B64_SALVAGE_RE = re.compile(r'"b64_json"\s*:\s*"([A-Za-z0-9+/=]{64,})"')
_URL_SALVAGE_RE = re.compile(r'"url"\s*:\s*"(https?://[^"\\\s]+)"')


class ProviderError(Exception):
    """服务商调用失败。``status`` 用于映射友好的用户提示。"""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return self.message


@dataclass
class ReferenceImage:
    """图生图的参考图（已校验过的图片字节）。"""

    data: bytes
    mime: str = "image/png"
    name: str = "reference.png"


@dataclass
class ImageResult:
    """一张生成结果（适配完成后 content 一定有值）。"""

    content: bytes = b""
    mime: str = "image/png"
    width: int = 0
    height: int = 0
    provider: str = ""
    model: str = ""
    revised_prompt: str = ""
    url: str = ""
    extra: dict = field(default_factory=dict)


_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """通过魔数判断图片格式，返回 (扩展名, MIME)。"""
    if not data:
        return None
    for magic, ext, mime in _MAGIC:
        if data.startswith(magic):
            return ext, mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def clean_snippet(text: str, limit: int = _SNIPPET_LIMIT) -> str:
    """压缩并截断错误摘要，避免把超长 HTML/JSON 直接回给用户。"""
    out = re.sub(r"\s+", " ", str(text or "")).strip()
    return out[:limit] + ("…" if len(out) > limit else "")


def decode_body(raw: bytes) -> str:
    """按 UTF-8 解码并去掉 BOM / NUL。

    部分中转站会在 JSON 前加 UTF-8 BOM 或在末尾补 NUL，
    直接 ``json.loads`` 会报“不是合法 JSON”。
    """
    return raw.decode("utf-8-sig", errors="replace").replace("\ufeff", "").replace("\x00", "")


def _salvage_items(text: str) -> list[dict]:
    """从被截断的 JSON 里抢救已经完整的图片条目。"""
    items: list[dict] = []
    seen: set[str] = set()
    for match in _B64_SALVAGE_RE.finditer(text):
        value = match.group(1)
        if value not in seen:
            seen.add(value)
            items.append({"b64_json": value})
    for match in _URL_SALVAGE_RE.finditer(text):
        value = match.group(1)
        if value not in seen:
            seen.add(value)
            items.append({"url": value})
    return items


def _png_ok(data: bytes) -> bool:
    """校验 PNG 结构是否完整。

    允许缺少 IEND（Pillow 仍能正常解码），但任何一个 chunk 被截断都判失败，
    避免把半张图当成功结果发出去。
    """
    if len(data) < 33 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    pos = 8
    saw_idat = False
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if length > len(data) or end > len(data):
            return False
        if ctype == b"IDAT":
            saw_idat = True
        if ctype == b"IEND":
            return saw_idat
        pos = end
    return saw_idat and pos == len(data)


def _image_looks_complete(data: bytes) -> bool:
    """判断图片字节是否足够完整（用于恢复被截断的 JSON 尾巴）。"""
    detected = sniff_image(data)
    if not detected:
        return False
    ext, _mime = detected
    if ext == "png":
        return _png_ok(data)
    if ext == "jpg":
        return data.rstrip(b"\x00").endswith(b"\xff\xd9")
    if ext == "gif":
        return data.rstrip(b"\x00").endswith(b";")
    if ext == "webp":
        if len(data) < 12:
            return False
        declared = int.from_bytes(data[4:8], "little") + 8
        return declared <= len(data) + 1
    return False


def _salvage_prefix_image(text: str) -> dict | None:
    """响应在 b64 字符串中间/尾部被截断时，尝试恢复一张完整图片。

    很多“截断”只丢了 JSON 的收尾引号/括号，图片数据其实已经完整：
    这里按 base64 长度容差解码，并用 PNG/JPEG/GIF/WEBP 的结束标记校验。
    """
    marker = '"b64_json"'
    idx = text.find(marker)
    if idx < 0:
        return None
    colon = text.find(":", idx)
    if colon < 0:
        return None
    quote = text.find('"', colon + 1)
    if quote < 0:
        return None
    payload = text[quote + 1:]
    end = payload.find('"')
    if end >= 0:
        payload = payload[:end]
    payload = re.sub(r"\s+", "", payload)
    payload = re.sub(r"[^A-Za-z0-9+/=]", "", payload)
    if len(payload) < 64:
        return None

    for cut in range(0, 9):
        candidate = payload[: len(payload) - cut] if cut else payload
        candidate = candidate[: len(candidate) - (len(candidate) % 4)]
        if len(candidate) < 64:
            continue
        try:
            data = base64.b64decode(candidate, validate=False)
        except (binascii.Error, ValueError):
            continue
        if _image_looks_complete(data):
            return {"b64_json": candidate}
    return None


def load_json_tolerant(
    text: str,
    *,
    expected_length: int | None = None,
    received_length: int | None = None,
) -> dict:
    """宽容解析服务商响应。

    处理三类真实世界问题：
    1. BOM / NUL 等杂字节（在 decode_body 已清掉）；
    2. 代理或服务商把响应截断在字符串中间；
    3. 尾部有额外数据。

    解析失败时先截到最后一个右花括号重试，再尝试抢救已完整的图片条目；
    全无办法时抛出带具体原因的 ProviderError，截断场景标记为可重试。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return {}

    truncated = bool(
        expected_length and received_length is not None and received_length < expected_length
    )

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        error_msg = f"{exc.msg}（位置 {exc.pos}）"
        # 报错位置贴着末尾，或字符串没闭合，多半是响应被截断
        if exc.pos >= max(0, len(cleaned) - 8) or exc.msg.startswith("Unterminated string"):
            truncated = True

        cut = cleaned.rfind("}")
        if cut > 0:
            try:
                candidate = json.loads(cleaned[: cut + 1])
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict):
                return candidate

        items = _salvage_items(cleaned)
        if items:
            logger.warning("生图响应不完整，已抢救 %d 张完整图片", len(items))
            return {"data": items}

        prefix_item = _salvage_prefix_image(cleaned)
        if prefix_item:
            logger.warning("生图响应尾部被截断，但图片数据完整，已恢复 1 张")
            return {"data": [prefix_item]}

        if truncated:
            if expected_length and received_length is not None and received_length < expected_length:
                raise ProviderError(
                    "服务商响应不完整：连接提前结束（收到 "
                    f"{received_length} 字节 / 声明 {expected_length} 字节），"
                    "图片数据没有传完",
                    retryable=True,
                ) from None
            raise ProviderError(
                f"服务商返回的不是合法 JSON：{error_msg}，响应疑似被截断",
                retryable=True,
            ) from None
        raise ProviderError(
            f"服务商返回的不是合法 JSON：{error_msg}", retryable=False
        ) from None

    if not isinstance(data, dict):
        raise ProviderError("服务商返回结构异常（不是对象）", retryable=False)
    return data


def decode_base64(data: str, secret: str = "") -> bytes:
    """宽容地解析 base64 / data URI。"""
    text = str(data or "").strip()
    if not text:
        raise ProviderError("图片字段为空")
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    text = re.sub(r"\s+", "", text)
    pad = (-len(text)) % 4
    try:
        return base64.b64decode(text + "=" * pad, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ProviderError(f"base64 解码失败：{exc}") from None


def _declared_length(resp: aiohttp.ClientResponse) -> int | None:
    """解析 Content-Length；缺失或非法时返回 None。

    有 ``Transfer-Encoding`` 时按 HTTP 规范忽略 Content-Length：
    分块传输以 0 长度块为结束标志，残留的 CL 头不可作为完整性依据。
    """
    if resp.headers.get("Transfer-Encoding"):
        return None
    raw = resp.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


async def _read_body_limited(
    resp: aiohttp.ClientResponse,
    max_bytes: int,
    *,
    what: str = "服务商响应",
    declared: int | None = None,
) -> bytes:
    """读完整个响应体（最多 ``max_bytes`` 字节）。

    注意 ``StreamReader.read(n)`` 的语义是“一有数据就返回、最多 n 字节”，
    并不是读满 n 字节。单次调用只会拿到第一个网络数据块（常见 7KB~128KB），
    对 2MB 以上的 base64 图片响应会直接截断，表现为“JSON 在 b64 中间断掉”。
    这里必须循环读到 EOF。
    """
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await resp.content.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ProviderError(
                    f"{what}超过 {max_bytes // (1024 * 1024)}MB 上限", retryable=False
                )
            chunks.append(chunk)
    except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError):
        # 连接在中途断开：aiohttp 可能在第一次 read 就静默返回半截数据，
        # 也可能在后续 read 抛 payload/connection 错误。统一给出字节数。
        if declared is not None and total < declared:
            raise ProviderError(
                f"{what}不完整：连接提前结束（收到 {total} 字节 / 声明 {declared} 字节），"
                "数据没有传完",
                retryable=True,
            ) from None
        raise
    return b"".join(chunks)


class _RetrySignal(Exception):
    def __init__(self, message: str, status: int | None):
        self.message = message
        self.status = status


async def request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    payload: dict | None = None,
    form: aiohttp.FormData | None = None,
    timeout: float = 60.0,
    retries: int = 2,
    backoff: float = 2.0,
    secret: str = "",
    max_body: int = _MAX_BODY_BYTES,
    retry_on_timeout: bool = True,
) -> dict:
    """带退避重试的 JSON 请求。

    只对网络错误、408/425/429、5xx 重试；4xx（参数/鉴权/内容审核）立即失败，
    避免重复计费。生成类 POST 建议传 ``retry_on_timeout=False``：
    超时可能代表服务商已经出图并计费，重试会造成重复扣费。
    """
    retries = max(0, int(retries))
    last: ProviderError | None = None

    for attempt in range(retries + 1):
        try:
            body_kwargs = {"data": form} if form is not None else {"json": payload}
            async with session.request(
                method, url, headers=headers, timeout=timeout, **body_kwargs
            ) as resp:
                declared = _declared_length(resp)
                if declared is not None and declared > max_body:
                    raise ProviderError(
                        f"服务商响应体过大（约 {declared // (1024 * 1024)}MB），"
                        f"超过上限 {max_body // (1024 * 1024)}MB",
                        retryable=False,
                    )

                try:
                    raw_bytes = await _read_body_limited(
                        resp, max_body, declared=declared
                    )
                except ProviderError as exc:
                    if resp.status >= 400:
                        # 错误响应体不完整时仍按状态码处理，4xx 不重试
                        retryable = resp.status in (408, 425, 429) or resp.status >= 500
                        message = f"HTTP {resp.status}：响应体不完整"
                        if retryable and attempt < retries:
                            raise _RetrySignal(message, resp.status) from None
                        raise ProviderError(
                            message, retryable=retryable, status=resp.status
                        ) from None
                    if exc.retryable and attempt < retries:
                        raise _RetrySignal(exc.message, None) from None
                    raise
                raw = decode_body(raw_bytes)
                if resp.status >= 400:
                    snippet = _redact(clean_snippet(raw), secret)
                    retryable = resp.status in (408, 425, 429) or resp.status >= 500
                    message = f"HTTP {resp.status}：{snippet}"
                    if retryable and attempt < retries:
                        raise _RetrySignal(message, resp.status)
                    raise ProviderError(message, retryable=retryable, status=resp.status)

                try:
                    data = load_json_tolerant(
                        raw, expected_length=declared, received_length=len(raw_bytes)
                    )
                except ProviderError as exc:
                    # 截断只做一次网络层重试，剩下交给适配器换返回格式
                    if exc.retryable and attempt < min(retries, 1):
                        raise _RetrySignal(exc.message, None) from None
                    raise

                # OpenAI 风格并列错误：{"error": {...}}
                error = data.get("error")
                if error and not any(k in data for k in ("data", "images", "candidates", "output")):
                    if isinstance(error, dict):
                        detail = error.get("message") or error.get("code") or str(error)
                    else:
                        detail = str(error)
                    raise ProviderError(
                        "服务商返回错误：" + _redact(clean_snippet(str(detail)), secret),
                        retryable=False,
                    )
                return data

        except _RetrySignal as exc:
            last = ProviderError(exc.message, retryable=True, status=exc.status)
        except asyncio.TimeoutError:
            message = f"请求超时（{timeout:g} 秒）"
            if not retry_on_timeout:
                raise ProviderError(
                    message + "；为避免重复计费已停止重试", retryable=False
                ) from None
            last = ProviderError(message, retryable=True)
        except aiohttp.ClientError as exc:
            last = ProviderError(
                f"网络请求失败：{exc.__class__.__name__}",
                retryable=True,
            )
        except ProviderError:
            raise

        if attempt < retries:
            delay = min(15.0, backoff * (2 ** attempt)) + random.uniform(0, 0.3)
            await asyncio.sleep(delay)

    raise last or ProviderError("请求失败（未知原因）", retryable=True)


def _redact(text: str, secret: str) -> str:
    if secret and secret in text:
        text = text.replace(secret, "***")
    return text


async def download_image(
    session: aiohttp.ClientSession,
    url: str,
    *,
    max_bytes: int,
    timeout: float = 60.0,
    secret: str = "",
) -> bytes:
    """下载结果图（URL 模式），带体积与协议限制。"""
    if not url:
        raise ProviderError("图片地址为空")
    if not str(url).lower().startswith(("http://", "https://")):
        if str(url).startswith("data:"):
            return decode_base64(url, secret=secret)
        raise ProviderError("不支持的图片地址协议", retryable=False)

    try:
        async with session.get(url, timeout=timeout, allow_redirects=True, max_redirects=3) as resp:
            if resp.status >= 400:
                raise ProviderError(
                    f"下载图片失败：HTTP {resp.status}",
                    retryable=resp.status >= 500,
                    status=resp.status,
                )
            declared = _declared_length(resp)
            if declared is not None and declared > max_bytes:
                raise ProviderError(
                    f"图片超过体积上限（{max_bytes // (1024 * 1024)}MB）", retryable=False
                )
            data = await _read_body_limited(
                resp, max_bytes, what="图片", declared=declared
            )
            if declared is not None and len(data) < declared:
                raise ProviderError(
                    f"图片下载不完整：连接提前结束（收到 {len(data)} 字节 / 声明 {declared} 字节）",
                    retryable=True,
                )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise ProviderError(f"下载图片失败：{exc.__class__.__name__}", retryable=True) from None

    if not data:
        raise ProviderError("下载到空图片", retryable=True)
    return data


class ImageProvider:
    """服务商适配基类。"""

    kind = "base"
    supports_negative = False
    supports_edit = False

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        request_timeout: float = 180.0,
        max_retries: int = 2,
        backoff: float = 2.0,
        max_download_bytes: int = 20 * 1024 * 1024,
        size_mode: str = "auto",
        custom_size: str = "1024x1024",
        base_edge: int = 1024,
        response_mode: str = "auto",
    ):
        self.profile = profile
        self.model = profile.resolved_model()
        self.base_url = profile.resolved_base_url()
        self.api_key = profile.api_key
        self.quality = profile.quality
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.max_download_bytes = max_download_bytes
        self.size_mode = size_mode
        self.custom_size = custom_size
        self.base_edge = base_edge
        self.response_mode = response_mode

    # ---------- 公共工具 ----------

    def size_for(self, ratio: str) -> SizeSpec:
        return resolve_size(
            self.kind, self.model, ratio, self.size_mode, self.custom_size, self.base_edge
        )

    async def _as_result(self, session: aiohttp.ClientSession, item: dict, size: SizeSpec) -> ImageResult:
        """把服务商返回的一项（base64 或 URL）转成 ImageResult。"""
        content: bytes = b""
        url = str(item.get("url") or item.get("image_url") or "").strip()

        b64 = (
            item.get("b64_json")
            or item.get("b64")
            or item.get("base64")
            or item.get("image")
            or ""
        )
        if isinstance(b64, str) and b64.strip() and not b64.strip().lower().startswith("http"):
            content = decode_base64(b64)
        elif url:
            content = await download_image(
                session,
                url,
                max_bytes=self.max_download_bytes,
                timeout=self.request_timeout,
                secret=self.api_key,
            )
        elif isinstance(b64, str) and b64.strip().lower().startswith("http"):
            url = b64.strip()
            content = await download_image(
                session,
                url,
                max_bytes=self.max_download_bytes,
                timeout=self.request_timeout,
                secret=self.api_key,
            )

        if not content:
            raise ProviderError("服务商没有返回图片数据", retryable=False)
        if len(content) > self.max_download_bytes:
            raise ProviderError("图片超过体积上限", retryable=False)

        detected = sniff_image(content)
        if not detected:
            raise ProviderError("服务商返回的图片格式无法识别", retryable=False)
        _ext, mime = detected

        result = ImageResult(
            content=content,
            mime=str(item.get("mime_type") or item.get("mimeType") or mime),
            width=int(item.get("width") or size.width or 0),
            height=int(item.get("height") or size.height or 0),
            provider=self.kind,
            model=self.model,
            revised_prompt=str(item.get("revised_prompt") or ""),
            url=url,
        )
        return result

    async def generate(
        self,
        session: aiohttp.ClientSession,
        *,
        prompt: str,
        negative: str,
        n: int,
        ratio: str,
    ) -> list[ImageResult]:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def edit(
        self,
        session: aiohttp.ClientSession,
        *,
        prompt: str,
        negative: str,
        n: int,
        ratio: str,
        image: ReferenceImage,
    ) -> list[ImageResult]:  # pragma: no cover - 仅支持图生图的服务商覆写
        raise ProviderError(
            "当前服务商不支持图生图，请改用 OpenAI 兼容服务商", retryable=False
        )
