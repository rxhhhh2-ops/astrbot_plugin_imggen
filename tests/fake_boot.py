# -*- coding: utf-8 -*-
"""测试引导：把 fake_astrbot 放进 sys.path，再放插件根目录。"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fake_astrbot"))
sys.path.insert(0, os.path.dirname(HERE))
