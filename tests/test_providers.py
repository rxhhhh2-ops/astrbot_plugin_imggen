# -*- coding: utf-8 -*-
"""服务商适配器测试：用本地 aiohttp 假服务器模拟 OpenAI / Gemini / DashScope。

全程离线（只连 127.0.0.1），直接 python tests/test_providers.py 运行。
"""

import asyncio
import base64
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fake_boot  # noqa: F401,E402  # 提供 astrbot.api.logger

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from core.config import ProviderProfile, RuntimeSettings  # noqa: E402
from core.providers import build_chain  # noqa: E402
from core.providers.base import ProviderError, ReferenceImage  # noqa: E402
from core.providers.dashscope import DashScopeProvider  # noqa: E402
from core.providers.gemini import GeminiProvider  # noqa: E402
from core.providers.mock import render_placeholder  # noqa: E402
from core.providers.openai_compat import OpenAICompatProvider  # noqa: E402

PNG = render_placeholder(64, 64, "unit-test")
PNG_B64 = base64.b64encode(PNG).decode()

passed = 0


def check(cond, msg):
    global passed
    assert cond, "FAIL: " + msg
    passed += 1
    print("  ok -", msg)


async def start(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def case_openai_b64():
    print("== OpenAI 兼容：b64 返回 ==")
    captured = {}

    async def handler(request):
        captured["payload"] = await request.json()
        captured["auth"] = request.headers.get("Authorization")
        return web.json_response({"data": [{"b64_json": PNG_B64}]})

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai",
            api_key="sk-secret",
            base_url=base + "/v1",
            model="gpt-image-1",
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="一只猫", negative="", n=1, ratio="1:1"
            )
        check(len(results) == 1 and results[0].content == PNG, "b64 响应解析为图片字节")
        check(captured["payload"]["size"] == "1024x1024", "默认 1:1 尺寸")
        check(captured["payload"]["model"] == "gpt-image-1", "模型名透传")
        check(
            "response_format" not in captured["payload"],
            "gpt-image 不发送 response_format",
        )
        check(captured["auth"] == "Bearer sk-secret", "Authorization 头正确")
    finally:
        await runner.cleanup()


async def case_openai_url():
    print("== OpenAI 兼容：URL 返回 ==")
    captured = {}

    async def gen(request):
        captured["payload"] = await request.json()
        return web.json_response({"data": [{"url": base + "/img.png"}]})

    async def image(request):
        return web.Response(body=PNG, content_type="image/png")

    app = web.Application()
    app.router.add_post("/v1/images/generations", gen)
    app.router.add_get("/img.png", image)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai",
            api_key="sk-secret",
            base_url=base + "/v1",
            model="flux.1-schnell",
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=1, ratio="16:9"
            )
        check(
            len(results) == 1 and results[0].content == PNG, "URL 结果被下载为图片字节"
        )
        check(
            captured["payload"]["response_format"] == "url",
            "auto 模式优先请求 url（JSON 小、抗截断）",
        )
        check(captured["payload"]["size"] == "1792x1024", "16:9 映射到通用尺寸表")
    finally:
        await runner.cleanup()


async def case_openai_retry():
    print("== OpenAI 兼容：5xx 重试 ==")
    state = {"calls": 0}

    async def handler(request):
        state["calls"] += 1
        if state["calls"] == 1:
            return web.Response(status=500, text="upstream boom")
        return web.json_response({"data": [{"b64_json": PNG_B64}]})

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="dall-e-3"
        )
        provider = OpenAICompatProvider(
            profile, request_timeout=10, max_retries=2, backoff=0.01
        )
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=3, ratio="1:1"
            )
        check(state["calls"] == 2, f"5xx 后重试一次（实际 {state['calls']} 次）")
        check(
            len(results) == 1 and results[0].content == PNG, "dall-e-3 强制 n=1 并成功"
        )
    finally:
        await runner.cleanup()


