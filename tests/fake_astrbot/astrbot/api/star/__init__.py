# -*- coding: utf-8 -*-
"""假 Star / Context / StarTools。"""
import os
import tempfile
from pathlib import Path


class Context:
    def __init__(self):
        self.sent = []
        self.web_apis = []

    async def send_message(self, session, message_chain):
        self.sent.append((session, message_chain))
        return True

    async def get_using_provider(self, umo=None):
        return None

    async def get_using_provider_async(self, umo=None, **kwargs):
        return None

    def register_web_api(self, route, view_handler=None, methods=None, desc=None):
        self.web_apis.append(
            {"route": route, "handler": view_handler, "methods": methods, "desc": desc}
        )


class Star:
    def __init__(self, context: Context):
        self.context = context
        self._kv = {}

    async def put_kv_data(self, key, value):
        self._kv[key] = value

    async def get_kv_data(self, key, default=None):
        return self._kv.get(key, default)

    async def delete_kv_data(self, key):
        self._kv.pop(key, None)


class StarTools:
    @staticmethod
    def get_data_dir(plugin_name=None):
        root = os.environ.get("FAKE_IMGGEN_DATA")
        if not root:
            root = os.path.join(tempfile.gettempdir(), "fake_imggen_data")
        path = Path(root) / (plugin_name or "plugin")
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()
