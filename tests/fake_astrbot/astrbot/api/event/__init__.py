# -*- coding: utf-8 -*-
"""假事件/过滤器：与真实 AstrBot 的关键签名保持一致。"""

import asyncio

from astrbot.api import llm_tool  # noqa: F401  兼容 from astrbot.api import llm_tool


class EventMessageType:
    GROUP_MESSAGE = 1
    PRIVATE_MESSAGE = 2
    OTHER_MESSAGE = 4
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


class MessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])

    def __iter__(self):
        return iter(self.chain)

    def __len__(self):
        return len(self.chain)

    # 与真实框架一致的链式构建接口
    def message(self, text):
        from astrbot.api.message_components import Plain

        self.chain.append(Plain(text))
        return self

    def file_image(self, path):
        from astrbot.api.message_components import Image

        self.chain.append(Image.fromFileSystem(path))
        return self

    def image(self, url):
        from astrbot.api.message_components import Image

        self.chain.append(Image.fromURL(url))
        return self


class _Filter:
    """记录装饰器调用但不真正注册；handler 直接通过实例属性访问。"""

    EventMessageType = EventMessageType

    def command(self, name, alias=None, priority=0, **kwargs):
        def deco(fn):
            fn._cmd = name
            fn._alias = alias or set()
            fn._priority = priority
            return fn

        return deco

    def command_group(self, name):
        def deco(fn):
            fn._cmd_group = name
            return fn

        return deco

    def event_message_type(self, t):
        def deco(fn):
            fn._event_message_type = t
            return fn

        return deco

    def permission_type(self, t):
        return lambda fn: fn

    def regex(self, pattern):
        return lambda fn: fn

    def platform_adapter_type(self, t):
        return lambda fn: fn

    def on_astrbot_loaded(self):
        return lambda fn: fn

    def llm_tool(self, name=None, **kwargs):
        return llm_tool(name=name, **kwargs)


filter = _filter = _Filter()


class AstrMessageEvent:
    def __init__(
        self,
        message_str: str,
        umo: str = "test:GroupMessage:1",
        sender_name: str = "测试用户",
        sender_id: str = "10001",
        group_id: str = "",
        admin: bool = False,
        platform: str = "aiocqhttp",
        message=None,
    ):
        self.message_str = message_str
        self.message_obj = type(
            "M",
            (),
            {"message_str": message_str, "message": list(message or [])},
        )()
        self.unified_msg_origin = umo
        self.sender_name = sender_name
        self.sender_id = sender_id
        self.group_id = group_id
        self.platform = platform
        self.admin = admin
        self.results = []
        self.stopped = False
        self.call_llm = False
        self._has_send_oper = False

    # ----- 结果构造 -----
    def plain_result(self, text):
        self.results.append(("plain", text))
        return ("plain", text)

    def chain_result(self, chain):
        self.results.append(("chain", chain))
        return ("chain", chain)

    def image_result(self, path):
        self.results.append(("image", path))
        return ("image", path)

    # ----- 事件信息 -----
    def get_sender_name(self):
        return self.sender_name

    def get_sender_id(self):
        return self.sender_id

    def get_group_id(self):
        return self.group_id

    def get_session_id(self):
        return self.sender_id

    def get_platform_name(self):
        return self.platform

    def is_admin(self):
        return self.admin

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    def should_call_llm(self, call_llm):
        """与 AstrBot 一致：call_llm=True 表示跳过默认 LLM 请求。"""
        self.call_llm = call_llm

    # ----- 便捷断言 -----
    @property
    def texts(self):
        return [content for kind, content in self.results if kind == "plain"]

    @property
    def chains(self):
        return [content for kind, content in self.results if kind == "chain"]


def run(handler, event):
    """驱动 async 生成器 handler 到结束，返回产出列表。"""

    async def _drive():
        outs = []
        agen = handler(event)
        if hasattr(agen, "__aiter__"):
            async for item in agen:
                outs.append(item)
        else:
            await agen
        return outs

    return asyncio.get_event_loop().run_until_complete(_drive())