async def case_openai_auth_error():
    print("== OpenAI 兼容：鉴权失败 ==")
    state = {"calls": 0}

    async def handler(request):
        state["calls"] += 1
        return web.json_response(
            {"error": {"message": "invalid api key sk-secret"}}, status=401
        )

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai",
            api_key="sk-secret",
            base_url=base + "/v1",
            model="gpt-image-1",
        )
        provider = OpenAICompatProvider(
            profile, request_timeout=10, max_retries=2, backoff=0.01
        )
        async with aiohttp.ClientSession() as session:
            try:
                await provider.generate(
                    session, prompt="cat", negative="", n=1, ratio="1:1"
                )
                raised = None
            except ProviderError as exc:
                raised = exc
        check(raised is not None and raised.status == 401, "401 抛出 ProviderError")
        check(raised is not None and not raised.retryable, "401 不重试")
        check(state["calls"] == 1, "鉴权失败只请求一次（不重复计费）")
        check(
            "sk-secret" not in (raised.message if raised else ""),
            "错误信息中密钥被打码",
        )
    finally:
        await runner.cleanup()


async def case_gemini():
    print("== Gemini 原生 ==")
    bodies = []
    state = {"reject_image_config": True}

    async def handler(request):
        body = await request.json()
        bodies.append(body)
        config = body.get("generationConfig") or {}
        if state["reject_image_config"] and "imageConfig" in config:
            return web.json_response(
                {"error": {"message": "imageConfig is not supported by this model"}},
                status=400,
            )
        return web.json_response(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "here you go"},
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": PNG_B64,
                                    }
                                },
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post(
        "/v1beta/models/gemini-2.5-flash-image:generateContent", handler
    )
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="gemini",
            api_key="g-key",
            base_url=base + "/v1beta",
            model="gemini-2.5-flash-image",
        )
        provider = GeminiProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=1, ratio="16:9"
            )
        check(len(results) == 1 and results[0].content == PNG, "inlineData 解析为图片")
        check(len(bodies) == 2, "不支持 imageConfig 时自动降级重试")
        check(
            bodies[0]["generationConfig"]["imageConfig"]["aspectRatio"] == "16:9",
            "首次请求携带比例",
        )
        check(
            "imageConfig" not in bodies[1]["generationConfig"],
            "降级请求去掉 imageConfig",
        )
    finally:
        await runner.cleanup()


async def case_dashscope():
    print("== DashScope 异步任务 ==")
    captured = {}
    state = {"polls": 0}

    async def create(request):
        captured["payload"] = await request.json()
        captured["async_header"] = request.headers.get("X-DashScope-Async")
        return web.json_response(
            {"output": {"task_id": "task-1", "task_status": "PENDING"}}
        )

    async def task(request):
        state["polls"] += 1
        if state["polls"] == 1:
            return web.json_response({"output": {"task_status": "RUNNING"}})
        return web.json_response(
            {
                "output": {
                    "task_status": "SUCCEEDED",
                    "results": [{"url": base + "/img.png"}],
                }
            }
        )

    async def image(request):
        return web.Response(body=PNG, content_type="image/png")

    app = web.Application()
    app.router.add_post("/api/v1/services/aigc/text2image/image-synthesis", create)
    app.router.add_get("/api/v1/tasks/task-1", task)
    app.router.add_get("/img.png", image)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="dashscope",
            api_key="d-key",
            base_url=base + "/api/v1",
            model="qwen-image-plus",
        )
        provider = DashScopeProvider(profile, request_timeout=10, max_retries=0)
        provider.poll_interval = 0.01
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="模糊", n=1, ratio="1:1"
            )
        check(len(results) == 1 and results[0].content == PNG, "轮询拿到结果图")
        check(captured["async_header"] == "enable", "异步任务头正确")
        check(
            captured["payload"]["parameters"]["size"] == "1024*1024",
            "尺寸使用 宽*高 格式",
        )
        check(
            captured["payload"]["input"]["negative_prompt"] == "模糊",
            "negative_prompt 透传",
        )
        check(state["polls"] >= 2, "至少轮询两次直到成功")
    finally:
        await runner.cleanup()


