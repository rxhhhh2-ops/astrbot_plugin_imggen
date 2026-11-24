# -*- coding: utf-8 -*-
"""提示词处理：清洗、截断、风格模板拼装、触发词剥离。"""

from __future__ import annotations

import re

from .styles import Style

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_LEADING_PUNCT = "：:，,、;；。. "
_DUP_PUNCT_RE = re.compile(r"[，,]\s*(?:[，,]\s*)+")


def sanitize(text: str, max_chars: int = 800) -> str:
    """去掉控制字符、压缩空白、裁剪长度。

    保留换行（多行描述对模型是有用的），但连续空行会折叠。
    """
    if not text:
        return ""
    out = _CONTROL_RE.sub(" ", str(text))
    out = out.replace("\u200b", " ").replace("\ufeff", " ")
    out = _SPACE_RE.sub(" ", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if max_chars > 0 and len(out) > max_chars:
        out = out[:max_chars].rstrip("，,、;；。. ") + "…"
    return out


def join_parts(parts) -> str:
    """用中文逗号拼接非空片段，并去掉片段尾部的标点与重复标点。"""
    cleaned: list[str] = []
    for part in parts:
        if not part:
            continue
        text = str(part).strip()
        text = text.strip(_LEADING_PUNCT)
        if text:
            cleaned.append(text)
    if not cleaned:
        return ""
    joined = "，".join(cleaned)
    return _DUP_PUNCT_RE.sub("，", joined)


def build_prompt(
    style: Style,
    user_prompt: str,
    *,
    include_style: bool = True,
    negative_as_hint: bool = True,
) -> tuple[str, str]:
    """返回 (最终提示词, 负面提示词)。

    - include_style=False 时只用用户原文（例如用户明确说“不要加任何风格”）；
    - negative_as_hint 仅对不支持 negative_prompt 的服务商生效，由调用方决定。
    """
    user_prompt = (user_prompt or "").strip()
    if not include_style:
        return user_prompt, ""
    prompt = join_parts([style.prefix, user_prompt, style.suffix])
    negative = style.negative or ""
    if negative and negative_as_hint:
        prompt = join_parts([prompt, f"画面中避免出现：{negative}"])
    return prompt, negative


def strip_prefixes(text: str, prefixes) -> str:
    """剥离开头的触发词，例如“画一张” → 剩余描述。"""
    out = (text or "").strip()
    if not out:
        return ""
    for prefix in sorted({p for p in prefixes if p}, key=len, reverse=True):
        if out.startswith(prefix):
            return out[len(prefix) :].lstrip(_LEADING_PUNCT).strip()
    return out


def strip_command(text: str, names) -> str:
    """剥离命令名：兼容 “/画 猫”、“画 猫”、“/画猫” 三种写法。"""
    out = (text or "").strip()
    if out.startswith("/"):
        out = out[1:].lstrip()
    if not out:
        return ""
    lowered = out.lower()
    name_set = {str(n).lower() for n in names if n}
    # 先尝试首个空白分隔的词
    match = re.match(r"(\S+)(?:\s+|$)", out)
    if match and match.group(1).lower() in name_set:
        return out[match.end() :].strip()
    # 再尝试连写形式（中文常见）
    for name in sorted(name_set, key=len, reverse=True):
        if lowered.startswith(name):
            return out[len(name) :].lstrip(_LEADING_PUNCT).strip()
    return out


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"
