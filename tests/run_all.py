# -*- coding: utf-8 -*-
"""一键跑全部离线测试：python tests/run_all.py"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ("test_core.py", "test_providers.py", "test_harness.py")


def main():
    failed = []
    for name in SUITES:
        print("=" * 62, flush=True)
        print(f"▶ {name}", flush=True)
        code = subprocess.call([sys.executable, os.path.join(HERE, name)])
        if code != 0:
            failed.append(name)
    print("=" * 62, flush=True)
    if failed:
        print("❌ 失败：" + "、".join(failed), flush=True)
        return 1
    print("✅ 全部测试通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