async def case_dashscope_failed():
    print("== DashScope 任务失败 ==")

    async def create(request):
        return web.json_response(
            {"output": {"task_id": "task-2", "task_status": "PENDING"}}
        )

    async def task(request):
        return web.json_response(
            {
                "output": {
                    "task_status": "FAILED",
                    "code": "DataInspectionFailed",
                    "message": "内容不合规",
                }
            }
        )

    app = web.Application()
    app.router.add_post("/api/v1/services/aigc/text2image/image-synthesis", create)
    app.router.add_get("/api/v1/tasks/task-2", task)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="dashscope",
            api_key="d-key",
            base_url=base + "/api/v1",
            model="qwen-image-plus",
        )
        provider = DashScopeProvider(profile, request_timeout=10, max_retries=0)
        provider.poll_interval = 0.01
        async with aiohttp.ClientSession() as session:
            try:
                await provider.generate(
                    session, prompt="cat", negative="", n=1, ratio="1:1"
                )
                raised = None
            except ProviderError as exc:
                raised = exc
        check(
            raised is not None and "失败" in raised.message,
            "任务失败转为 ProviderError",
        )
        check(
            raised is not None and "内容不合规" in raised.message, "保留服务商失败原因"
        )
    finally:
        await runner.cleanup()


async def case_empty_response():
    print("== 空响应防御 ==")

    async def handler(request):
        return web.json_response({"data": []})

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="gpt-image-1"
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            try:
                await provider.generate(
                    session, prompt="cat", negative="", n=1, ratio="1:1"
                )
                raised = None
            except ProviderError as exc:
                raised = exc
        check(
            raised is not None and "没有图片数据" in raised.message,
            "空 data 被识别为失败",
        )
    finally:
        await runner.cleanup()


async def case_openai_strip_params():
    print("== OpenAI 兼容：不支持 response_format 时降级 ==")
    bodies = []

    async def handler(request):
        payload = await request.json()
        bodies.append(payload)
        if "response_format" in payload:
            return web.json_response(
                {"error": {"message": "Unsupported parameter: response_format"}},
                status=400,
            )
        return web.json_response({"data": [{"b64_json": PNG_B64}]})

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="flux.1-schnell"
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=1, ratio="1:1"
            )
        check(len(bodies) == 2, "参数不支持时自动重试一次")
        check("response_format" not in bodies[1], "重试请求去掉了 response_format")
        check(len(results) == 1 and results[0].content == PNG, "降级后成功出图")
    finally:
        await runner.cleanup()


async def case_bom_nul():
    print("== BOM / NUL 杂字节 ==")
    body = (
        b"\xef\xbb\xbf"
        + json.dumps({"created": 1, "data": [{"b64_json": PNG_B64}]}).encode()
        + b"\x00\x00"
    )

    async def handler(request):
        return web.Response(body=body, content_type="application/json")

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="gpt-image-1"
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=1, ratio="1:1"
            )
        check(
            len(results) == 1 and results[0].content == PNG,
            "带 BOM 与尾部 NUL 的 JSON 可正常解析",
        )
    finally:
        await runner.cleanup()


async def case_truncated_salvage():
    print("== 截断响应抢救 ==")
    body = (
        '{"created":123,"data":[{"b64_json":"'
        + PNG_B64
        + '"},{"b64_json":"iVBORw0KGgoAAAANSUhEUg'
    ).encode()

    async def handler(request):
        return web.Response(body=body, content_type="application/json")

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="gpt-image-1"
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=2, ratio="1:1"
            )
        check(len(results) == 1, "从截断 JSON 中抢救出 1 张完整图片")
        check(results[0].content == PNG, "抢救出的图片字节正确")
    finally:
        await runner.cleanup()


async def case_truncated_retry():
    print("== 无法抢救的截断响应会重试 ==")
    state = {"calls": 0}
    body = b'{"created":1,"data":[{"b64_json":"iVBORw0KGgoAAAA'

    async def handler(request):
        state["calls"] += 1
        return web.Response(body=body, content_type="application/json")

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="gpt-image-1"
        )
        provider = OpenAICompatProvider(
            profile, request_timeout=10, max_retries=1, backoff=0.01
        )
        async with aiohttp.ClientSession() as session:
            try:
                await provider.generate(
                    session, prompt="cat", negative="", n=1, ratio="1:1"
                )
                raised = None
            except ProviderError as exc:
                raised = exc
        check(state["calls"] == 2, f"截断响应触发一次重试（实际 {state['calls']} 次）")
        check(
            raised is not None and "不是合法 JSON" in raised.message,
            "最终报错说明 JSON 非法",
        )
        check(raised is not None and "截断" in raised.message, "报错提示响应被截断")
    finally:
        await runner.cleanup()


