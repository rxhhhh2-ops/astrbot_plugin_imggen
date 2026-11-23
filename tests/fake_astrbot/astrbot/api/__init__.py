# -*- coding: utf-8 -*-
"""极简假 AstrBot：只实现插件用到的 API，用于离线测试。"""
import logging


class _Logger:
    def info(self, *a):
        print("[log]", *a)

    def warning(self, *a):
        print("[warn]", *a)

    def error(self, *a):
        print("[err]", *a)

    def debug(self, *a):
        pass


logger = _Logger()


class AstrBotConfig(dict):
    def save_config(self):
        pass


# llm_tool：记录注册但不真正解析 docstring（测试环境无需 docstring_parser）
REGISTERED_TOOLS = {}


def llm_tool(name=None, **kwargs):
    def deco(fn):
        tool_name = name or fn.__name__
        fn._llm_tool = tool_name
        REGISTERED_TOOLS[tool_name] = fn
        return fn

    return deco
