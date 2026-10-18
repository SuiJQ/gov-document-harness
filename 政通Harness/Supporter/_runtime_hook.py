"""PyInstaller 运行时钩子：让 `__main__.py` 的相对导入能解析。

问题：`zhengtong/__main__.py` 里有 `from .cli.main import main`，
PyInstaller 直接执行 `__main__.py` 时没有父包上下文，相对导入失败。

方案：在 bootstrap 阶段把 `zhengtong` 包注册到 `sys.modules`，
这样 `__main__.py` 的相对导入就能解析。
"""

import importlib
import sys


def _register_zhengtong_package():
    """把 zhengtong 包及其子模块注册到 sys.modules。"""
    if "zhengtong" in sys.modules:
        return
    # 先导入 zhengtong 包本身（会触发 __init__.py）
    importlib.import_module("zhengtong")


_register_zhengtong_package()