async def case_openai_format_fallback():
    print("== OpenAI 兼容：url 失败退回 b64 ==")
    formats = []

    async def handler(request):
        payload = await request.json()
        formats.append(payload.get("response_format"))
        if payload.get("response_format") == "url":
            # 模拟中转站把大 JSON 截断
            body = b'{"created":1,"data":[{"b64_json":"iVBORw0KGgoAAAA'
            return web.Response(body=body, content_type="application/json")
        return web.json_response({"data": [{"b64_json": PNG_B64}]})

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="flux.1-schnell"
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=1, ratio="1:1"
            )
        check(formats == ["url", "b64"], f"url 截断后自动改用 b64（实际 {formats}）")
        check(len(results) == 1 and results[0].content == PNG, "回退 b64 后成功出图")
    finally:
        await runner.cleanup()


async def case_truncated_prefix_salvage():
    print("== 仅缺 JSON 尾巴时恢复图片 ==")
    # 图片 base64 完整，只丢了结尾的引号/括号
    body = ('{"created":1,"data":[{"b64_json":"' + PNG_B64).encode()

    async def handler(request):
        return web.Response(body=body, content_type="application/json")

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="gpt-image-1"
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=1, ratio="1:1"
            )
        check(
            len(results) == 1 and results[0].content == PNG, "从截断响应中恢复完整图片"
        )
    finally:
        await runner.cleanup()


async def case_truncated_png_without_iend():
    print("== 缺 IEND 的完整 PNG 也能恢复 ==")
    png_cut = PNG[:-12]  # 去掉 IEND chunk，图片数据本身完整
    body = (
        '{"created":1,"data":[{"b64_json":"' + base64.b64encode(png_cut).decode()
    ).encode()

    async def handler(request):
        return web.Response(body=body, content_type="application/json")

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="gpt-image-1"
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=1, ratio="1:1"
            )
        check(
            len(results) == 1 and results[0].content == png_cut,
            "缺 IEND 但 chunk 完整的 PNG 被恢复",
        )
    finally:
        await runner.cleanup()


async def case_large_b64_body():
    print("== 大 b64 响应必须读满整个响应体 ==")
    # 512KB 图片 → ~683KB base64，远超 aiohttp 单次 read(n) 的首块大小
    big_png = b"\x89PNG\r\n\x1a\n" + os.urandom(512 * 1024)
    big_b64 = base64.b64encode(big_png).decode()

    async def handler(request):
        return web.json_response({"data": [{"b64_json": big_b64}]})

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="gpt-image-1"
        )
        provider = OpenAICompatProvider(profile, request_timeout=20, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=1, ratio="1:1"
            )
        check(len(results) == 1, "大响应完整解析出 1 张图")
        check(
            results[0].content == big_png, f"响应体读满（{len(big_png)} 字节未丢数据）"
        )
    finally:
        await runner.cleanup()


async def case_chunked_large_body():
    print("== chunked 大响应也必须读满 ==")
    big_png = b"\x89PNG\r\n\x1a\n" + os.urandom(400 * 1024)
    body = json.dumps(
        {"data": [{"b64_json": base64.b64encode(big_png).decode()}]}
    ).encode()

    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        for i in range(0, len(body), 32 * 1024):
            await resp.write(body[i : i + 32 * 1024])
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="gpt-image-1"
        )
        provider = OpenAICompatProvider(profile, request_timeout=20, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=1, ratio="1:1"
            )
        check(
            len(results) == 1 and results[0].content == big_png,
            "无 Content-Length 的 chunked 大响应读满",
        )
    finally:
        await runner.cleanup()


async def case_early_close_detected():
    print("== 声明大、连接提前关闭 ==")

    async def handler(request):
        resp = web.StreamResponse(
            headers={"Content-Type": "application/json", "Content-Length": "2269382"}
        )
        await resp.prepare(request)
        await resp.write(b'{"created":1,"data":[{"b64_json":"' + b"iVBORw0KGgo" * 500)
        request.transport.close()  # 只发第一块就断开
        return resp

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="gpt-image-1"
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            try:
                await provider.generate(
                    session, prompt="cat", negative="", n=1, ratio="1:1"
                )
                raised = None
            except ProviderError as exc:
                raised = exc
        check(raised is not None, "提前断开的响应会报错而不是返回半张图")
        check(
            raised is not None and "连接提前结束" in raised.message,
            "错误说明连接提前结束",
        )
        check(
            raised is not None
            and "收到" in raised.message
            and "2269382" in raised.message,
            "错误带收到/声明字节数",
        )
        check(raised is not None and raised.retryable, "该错误标记为可重试")
    finally:
        await runner.cleanup()


