# -*- coding: utf-8 -*-
"""假 astrbot.api.web：供插件页面后端测试使用（真实环境用 AstrBot 自带实现）。"""


class _MultiDict:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, name, default=None, type=None):  # noqa: A002 - 对齐真实签名
        value = self.data.get(name, default)
        if type is not None and value is not None:
            try:
                return type(value)
            except (TypeError, ValueError):
                return default
        return value


class _FakeRequest:
    def __init__(self):
        self.method = "GET"
        self.path = "/"
        self.plugin_name = "astrbot_plugin_imggen"
        self.username = "tester"
        self.headers = {}
        self.cookies = {}
        self.content_type = ""
        self.client_host = "127.0.0.1"
        self.path_params = {}
        self.query = _MultiDict()
        self.args = self.query

    async def json(self, default=None):
        return default if default is not None else {}

    async def get_json(self):
        return {}

    async def body(self):
        return b""

    async def form(self):
        return {}

    async def files(self):
        return {}


request = _FakeRequest()


def json_response(data, status_code=200):
    if isinstance(data, dict) and "status" in data:
        return data
    return {"status": "ok", "data": data}


def error_response(message, status_code=400):
    return {"status": "error", "message": message}


async def file_response(path, filename=None, content_type=None):
    return {"path": str(path), "filename": filename or ""}
