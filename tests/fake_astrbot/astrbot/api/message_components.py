# -*- coding: utf-8 -*-
"""假消息组件：保留插件实际用到的构造方法。"""


class BaseMessageComponent:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class Plain(BaseMessageComponent):
    def __init__(self, text: str, **kwargs):
        super().__init__(text=text, **kwargs)


class Image(BaseMessageComponent):
    def __init__(self, file, **kwargs):
        super().__init__(file=file, **kwargs)

    @staticmethod
    def fromFileSystem(path, **kwargs):
        from pathlib import Path

        resolved = Path(path).resolve()
        return Image(file=str(resolved), path=str(resolved), **kwargs)

    @staticmethod
    def fromURL(url, **kwargs):
        return Image(file=url, url=url, **kwargs)

    @staticmethod
    def fromBase64(data, **kwargs):
        return Image(file=f"base64://{data}", **kwargs)

    @staticmethod
    def fromBytes(data: bytes):
        import base64

        return Image.fromBase64(base64.b64encode(data).decode())
