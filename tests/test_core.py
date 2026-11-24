# -*- coding: utf-8 -*-
"""核心模块单元测试：风格、提示词、尺寸、存储、限流、配置。

不依赖 AstrBot 运行时，直接 python tests/test_core.py 运行。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fake_boot  # noqa: F401,E402  # 提供 astrbot.api.logger

from core.breaker import ProviderBreaker  # noqa: E402
from core.config import ProviderProfile, RuntimeSettings  # noqa: E402
from core.limits import LimitError, UsageLimiter  # noqa: E402
from core.prompt import (  # noqa: E402
    build_prompt,
    join_parts,
    sanitize,
    strip_command,
    strip_prefixes,
)
from core.providers.mock import render_placeholder  # noqa: E402
from core.sizes import parse_dimensions, parse_ratio, resolve_size  # noqa: E402
from core.store import ImageStore  # noqa: E402
from core.styles import BUILTIN_STYLES, StyleBook, normalize  # noqa: E402

passed = 0


def check(cond, msg):
    global passed
    assert cond, "FAIL: " + msg
    passed += 1
    print("  ok -", msg)


def test_styles():
    print("== 风格库 ==")
    check(len(BUILTIN_STYLES) >= 30, f"内置风格不少于 30 种（实际 {len(BUILTIN_STYLES)}）")
    ids = [s.id for s in BUILTIN_STYLES]
    check(len(ids) == len(set(ids)), "风格 id 无重复")

    book = StyleBook()
    check(book.resolve("国风水墨").id == "guofeng", "按名称解析国风水墨")
    check(book.resolve("水墨").id == "guofeng", "按别名解析水墨")
    check(book.resolve("anime").id == "anime", "按 id 解析 anime")
    check(book.resolve("二次元风格").id == "anime", "带“风格”后缀也能解析")
    check(book.resolve("用赛博朋克画一只猫").id == "cyberpunk", "整句里包含风格名时可解析")
    check(book.resolve_token("水墨画里的山") is None, "整词匹配不会误吞句子")
    check(book.detect("画一只像素风小猫").id == "pixel", "关键词识别像素风")
    check(book.detect("帮我画个产品海报").id in ("poster", "product"), "关键词识别海报/产品")
    check(book.detect("今天天气不错") is None, "无关键词时不强行匹配")
    check(book.get("不存在的风格").id == "general", "未知风格回退默认")

    # 用户自定义风格
    tmp = tempfile.mkdtemp(prefix="imggen_styles_")
    path = os.path.join(tmp, "styles.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "styles": {
                    "my": {
                        "name": "我的风格",
                        "aliases": ["mys"],
                        "prefix": "自定义前缀",
                        "ratio": "16:9",
                    }
                },
                "override": {"guofeng": {"prefix": "覆盖后的水墨前缀"}},
                "disable": ["ukiyoe"],
            },
            fh,
            ensure_ascii=False,
        )
    book2 = StyleBook(path)
    check(book2.resolve("mys").id == "my", "自定义风格可按别名解析")
    check(book2.get("guofeng").prefix == "覆盖后的水墨前缀", "override 生效")
    check("ukiyoe" not in book2.styles, "disable 生效")

    # 损坏文件不影响内置风格
    bad = os.path.join(tmp, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    book3 = StyleBook(bad)
    check(len(book3.styles) == len(BUILTIN_STYLES), "损坏的用户风格文件被忽略")
    check(normalize(" 国风 风格 ") == "国风", "风格名归一化去空白与后缀")


def test_prompt():
    print("== 提示词 ==")
    check(sanitize("  你好\x00世界  ") == "你好 世界", "清洗控制字符与首尾空白")
    long_text = "字" * 50
    clamped = sanitize(long_text, 20)
    check(len(clamped) <= 21 and clamped.endswith("…"), "超长提示词被截断并加省略号")
    check(join_parts(["a，", "", "b"]) == "a，b", "拼接去空并清理标点")

    book = StyleBook()
    style = book.get("guofeng")
    prompt, negative = build_prompt(style, "山间小筑", negative_as_hint=True)
    check(style.prefix in prompt and "山间小筑" in prompt, "风格前缀与用户描述都进入提示词")
    check("避免出现" in prompt and negative, "负面词以“避免出现”追加")
    prompt2, _ = build_prompt(style, "山间小筑", negative_as_hint=False)
    check("避免出现" not in prompt2, "关闭负面提示时不追加")

    check(strip_prefixes("画一张 一只猫", ("画一张", "帮我画")) == "一只猫", "剥离触发词")
    check(strip_command("/画 一只猫", ("画",)) == "一只猫", "剥离 /画 命令")
    check(strip_command("画一只猫", ("画",)) == "一只猫", "剥离连写命令")
    check(strip_command("绘画 一只猫", ("画", "绘画")) == "一只猫", "剥离别名命令")


def test_sizes():
    print("== 尺寸 ==")
    check(parse_ratio("16:9") == "16:9", "标准比例直通")
    check(parse_ratio("横图") == "16:9", "中文别名 横图")
    check(parse_ratio("1024x1792") == "9:16", "按宽高换算到最接近比例")
    check(parse_ratio("乱写", "1:1") == "1:1", "无法识别时回退默认")
    check(parse_dimensions("1280x720") == (1280, 720), "解析自定义尺寸")
    check(parse_dimensions("xx", (512, 512)) == (512, 512), "非法尺寸回退默认")

    spec = resolve_size("openai", "gpt-image-1", "16:9")
    check(spec.value == "1536x1024", f"gpt-image 16:9 → {spec.value}")
    spec = resolve_size("openai", "dall-e-3", "16:9")
    check(spec.value == "1792x1024", f"dall-e-3 16:9 → {spec.value}")
    spec = resolve_size("openai", "flux.1-schnell", "9:16")
    check(spec.value == "1024x1792", f"通用模型 9:16 → {spec.value}")
    spec = resolve_size("dashscope", "qwen-image-plus", "16:9")
    check(spec.value == "1280*720", f"dashscope 16:9 → {spec.value}")
    spec = resolve_size("openai", "gpt-image-1", "1:1", size_mode="custom", custom_size="640x640")
    check(spec.value == "640x640", "自定义尺寸模式生效")
    spec = resolve_size("gemini", "gemini-2.5-flash-image", "3:4")
    check(spec.value == "3:4" and spec.width < spec.height, "gemini 用比例作为尺寸值")


def test_store():
    print("== 存储 ==")
    root = tempfile.mkdtemp(prefix="imggen_store_")
    store = ImageStore(root, keep_days=0, max_files=50)
    png = render_placeholder(64, 64, "hello")
    path = store.save(png)
    check(path.exists() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "PNG 落盘且魔数正确")

    try:
        store.save(b"<html>not an image</html>")
        raised = False
    except ValueError:
        raised = True
    check(raised, "非图片字节被拒绝")

    stats = store.stats()
    check(stats["count"] == 1 and stats["bytes"] > 0, "统计数量与体积")

    # 超期清理
    root2 = tempfile.mkdtemp(prefix="imggen_store_age_")
    store2 = ImageStore(root2, keep_days=1, max_files=100)
    old = store2.save(png)
    old_time = time.time() - 3 * 86400
    os.utime(old, (old_time, old_time))
    info = store2.cleanup()
    check(info["deleted"] == 1 and not old.exists(), "超期文件被清理")

    # 超量清理
    root3 = tempfile.mkdtemp(prefix="imggen_store_count_")
    store3 = ImageStore(root3, keep_days=0, max_files=10)
    for _ in range(12):
        store3.save(png)
    info = store3.cleanup()
    check(info["remaining"] <= 10, f"超量裁剪后剩余 {info['remaining']} 张（上限 10）")


async def _limits_case():
    print("== 限流 ==")
    # Linux 容器刚启动时 monotonic 可能只有几十秒；首次请求不能被误判为冷却。
    with mock.patch("core.limits.time.monotonic", return_value=5.0):
        fresh = UsageLimiter(
            cooldown_seconds=60, daily_limit=0, max_concurrency=1,
            queue_timeout=0.2, max_queue=0,
        )
        await fresh.acquire("fresh")
        fresh.release("fresh", success=False)
    check(True, "低 uptime 下首次请求不被误判冷却")

    limiter = UsageLimiter(cooldown_seconds=60, daily_limit=0, max_concurrency=1,
                           queue_timeout=0.2, max_queue=0)
    await limiter.acquire("u1")
    try:
        await limiter.acquire("u1")
        check(False, "同用户并发应被拒绝")
    except LimitError as exc:
        check("上一张" in exc.message, "同用户并发被拒绝")
    limiter.release("u1", success=False)
    try:
        await limiter.acquire("u1")
        check(False, "冷却应生效")
    except LimitError as exc:
        check("冷却" in exc.message, "冷却期提示")

    await limiter.acquire("u2")
    try:
        await limiter.acquire("u3")
        check(False, "排队应有上限")
    except LimitError as exc:
        check("排队" in exc.message or "较多" in exc.message, "队列满时快速拒绝")
    limiter.release("u2")

    quota = UsageLimiter(cooldown_seconds=0, daily_limit=1, max_concurrency=2,
                         queue_timeout=1, max_queue=2)
    await quota.acquire("u4")
    quota.release("u4", success=True)
    try:
        await quota.acquire("u4")
        check(False, "每日额度应生效")
    except LimitError as exc:
        check("额度" in exc.message, "每日额度用尽提示")

    refund = UsageLimiter(cooldown_seconds=0, daily_limit=1, max_concurrency=2,
                          queue_timeout=1, max_queue=2)
    await refund.acquire("u5")
    refund.release("u5", success=False)
    await refund.acquire("u5")  # 失败应退回额度
    refund.release("u5", success=True)
    check(True, "失败请求退回额度")

    print("== 额度持久化 ==")
    state_path = os.path.join(tempfile.mkdtemp(prefix="imggen_usage_"), "usage.json")
    persist = UsageLimiter(
        cooldown_seconds=60, daily_limit=5, max_concurrency=2,
        queue_timeout=1, max_queue=2, state_path=state_path,
    )
    await persist.acquire("u6")
    persist.release("u6", success=True)
    check(os.path.isfile(state_path), "额度状态写入文件")

    reloaded = UsageLimiter(
        cooldown_seconds=60, daily_limit=5, max_concurrency=2,
        queue_timeout=1, max_queue=2, state_path=state_path,
    )
    check(reloaded.snapshot("u6")["user_today"] == 1, "重启后每日额度仍保留")
    try:
        await reloaded.acquire("u6")
        check(False, "重启后冷却应生效")
    except LimitError as exc:
        check("冷却" in exc.message, "重启后冷却仍生效")

    refund_persist = UsageLimiter(
        cooldown_seconds=0, daily_limit=5, max_concurrency=2,
        queue_timeout=1, max_queue=2, state_path=state_path,
    )
    await refund_persist.acquire("u7")
    refund_persist.release("u7", success=False)
    again = UsageLimiter(
        cooldown_seconds=0, daily_limit=5, max_concurrency=2,
        queue_timeout=1, max_queue=2, state_path=state_path,
    )
    check(
        "u7" not in again._daily or again._daily["u7"][1] == 0,
        "失败退回的额度也持久化",
    )


def test_breaker():
    print("== 熔断 ==")
    now = [1000.0]
    breaker = ProviderBreaker(threshold=2, cooldown=30, clock=lambda: now[0])
    key = "openai|https://x|m"
    check(breaker.allow(key), "初始允许请求")
    breaker.record_failure(key)
    check(breaker.allow(key), "单次失败仍允许")
    breaker.record_failure(key)
    check(not breaker.allow(key), "达到阈值后熔断")
    check(breaker.retry_after(key) == 30, "熔断剩余时间正确")
    now[0] += 31
    check(breaker.allow(key), "冷却结束后恢复")
    breaker.record_failure(key)
    breaker.record_success(key)
    check(breaker.allow(key) and not breaker.snapshot(), "成功会清零失败计数")


def test_config():
    print("== 配置 ==")
    settings = RuntimeSettings.from_mapping({})
    check(settings.primary.kind == "openai", "缺省服务商为 openai")
    check(settings.max_images == 4 and settings.default_images == 1, "张数默认值")
    check(not settings.primary.ready(), "未填 key 时不可用")

    settings = RuntimeSettings.from_mapping(
        {
            "provider": "MOCK",
            "default_images": 99,
            "max_images": 999,
            "cooldown_seconds": -5,
            "daily_limit": -1,
            "max_concurrency": 0,
            "trigger_prefixes": "画,画一张,帮我画",
            "custom_size": "bad",
        }
    )
    check(settings.primary.kind == "mock", "服务商大小写归一化")
    check(settings.max_images == 10, "max_images 被裁剪到上限 10")
    check(settings.default_images == 4, "default_images 不超过 max_images")
    check(settings.cooldown_seconds == 0, "负冷却被裁剪为 0")
    check(settings.daily_limit == 0, "负额度被裁剪为 0（不限）")
    check(settings.max_concurrency == 1, "并发下限为 1")
    check("画" not in settings.trigger_prefixes, "过宽的单个“画”前缀被丢弃")
    check(settings.allowed_images(99) == 10, "请求张数受上限约束")

    check(RuntimeSettings.from_mapping({"openai_response_format": "URL"}).openai_response_format == "url",
          "返回格式大小写归一化")
    check(RuntimeSettings.from_mapping({"openai_response_format": "xxx"}).openai_response_format == "auto",
          "非法返回格式回退 auto")

    profile = ProviderProfile(kind="openai", api_key="k")
    check(profile.resolved_base_url().startswith("https://api.openai.com"), "默认 base_url 补齐")
    check(profile.resolved_model() == "gpt-image-1", "默认模型补齐")
    check(profile.ready(), "有 key 即可用")


def main():
    test_styles()
    test_prompt()
    test_sizes()
    test_store()
    asyncio.run(_limits_case())
    test_breaker()
    test_config()
    print(f"\n全部通过：{passed} 项")


if __name__ == "__main__":
    main()
