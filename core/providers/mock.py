# -*- coding: utf-8 -*-
"""离线占位图服务商：不联网也能出图，用于试装、演示与自动化测试。

优先用 Pillow 画一张带文字的渐变图；Pillow 不可用时退回纯标准库
（zlib + struct）生成渐变 PNG，保证任何环境都有输出。
"""

from __future__ import annotations

import struct
import zlib

from .base import ImageProvider, ImageResult

_MAX_EDGE = 2048


def _clamp(value: int) -> int:
    return max(64, min(_MAX_EDGE, int(value)))


def _png_gradient(width: int, height: int, top: tuple, bottom: tuple) -> bytes:
    """纯标准库生成竖向渐变 PNG（RGB）。"""
    rows = bytearray()
    denom = max(1, height - 1)
    for y in range(height):
        t = y / denom
        rgb = bytes(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        rows.append(0)  # filter type 0
        rows.extend(rgb * width)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )


def render_placeholder(width: int, height: int, prompt: str, index: int = 0) -> bytes:
    """生成占位图字节。"""
    width, height = _clamp(width), _clamp(height)
    shift = (index * 17) % 60
    top = (30 + shift, 38, 62)
    bottom = (70, 110 + shift, 150)

    try:
        from PIL import Image as PILImage, ImageDraw
    except Exception:  # noqa: BLE001 - 无 Pillow 时用纯标准库
        return _png_gradient(width, height, top, bottom)

    image = PILImage.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)

    def ascii_only(text: str) -> str:
        return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text)

    lines = [
        "MOCK IMAGE",
        f"{width} x {height}",
        ascii_only(prompt)[:40],
    ]
    font = None
    try:  # Pillow >= 9.2 自带默认字体
        from PIL import ImageFont

        font = ImageFont.load_default(size=max(14, width // 32))
    except Exception:  # noqa: BLE001
        font = None

    y = max(8, height // 3)
    for line in lines:
        draw.text((max(8, width // 20), y), line, fill=(235, 240, 250), font=font)
        y += max(18, width // 22)

    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class MockProvider(ImageProvider):
    kind = "mock"
    supports_negative = False
    supports_edit = True

    async def generate(
        self, session, *, prompt, negative, n, ratio
    ) -> list[ImageResult]:
        size = self.size_for(ratio)
        results: list[ImageResult] = []
        for index in range(max(1, int(n))):
            content = render_placeholder(size.width, size.height, prompt, index)
            results.append(
                ImageResult(
                    content=content,
                    mime="image/png",
                    width=size.width,
                    height=size.height,
                    provider="mock",
                    model=self.model or "mock-1",
                )
            )
        return results

    async def edit(
        self, session, *, prompt, negative, n, ratio, image
    ) -> list[ImageResult]:
        """离线模式：原样返回参考图，便于验证图生图链路。"""
        size = self.size_for(ratio)
        results: list[ImageResult] = []
        for _index in range(max(1, int(n))):
            results.append(
                ImageResult(
                    content=image.data,
                    mime=image.mime or "image/png",
                    width=size.width,
                    height=size.height,
                    provider="mock",
                    model=self.model or "mock-1",
                    revised_prompt=prompt,
                )
            )
        return results