async def case_download_large_image():
    print("== URL 模式下载大图必须读满 ==")
    big_png = b"\x89PNG\r\n\x1a\n" + os.urandom(512 * 1024)

    async def gen(request):
        return web.json_response({"data": [{"url": base + "/big.png"}]})

    async def image(request):
        return web.Response(body=big_png, content_type="image/png")

    app = web.Application()
    app.router.add_post("/v1/images/generations", gen)
    app.router.add_get("/big.png", image)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="flux.1-schnell"
        )
        provider = OpenAICompatProvider(profile, request_timeout=20, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.generate(
                session, prompt="cat", negative="", n=1, ratio="1:1"
            )
        check(len(results) == 1, "URL 模式出图成功")
        check(results[0].content == big_png, f"下载读满（{len(big_png)} 字节未丢数据）")
    finally:
        await runner.cleanup()


async def case_error_body_truncated():
    print("== 错误响应体不完整仍按 HTTP 状态处理 ==")
    state = {"calls": 0}

    async def handler(request):
        state["calls"] += 1
        resp = web.StreamResponse(
            status=400,
            headers={"Content-Type": "application/json", "Content-Length": "1000"},
        )
        await resp.prepare(request)
        await resp.write(b'{"error":{"message":"bad request"')
        request.transport.close()
        return resp

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="gpt-image-1"
        )
        provider = OpenAICompatProvider(
            profile, request_timeout=10, max_retries=2, backoff=0.01
        )
        async with aiohttp.ClientSession() as session:
            try:
                await provider.generate(
                    session, prompt="cat", negative="", n=1, ratio="1:1"
                )
                raised = None
            except ProviderError as exc:
                raised = exc
        check(raised is not None and raised.status == 400, "保留 HTTP 400 状态")
        check(
            raised is not None and "HTTP 400" in raised.message,
            "错误信息以 HTTP 状态为主",
        )
        check(state["calls"] == 1, "4xx 截断不重试")
    finally:
        await runner.cleanup()


