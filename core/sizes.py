# -*- coding: utf-8 -*-
"""尺寸/比例换算：把用户说的“16:9”翻译成各服务商真正接受的尺寸。

各家限制不同：
- OpenAI 官方（gpt-image / dall-e-3）只接受固定几档尺寸；
- DashScope 用 "宽*高" 字符串；
- Gemini 走原始比例（aspectRatio）；
- mock 本地画布任意尺寸。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

RATIOS: dict[str, tuple[int, int]] = {
    "1:1": (1, 1),
    "4:3": (4, 3),
    "3:4": (3, 4),
    "16:9": (16, 9),
    "9:16": (9, 16),
    "3:2": (3, 2),
    "2:3": (2, 3),
    "21:9": (21, 9),
    "9:21": (9, 21),
}

RATIO_ALIASES: dict[str, str] = {
    "方": "1:1",
    "方形": "1:1",
    "方图": "1:1",
    "正方形": "1:1",
    "square": "1:1",
    "横": "16:9",
    "横图": "16:9",
    "横向": "16:9",
    "宽": "16:9",
    "宽屏": "16:9",
    "landscape": "16:9",
    "banner": "16:9",
    "竖": "9:16",
    "竖图": "9:16",
    "竖向": "9:16",
    "竖屏": "9:16",
    "portrait": "9:16",
    "手机壁纸": "9:16",
    "超宽": "21:9",
}

# OpenAI 官方三档（gpt-image-1）与 dall-e-3 三档
OPENAI_GPT_IMAGE: dict[str, str] = {
    "1:1": "1024x1024",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
}
OPENAI_DALLE3: dict[str, str] = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
}
# 通用兼容接口（很多中转站/硅基流动等支持更宽的尺寸集合）
OPENAI_GENERIC: dict[str, str] = {
    "1:1": "1024x1024",
    "4:3": "1024x768",
    "3:4": "768x1024",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
}
DASHSCOPE_SIZES: dict[str, str] = {
    "1:1": "1024*1024",
    "4:3": "1152*864",
    "3:4": "864*1152",
    "3:2": "1248*832",
    "2:3": "832*1248",
    "16:9": "1280*720",
    "9:16": "720*1280",
    "21:9": "1280*548",
    "9:21": "548*1280",
}

_DIM_RE = re.compile(r"^\s*(\d{2,5})\s*[x×*]\s*(\d{2,5})\s*$")


@dataclass(frozen=True)
class SizeSpec:
    """一次生成请求的尺寸。

    width/height 用于本地记录与 mock 画布；
    value 是发给服务商的字符串（比例或尺寸，取决于服务商）。
    """

    width: int
    height: int
    value: str = ""

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}"


def ratio_value(ratio: str) -> float:
    pair = RATIOS.get(ratio) or (1, 1)
    return pair[0] / pair[1]


def parse_ratio(raw, default: str = "1:1") -> str:
    """把各种写法归一化到标准比例键。无法识别时返回 default。"""
    if raw is None:
        return default
    text = (
        str(raw).strip().lower().replace("：", ":").replace("×", ":").replace("x", ":")
    )
    text = re.sub(r"\s+", "", text)
    if not text:
        return default
    if text in RATIOS:
        return text
    if text in RATIO_ALIASES:
        return RATIO_ALIASES[text]

    match = re.fullmatch(r"(\d+):(\d+)", text)
    if match:
        width, height = int(match.group(1)), int(match.group(2))
        if width > 0 and height > 0:
            return _closest_standard(width / height, default)
    return default


def parse_dimensions(raw, default: tuple[int, int] = (1024, 1024)) -> tuple[int, int]:
    """解析 “1024x1024”；失败返回默认值。"""
    match = _DIM_RE.match(str(raw or ""))
    if not match:
        return default
    width, height = int(match.group(1)), int(match.group(2))
    if 64 <= width <= 8192 and 64 <= height <= 8192:
        return width, height
    return default


def _closest_standard(value: float, default: str = "1:1") -> str:
    best = default
    best_delta = float("inf")
    for key, (width, height) in RATIOS.items():
        delta = abs(math.log((width / height) / value)) if value > 0 else float("inf")
        if delta < best_delta:
            best, best_delta = key, delta
    return best


def _snap(value: int, multiple: int = 8, lo: int = 256, hi: int = 4096) -> int:
    out = max(lo, min(hi, int(round(value / multiple)) * multiple))
    return out


def scale(ratio: str, base_edge: int = 1024) -> tuple[int, int]:
    """按比例和长边基准生成尺寸（模拟/本地贴图用）。"""
    width, height = RATIOS.get(ratio, (1, 1))
    if width >= height:
        w = base_edge
        h = base_edge * height / width
    else:
        h = base_edge
        w = base_edge * width / height
    return _snap(w), _snap(h)


def closest_by_ratio(
    table: dict[str, str], ratio: str, default_key: str = "1:1"
) -> str:
    """从服务商支持的尺寸表里挑比例最接近的一项。"""
    if ratio in table:
        return table[ratio]
    target = ratio_value(ratio)
    best_key = default_key if default_key in table else next(iter(table))
    best_delta = float("inf")
    for key in table:
        pair = RATIOS.get(key)
        if not pair:
            continue
        delta = abs(math.log((pair[0] / pair[1]) / target))
        if delta < best_delta:
            best_key, best_delta = key, delta
    return table[best_key]


def resolve_size(
    provider_kind: str,
    model: str,
    ratio: str,
    size_mode: str = "auto",
    custom_size: str = "1024x1024",
    base_edge: int = 1024,
) -> SizeSpec:
    """按服务商能力解析出最终尺寸。"""
    ratio = parse_ratio(ratio, "1:1")
    model_lower = (model or "").lower()
    custom = size_mode == "custom"

    if provider_kind == "openai":
        if custom:
            width, height = parse_dimensions(custom_size, scale(ratio, base_edge))
            return SizeSpec(width, height, f"{width}x{height}")
        if model_lower.startswith("dall-e-3"):
            value = closest_by_ratio(OPENAI_DALLE3, ratio)
        elif model_lower.startswith("gpt-image"):
            value = closest_by_ratio(OPENAI_GPT_IMAGE, ratio, "1:1")
        else:
            value = closest_by_ratio(OPENAI_GENERIC, ratio)
        width, height = parse_dimensions(value, scale(ratio, base_edge))
        return SizeSpec(width, height, value)

    if provider_kind == "dashscope":
        if custom:
            width, height = parse_dimensions(custom_size, scale(ratio, base_edge))
            return SizeSpec(width, height, f"{width}*{height}")
        value = closest_by_ratio(DASHSCOPE_SIZES, ratio)
        width, height = parse_dimensions(
            value.replace("*", "x"), scale(ratio, base_edge)
        )
        return SizeSpec(width, height, value)

    if provider_kind == "gemini":
        width, height = (
            parse_dimensions(custom_size, scale(ratio, base_edge))
            if custom
            else scale(ratio, base_edge)
        )
        return SizeSpec(width, height, ratio)

    # mock 及未知服务商：本地画布
    width, height = (
        parse_dimensions(custom_size, scale(ratio, base_edge))
        if custom
        else scale(ratio, base_edge)
    )
    return SizeSpec(width, height, f"{width}x{height}")
