# -*- coding: utf-8 -*-
"""插件层集成测试：用假 AstrBot + mock 服务商跑通命令、关键词触发与 llm_tool。

直接 python tests/test_harness.py 运行，不需要真实 AstrBot / 网络 / API Key。
"""

import asyncio
import base64
import importlib.util
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 每个测试进程用独立数据目录
os.environ.setdefault("FAKE_IMGGEN_DATA", tempfile.mkdtemp(prefix="imggen_harness_"))

sys.path.insert(0, HERE)
import fake_boot  # noqa: F401,E402
from astrbot.api import AstrBotConfig, REGISTERED_TOOLS  # noqa: E402
from astrbot.api.event import AstrMessageEvent  # noqa: E402
from astrbot.api.message_components import Image as FakeImage, Plain as FakePlain  # noqa: E402
from astrbot.api.star import Context  # noqa: E402
from core.providers.mock import render_placeholder  # noqa: E402

REF_PNG = render_placeholder(32, 32, "reference")

passed = 0


def check(cond, msg):
    global passed
    assert cond, "FAIL: " + msg
    passed += 1
    print("  ok -", msg)


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "imggen_main", os.path.join(ROOT, "main.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses 需要能从 sys.modules 找到模块
    spec.loader.exec_module(module)
    return module


MOD = load_plugin()
ImageGenPlugin = MOD.ImageGenPlugin
_CREATED = []


def make_plugin(**overrides):
    # 每个插件实例使用干净的额度状态，避免持久化文件影响其它用例
    try:
        data_root = Path(os.environ["FAKE_IMGGEN_DATA"]) / "astrbot_plugin_imggen"
        (data_root / "usage.json").unlink()
    except (KeyError, FileNotFoundError, OSError):
        pass
    config = {
        "provider": "mock",
        "api_key": "",
        "model": "",
        "auto_trigger": True,
        "cooldown_seconds": 0,
        "daily_limit": 0,
        "show_progress": False,
        "default_images": 1,
    }
    config.update(overrides)
    plugin = ImageGenPlugin(Context(), AstrBotConfig(config))
    _CREATED.append(plugin)
    return plugin


async def drive(handler, event):
    outs = []
    async for item in handler(event):
        outs.append(item)
    return outs


async def drive_like_astrbot(handler, event):
    """模拟 AstrBot 调度器：事件已停止时，后续 yield 不再进入发送阶段。"""
    outs = []
    agen = handler(event)
    async for item in agen:
        if event.is_stopped():
            break
        outs.append(item)
    if hasattr(agen, "aclose"):
        await agen.aclose()
    return outs


async def drain_bg(plugin):
    """等待工具后台 worker 处理完所有排队任务。"""
    queue = getattr(plugin, "_tool_queue", None)
    if queue is not None:
        await asyncio.wait_for(queue.join(), timeout=20)


def chain_paths(event):
    paths = []
    for chain in event.chains:
        for comp in chain:
            if isinstance(comp, FakeImage):
                paths.append(comp.path or comp.file)
    return paths


def chain_texts(event):
    texts = []
    for chain in event.chains:
        for comp in chain:
            if isinstance(comp, FakePlain):
                texts.append(comp.text)
    return texts


async def main():
    print("== 插件注册 ==")
    check("generate_image" in REGISTERED_TOOLS, "llm_tool generate_image 已注册")
    check("list_image_styles" in REGISTERED_TOOLS, "llm_tool list_image_styles 已注册")

    plugin = make_plugin()

    print("== /画 基础出图 ==")
    event = AstrMessageEvent("画 一只戴墨镜的橘猫")
    await drive(plugin.draw_cmd, event)
    paths = chain_paths(event)
    check(len(paths) == 1, "返回 1 张图片")
    check(Path(paths[0]).exists(), "图片文件已落盘")
    check(Path(paths[0]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "落盘文件是合法 PNG")
    check(any("绘制完成" in text for text in chain_texts(event)), "回复带完成说明")

    print("== 指定风格 ==")
    event = AstrMessageEvent("画 国风水墨 山间小筑")
    await drive(plugin.draw_cmd, event)
    check(
        any("国风水墨" in text for text in chain_texts(event)), "按名称指定国风水墨风格"
    )

    event = AstrMessageEvent("画 二次元 猫耳少女")
    await drive(plugin.draw_cmd, event)
    check(any("二次元" in text for text in chain_texts(event)), "按名称指定二次元风格")

    event = AstrMessageEvent("画 pixel 机器人")
    await drive(plugin.draw_cmd, event)
    check(any("像素风" in text for text in chain_texts(event)), "按 id 指定像素风")

    print("== 参数与比例 ==")
    event = AstrMessageEvent("画 赛博朋克 雨夜街景 --比例 16:9 --n 2")
    await drive(plugin.draw_cmd, event)
    paths = chain_paths(event)
    check(len(paths) == 2, "--n 2 生效")
    check(any("1024x576" in text for text in chain_texts(event)), "--比例 16:9 生效")

    event = AstrMessageEvent("画 写实摄影 猫 --n 99")
    await drive(plugin.draw_cmd, event)
    check(len(chain_paths(event)) == 4, "--n 受 max_images=4 约束")

    print("== 帮助与错误输入 ==")
    event = AstrMessageEvent("画")
    await drive(plugin.draw_cmd, event)
    check(event.texts and "生图用法" in event.texts[-1], "空参数返回用法")

    event = AstrMessageEvent("画 帮助")
    await drive(plugin.draw_cmd, event)
    check(any("也可以在聊天里" in text for text in event.texts), "帮助文案")

    event = AstrMessageEvent("画 未知风格 一只猫")
    await drive(plugin.draw_cmd, event)
    check(len(chain_paths(event)) == 1, "未知风格回退默认并照常出图")

    print("== 风格命令 ==")
    event = AstrMessageEvent("画风")
    await drive(plugin.styles_cmd, event)
    check(event.texts and "可用风格" in event.texts[-1], "/画风 列出风格")
    check(
        "国风水墨" in event.texts[-1] and "二次元" in event.texts[-1],
        "列表包含中英文名",
    )

    event = AstrMessageEvent("画风 详情 国风水墨")
    await drive(plugin.styles_cmd, event)
    check(
        "风格前缀" in event.texts[-1] and "guofeng" in event.texts[-1],
        "/画风 详情 展示模板",
    )

    print("== 状态 ==")
    event = AstrMessageEvent("画状态")
    await drive(plugin.status_cmd, event)
    status = event.texts[-1]
    check("服务商" in status and "风格数" in status, "状态包含服务商与风格数")
    check("本地图片" in status, "状态包含本地存储")

    print("== 关键词自动触发 ==")
    event = AstrMessageEvent("画一张 小房子", group_id="g1")
    await drive(plugin.auto_trigger, event)
    check(event.call_llm is True, "触发后禁止默认 LLM 请求")
    check(not event.stopped, "触发后不能停止事件，否则调度器会丢弃结果")
    check(len(chain_paths(event)) == 1, "关键词触发出图")

    # 回归：show_progress=True 时第一个 yield 发生在服务商请求之前。
    # 旧实现先 stop_event()，调度器会在第一个 yield 处丢掉整个事件链，
    # 生成器被挂起，服务商根本收不到请求。
    print("== 关键词触发在真实调度语义下不被吞掉 ==")
    progress_plugin = make_plugin(show_progress=True)
    event = AstrMessageEvent("画一张 小房子", group_id="g1")
    outs = await drive_like_astrbot(progress_plugin.auto_trigger, event)
    check(not event.stopped, "关键词触发不会提前停止事件")
    check(event.call_llm is True, "关键词触发只禁止默认 LLM，不阻断发送链路")
    check(len(outs) >= 1, "进度与结果能通过调度器")
    check(len(chain_paths(event)) == 1, "调度语义下仍能拿到图片")

    event = AstrMessageEvent("帮我画 一只柴犬", group_id="g1")
    await drive(plugin.auto_trigger, event)
    check(len(chain_paths(event)) == 1, "“帮我画”前缀触发")

    event = AstrMessageEvent("/画 一只猫")
    await drive(plugin.auto_trigger, event)
    check(not event.stopped and not event.results, "斜杠命令不重复触发")

    event = AstrMessageEvent("今天天气不错")
    await drive(plugin.auto_trigger, event)
    check(not event.results, "普通聊天不触发")

    print("== 关键词触发后的提示词剥离 ==")
    event = AstrMessageEvent("画一张 戴帽子的猫", group_id="g1")
    await drive(plugin.auto_trigger, event)
    check(len(chain_paths(event)) == 1, "触发后剩余描述可正常出图")

    print("== llm_tool ==")
    tool_event = AstrMessageEvent("帮我画一只赛博朋克猫")
    reply = await REGISTERED_TOOLS["generate_image"](
        plugin, tool_event, prompt="赛博朋克风格的猫", style="anime"
    )
    check("已开始生成" in reply, "generate_image 立即返回已开始生成")
    check(len(tool_event.results) == 0, "工具不通过事件回包，而是主动发送")
    await drain_bg(plugin)
    check(len(plugin.context.sent) == 1, "后台完成后 context.send_message 主动发图")
    sent_chain = plugin.context.sent[0][1]
    check(any(isinstance(c, FakeImage) for c in sent_chain), "主动发送的消息包含图片")

    listed = await REGISTERED_TOOLS["list_image_styles"](plugin, AstrMessageEvent(""))
    check("二次元" in listed and "anime" in listed, "list_image_styles 返回风格与 id")

    print("== llm_tool 参数容错 ==")
    sent_before = len(plugin.context.sent)
    tool_event = AstrMessageEvent("画一张赛博朋克猫")
    reply = await REGISTERED_TOOLS["generate_image"](
        plugin,
        tool_event,
        prompt="赛博朋克猫",
        aspect_ratio="16:9",
        num_images=2,
        quality="hd",
        seed=42,
    )
    check("已开始生成 2 张" in reply, "工具吸收未知参数并按 num_images 生成 2 张")
    await drain_bg(plugin)
    new_sent = plugin.context.sent[sent_before:]
    check(
        bool(new_sent) and sum(isinstance(c, FakeImage) for c in new_sent[-1][1]) == 2,
        "主动发送的消息包含 2 张图片",
    )

    reply = await REGISTERED_TOOLS["generate_image"](
        plugin, AstrMessageEvent(""), aspect_ratio="1:1"
    )
    check("请先给出" in reply, "缺少 prompt 时给出提示而不是 TypeError")

    print("== llm_tool 不阻塞（避开 120s 工具超时） ==")
    slow_plugin = make_plugin()
    original_create = slow_plugin._create_images
    gate = asyncio.Event()

    async def gated_create(*args, **kwargs):
        await gate.wait()
        return await original_create(*args, **kwargs)

    slow_plugin._create_images = gated_create
    started = time.monotonic()
    reply = await REGISTERED_TOOLS["generate_image"](
        slow_plugin, AstrMessageEvent("画 慢图"), prompt="慢图"
    )
    elapsed = time.monotonic() - started
    check("已开始生成" in reply, "生成未完成时工具也先返回")
    check(bool(slow_plugin._tool_workers), "后台 worker 池已启动并等待")
    check(elapsed < 0.5, f"工具没有等待生成完成（{elapsed:.3f}s）")
    gate.set()
    await drain_bg(slow_plugin)
    check(len(slow_plugin.context.sent) == 1, "后台完成后主动发图")

    print("== 任务列表与取消 ==")
    job_plugin = make_plugin(max_concurrency=1)
    original_job_create = job_plugin._create_images
    job_gate = asyncio.Event()
    job_started = asyncio.Event()

    async def gated_job_create(*args, **kwargs):
        job_started.set()
        await job_gate.wait()
        return await original_job_create(*args, **kwargs)

    job_plugin._create_images = gated_job_create
    reply = await REGISTERED_TOOLS["generate_image"](
        job_plugin, AstrMessageEvent("画 慢任务"), prompt="慢任务"
    )
    match = re.search(r"#([0-9a-f]{6})", reply)
    check(match is not None, "工具返回任务编号")
    job_id = match.group(1) if match else ""
    await asyncio.wait_for(job_started.wait(), timeout=2)

    task_event = AstrMessageEvent("画任务")
    await drive(job_plugin.tasks_cmd, task_event)
    check(
        bool(task_event.texts) and f"#{job_id}" in task_event.texts[-1],
        "画任务 列出运行中的任务",
    )
    check("生成中" in task_event.texts[-1], "任务状态显示生成中")

    cancel_event = AstrMessageEvent(f"画取消 {job_id}")
    await drive(job_plugin.cancel_cmd, cancel_event)
    check(
        bool(cancel_event.texts) and "已取消 1 个任务" in cancel_event.texts[-1],
        "画取消 终止运行中的任务",
    )
    await drain_bg(job_plugin)
    await asyncio.sleep(0)
    check(
        job_plugin._tool_jobs[job_id]["status"] == "cancelled", "任务记录标记为已取消"
    )
    check(len(job_plugin.context.sent) == 0, "取消后不会发送图片")

    print("== 排队任务取消 ==")
    queue_plugin = make_plugin(max_concurrency=1)
    original_queue_create = queue_plugin._create_images
    queue_gate = asyncio.Event()
    queue_started = asyncio.Event()

    async def gated_queue_create(*args, **kwargs):
        queue_started.set()
        await queue_gate.wait()
        return await original_queue_create(*args, **kwargs)

    queue_plugin._create_images = gated_queue_create
    first_reply = await REGISTERED_TOOLS["generate_image"](
        queue_plugin, AstrMessageEvent("画 占位", sender_id="20001"), prompt="占位"
    )
    first_id = re.search(r"#([0-9a-f]{6})", first_reply).group(1)
    await asyncio.wait_for(queue_started.wait(), timeout=2)

    second_reply = await REGISTERED_TOOLS["generate_image"](
        queue_plugin, AstrMessageEvent("画 排队", sender_id="20002"), prompt="排队"
    )
    second_id = re.search(r"#([0-9a-f]{6})", second_reply).group(1)
    check(
        queue_plugin._tool_jobs[second_id]["status"] == "queued",
        "不同用户的第二个任务进入排队",
    )

    queued_cancel = AstrMessageEvent(f"画取消 {second_id}", sender_id="20002")
    await drive(queue_plugin.cancel_cmd, queued_cancel)
    check(
        queue_plugin._tool_jobs[second_id]["status"] == "cancelled",
        "排队任务可立即取消",
    )
    queue_gate.set()
    await drain_bg(queue_plugin)
    await asyncio.sleep(0)
    check(len(queue_plugin.context.sent) == 1, "只发送未被取消的任务")
    check(
        queue_plugin._tool_jobs[first_id]["status"] == "success",
        "先运行的任务正常完成",
    )

    print("== 熔断 ==")
    breaker_plugin = make_plugin()
    breaker_plugin._ensure()
    provider = breaker_plugin._chain[0]
    calls = {"n": 0}

    async def failing_generate(*args, **kwargs):
        calls["n"] += 1
        raise MOD.ProviderError("模拟上游失败", retryable=True)

    provider.generate = failing_generate
    for _ in range(3):
        try:
            await breaker_plugin._create_images("猫", "", 1, "1:1")
        except MOD.ProviderError:
            pass
    check(calls["n"] == 3, "熔断前三轮都调用服务商")
    try:
        await breaker_plugin._create_images("猫", "", 1, "1:1")
        raised = None
    except MOD.ProviderError as exc:
        raised = exc
    check(calls["n"] == 3, "熔断后不再调用服务商")
    check(raised is not None and "熔断" in raised.message, "返回熔断提示")

    print("== 黑名单 ==")
    blocked = make_plugin(blocked_words="违规词,猫")
    event = AstrMessageEvent("画 一只猫")
    await drive(blocked.draw_cmd, event)
    check(event.texts and "禁止" in event.texts[-1], "命中黑名单被拒绝")

    print("== 冷却 ==")
    cooled = make_plugin(cooldown_seconds=60)
    event = AstrMessageEvent("画 第一只")
    await drive(cooled.draw_cmd, event)
    check(len(chain_paths(event)) == 1, "冷却前首次可出图")
    event = AstrMessageEvent("画 第二只")
    await drive(cooled.draw_cmd, event)
    check(event.texts and "冷却" in event.texts[-1], "冷却期内被拒绝")

    print("== 每日额度 ==")
    limited = make_plugin(daily_limit=1)
    event = AstrMessageEvent("画 第一只")
    await drive(limited.draw_cmd, event)
    check(len(chain_paths(event)) == 1, "额度内可出图")
    event = AstrMessageEvent("画 第二只")
    await drive(limited.draw_cmd, event)
    check(event.texts and "额度" in event.texts[-1], "超额被拒绝")

    print("== 群聊/私聊开关 ==")
    group_off = make_plugin(allow_group=False)
    event = AstrMessageEvent("画 猫", group_id="g1")
    await drive(group_off.draw_cmd, event)
    check(event.texts and "群聊" in event.texts[-1], "关闭群聊后群消息被拒绝")

    print("== 未配置服务商 ==")
    unconfigured = make_plugin(provider="openai", api_key="", model="")
    event = AstrMessageEvent("画 猫")
    await drive(unconfigured.draw_cmd, event)
    check(
        any("还没有可用的生图服务" in text for text in event.texts),
        "缺配置时给出明确指引",
    )

    print("== 主服务失败 → 备用降级 ==")
    fallback = make_plugin(
        provider="openai",
        api_key="sk-fake",
        base_url="http://127.0.0.1:1/v1",
        model="gpt-image-1",
        max_retries=0,
        request_timeout=5,
        backup_enabled=True,
        backup_provider="mock",
    )
    event = AstrMessageEvent("画 降级测试")
    await drive(fallback.draw_cmd, event)
    check(len(chain_paths(event)) == 1, "主服务不可达时由备用服务商出图")
    check(
        any("绘制完成" in text for text in chain_texts(event)), "降级后仍返回完成说明"
    )

    print("== 图生图（QQ 附图） ==")
    ref_plugin = make_plugin()
    ref_event = AstrMessageEvent("画", message=[FakeImage.fromBytes(REF_PNG)])
    await drive(ref_plugin.draw_cmd, ref_event)
    ref_paths = chain_paths(ref_event)
    check(len(ref_paths) == 1, "附图后返回 1 张图")
    check(Path(ref_paths[0]).read_bytes() == REF_PNG, "mock 图生图原样返回参考图")
    check(
        any("图生图完成" in text for text in chain_texts(ref_event)),
        "回复标注图生图完成",
    )

    print("== 插件页面接口 ==")
    check(len(plugin.context.web_apis) == 9, "启动时注册了 9 个界面接口")
    check(
        any(
            item["route"] == "/astrbot_plugin_imggen/state"
            for item in plugin.context.web_apis
        ),
        "接口路由带插件名前缀",
    )

    state_res = await plugin.api_state()
    check(state_res.get("status") == "ok", "state 接口返回 ok")
    state_data = state_res["data"]
    check(len(state_data["styles"]) >= 30, "state 返回风格列表")
    check(state_data["provider"]["ready"] is True, "state 返回服务商可用状态")

    async def fake_body():
        return {
            "prompt": "一只戴墨镜的橘猫",
            "style": "anime",
            "ratio": "16:9",
            "count": 2,
        }

    plugin._json_body = fake_body
    gen = await plugin.api_generate()
    check(gen.get("status") == "ok", "generate 接口返回 ok")
    gen_data = gen["data"]
    check(len(gen_data["images"]) == 2, "generate 返回 2 张图")
    check(
        gen_data["images"][0]["thumb"].startswith("data:image/jpeg;base64,"),
        "generate 返回缩略图 data URI",
    )
    check(gen_data["style"]["name"] == "二次元", "界面按指定风格生成")
    check(gen_data["ratio"] == "16:9", "界面按指定比例生成")
    image_name = gen_data["images"][0]["name"]
    check((Path(plugin._store.root) / image_name).is_file(), "界面生成的图片已落盘")

    hist = await plugin.api_history()
    check(hist["status"] == "ok" and hist["data"]["items"], "history 返回最近图片")

    plugin._query_str = lambda key, default="": image_name
    thumb = await plugin.api_thumb()
    check(thumb["status"] == "ok" and thumb["data"]["thumb"], "thumb 返回缩略图")
    image = await plugin.api_image()
    check(
        isinstance(image, dict) and Path(image["path"]).is_file(), "image 返回文件响应"
    )

    plugin._query_str = lambda key, default="": "../etc/passwd"
    bad = await plugin.api_thumb()
    check(bad["status"] == "error", "非法图片名被拒绝")

    cleanup = await plugin.api_cleanup()
    check(
        cleanup["status"] == "ok" and "remaining" in cleanup["data"],
        "cleanup 返回清理统计",
    )

    reload_res = await plugin.api_reload()
    check(
        reload_res["status"] == "ok" and reload_res["data"]["styles"] >= 30,
        "reload 重读配置与风格",
    )

    async def empty_body():
        return {}

    plugin._json_body = empty_body
    empty = await plugin.api_generate()
    check(
        empty["status"] == "error" and "画面描述" in empty["message"],
        "界面空描述被拒绝",
    )

    print("== 工作台图生图 ==")
    ref_b64 = base64.b64encode(REF_PNG).decode()

    async def image_body():
        return {
            "prompt": "",
            "image": "data:image/png;base64," + ref_b64,
            "ratio": "1:1",
            "count": 1,
        }

    plugin._json_body = image_body
    edited_res = await plugin.api_generate()
    check(edited_res.get("status") == "ok", "工作台图生图返回 ok")
    check(edited_res["data"]["edited"] is True, "工作台返回 edited 标记")
    edited_name = edited_res["data"]["images"][0]["name"]
    check(
        (Path(plugin._store.root) / edited_name).read_bytes() == REF_PNG,
        "工作台图生图使用参考图字节",
    )

    async def bad_image_body():
        return {"prompt": "改图", "image": "data:image/png;base64,AAAA"}

    plugin._json_body = bad_image_body
    bad_image = await plugin.api_generate()
    check(
        bad_image["status"] == "error" and "参考图" in bad_image["message"],
        "非法参考图被拒绝",
    )

    gemini_plugin = make_plugin(
        provider="gemini", api_key="k", model="gemini-2.5-flash-image"
    )
    gemini_plugin._json_body = image_body
    unsupported = await gemini_plugin.api_generate()
    check(
        unsupported["status"] == "error" and "不支持图生图" in unsupported["message"],
        "非 OpenAI 兼容服务商拒绝图生图",
    )

    print("== 工作台设置接口 ==")
    settings_res = await plugin.api_settings()
    check(settings_res.get("status") == "ok", "settings 接口返回 ok")
    settings_data = settings_res["data"]
    check(
        [item["id"] for item in settings_data["providers"]]
        == ["openai", "gemini", "dashscope", "mock"],
        "settings 返回 4 个服务商选项",
    )
    check(settings_data["provider"] == "mock", "settings 回显当前服务商")
    check(settings_data["has_api_key"] is False, "mock 初始没有密钥")
    check(len(settings_data["styles"]) >= 30, "settings 返回默认风格选项")

    def settings_payload(**overrides):
        body = {
            "provider": "openai",
            "model": "gpt-image-1",
            "base_url": "",
            "api_key": "sk-test-abcd1234",
            "quality": "auto",
            "openai_response_format": "auto",
            "backup_enabled": False,
            "backup_provider": "mock",
            "backup_model": "",
            "backup_base_url": "",
            "backup_api_key": "",
            "default_style": "anime",
            "default_ratio": "16:9",
            "default_images": 2,
        }
        body.update(overrides)
        return body

    async def save_main():
        return settings_payload()

    plugin._json_body = save_main
    saved = await plugin.api_save_settings()
    check(saved.get("status") == "ok", "保存设置成功")
    check(saved["data"]["persisted"] is True, "配置已写回（假 AstrBot）")
    check(plugin.config["provider"] == "openai", "provider 写入配置")
    check(plugin.config["api_key"] == "sk-test-abcd1234", "API Key 写入配置")
    check(plugin.settings.primary.kind == "openai", "保存后立即重载服务商")
    check(plugin.settings.default_style == "anime", "默认风格已保存")
    check(plugin.settings.default_ratio == "16:9", "默认比例已保存")
    check(plugin.settings.default_images == 2, "默认张数已保存")
    check(saved["data"]["ready"] is True, "填好密钥后服务商可用")

    masked = await plugin.api_settings()
    check(masked["data"]["api_key_masked"] == "sk-****1234", "settings 只返回密钥掩码")

    async def save_keep_key():
        return settings_payload(model="dall-e-3", api_key="")

    plugin._json_body = save_keep_key
    kept = await plugin.api_save_settings()
    check(kept["status"] == "ok", "空密钥保存成功")
    check(plugin.config["api_key"] == "sk-test-abcd1234", "空密钥保留原值")

    async def save_clear_key():
        return settings_payload(model="dall-e-3", api_key="", clear_api_key=True)

    plugin._json_body = save_clear_key
    cleared = await plugin.api_save_settings()
    check(plugin.config["api_key"] == "", "可以显式清空密钥")
    check(cleared["data"]["ready"] is False, "无密钥时提示未就绪")
    check("尚未配置完整" in cleared["data"]["warning"], "返回未就绪警告")

    async def save_bad_provider():
        return {"provider": "nope"}

    plugin._json_body = save_bad_provider
    bad_provider = await plugin.api_save_settings()
    check(
        bad_provider["status"] == "error" and "服务商" in bad_provider["message"],
        "非法服务商被拒绝",
    )

    async def save_bad_base():
        return {"provider": "openai", "base_url": "ftp://example.com"}

    plugin._json_body = save_bad_base
    bad_base = await plugin.api_save_settings()
    check(
        bad_base["status"] == "error" and "http" in bad_base["message"],
        "非法接口地址被拒绝",
    )

    async def save_bad_style():
        return {"provider": "mock", "default_style": "zzz-not-a-style"}

    plugin._json_body = save_bad_style
    bad_style = await plugin.api_save_settings()
    check(
        bad_style["status"] == "error" and "风格" in bad_style["message"],
        "未知默认风格被拒绝",
    )

    print("== 生命周期 ==")
    await plugin.terminate()
    check(plugin._http is None, "terminate 关闭并释放 HTTP 会话")
    for extra in _CREATED:
        await extra.terminate()

    print(f"\n全部通过：{passed} 项")


if __name__ == "__main__":
    asyncio.run(main())