async def case_openai_edit():
    print("== OpenAI 兼容：图生图 /images/edits ==")
    captured = {}
    ref_bytes = b"\x89PNG\r\n\x1a\n" + os.urandom(1024)

    async def handler(request):
        form = await request.post()
        captured["model"] = form.get("model")
        captured["prompt"] = form.get("prompt")
        captured["n"] = form.get("n")
        captured["size"] = form.get("size")
        captured["response_format"] = form.get("response_format")
        field = form.get("image")
        captured["image"] = field.file.read() if hasattr(field, "file") else field
        return web.json_response({"data": [{"b64_json": PNG_B64}]})

    app = web.Application()
    app.router.add_post("/v1/images/edits", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai",
            api_key="sk-secret",
            base_url=base + "/v1",
            model="gpt-image-1",
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.edit(
                session,
                prompt="改成水墨风",
                negative="",
                n=1,
                ratio="1:1",
                image=ReferenceImage(ref_bytes, "image/png", "ref.png"),
            )
        check(
            len(results) == 1 and results[0].content == PNG, "edits 响应解析为图片字节"
        )
        check(captured["model"] == "gpt-image-1", "edits 透传模型")
        check(captured["prompt"] == "改成水墨风", "edits 透传提示词")
        check(captured["image"] == ref_bytes, "edits 上传参考图字节")
        check(
            captured["response_format"] is None,
            "gpt-image edits 不发送 response_format",
        )
        check(captured["size"] == "1024x1024", "edits 尺寸映射")
    finally:
        await runner.cleanup()


async def case_openai_edit_url():
    print("== OpenAI 兼容：图生图 url 返回 ==")
    formats = []

    async def edits(request):
        form = await request.post()
        formats.append(form.get("response_format"))
        return web.json_response({"data": [{"url": base + "/edited.png"}]})

    async def image(request):
        return web.Response(body=PNG, content_type="image/png")

    app = web.Application()
    app.router.add_post("/v1/images/edits", edits)
    app.router.add_get("/edited.png", image)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="flux.1-schnell"
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.edit(
                session,
                prompt="换背景",
                negative="",
                n=1,
                ratio="1:1",
                image=ReferenceImage(PNG, "image/png", "ref.png"),
            )
        check(formats == ["url"], "非 gpt-image 的 edits 优先请求 url 格式")
        check(
            len(results) == 1 and results[0].content == PNG, "edits 的 url 结果被下载"
        )
    finally:
        await runner.cleanup()


async def case_openai_edit_strip_format():
    print("== 图生图：不认 response_format 时去掉重试 ==")
    bodies = []

    async def handler(request):
        form = await request.post()
        bodies.append(form.get("response_format"))
        if form.get("response_format"):
            return web.json_response(
                {"error": {"message": "response_format is not supported"}}, status=400
            )
        return web.json_response({"data": [{"b64_json": PNG_B64}]})

    app = web.Application()
    app.router.add_post("/v1/images/edits", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="flux.1-schnell"
        )
        provider = OpenAICompatProvider(profile, request_timeout=10, max_retries=0)
        async with aiohttp.ClientSession() as session:
            results = await provider.edit(
                session,
                prompt="换背景",
                negative="",
                n=1,
                ratio="1:1",
                image=ReferenceImage(PNG, "image/png", "ref.png"),
            )
        check(bodies == ["url", None], "400 后去掉 response_format 重试一次")
        check(len(results) == 1 and results[0].content == PNG, "降级后图生图成功")
    finally:
        await runner.cleanup()


async def case_timeout_no_retry():
    print("== 生成请求超时不重试（防重复计费） ==")
    state = {"calls": 0}

    async def handler(request):
        state["calls"] += 1
        await asyncio.sleep(0.6)
        return web.json_response({"data": [{"b64_json": PNG_B64}]})

    app = web.Application()
    app.router.add_post("/v1/images/generations", handler)
    runner, base = await start(app)
    try:
        profile = ProviderProfile(
            kind="openai", api_key="k", base_url=base + "/v1", model="gpt-image-1"
        )
        provider = OpenAICompatProvider(
            profile, request_timeout=0.2, max_retries=2, backoff=0.01
        )
        async with aiohttp.ClientSession() as session:
            try:
                await provider.generate(
                    session, prompt="cat", negative="", n=1, ratio="1:1"
                )
                raised = None
            except ProviderError as exc:
                raised = exc
        check(state["calls"] == 1, f"超时只请求一次（实际 {state['calls']} 次）")
        check(raised is not None and "超时" in raised.message, "报错说明请求超时")
        check(
            raised is not None and "避免重复计费" in raised.message,
            "报错说明为避免重复计费已停止重试",
        )
    finally:
        await runner.cleanup()


def case_chain():
    print("== 服务商链 ==")
    settings = RuntimeSettings.from_mapping(
        {
            "provider": "openai",
            "api_key": "k",
            "base_url": "http://127.0.0.1:1/v1",
            "model": "gpt-image-1",
            "backup_enabled": True,
            "backup_provider": "mock",
        }
    )
    chain = build_chain(settings)
    check([p.kind for p in chain] == ["openai", "mock"], "主服务 + 备用按顺序构建")

    settings2 = RuntimeSettings.from_mapping(
        {"provider": "openai", "api_key": "", "model": ""}
    )
    check(build_chain(settings2) == [], "配置不完整时不构建任何服务商")


async def main():
    await case_openai_b64()
    await case_openai_url()
    await case_openai_retry()
    await case_openai_auth_error()
    await case_gemini()
    await case_dashscope()
    await case_dashscope_failed()
    await case_empty_response()
    await case_openai_strip_params()
    await case_bom_nul()
    await case_truncated_salvage()
    await case_truncated_retry()
    await case_openai_format_fallback()
    await case_truncated_prefix_salvage()
    await case_truncated_png_without_iend()
    await case_large_b64_body()
    await case_chunked_large_body()
    await case_early_close_detected()
    await case_download_large_image()
    await case_error_body_truncated()
    await case_openai_edit()
    await case_openai_edit_url()
    await case_openai_edit_strip_format()
    await case_timeout_no_retry()
    case_chain()
    print(f"\n全部通过：{passed} 项")


if __name__ == "__main__":
    asyncio.run(main())
