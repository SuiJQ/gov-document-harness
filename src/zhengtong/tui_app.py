"""TUI 入口：`from zhengtong.tui_app import 启动`。

## 为什么是这个模块名

`cli/main.py` 用 `importlib.import_module("zhengtong.tui_app")` 动态导入，
这样 `ModuleNotFoundError.name` 才能分清两种「没有」：

- `textual` → **用户的处置**是 `pip install textual`
- `zhengtong.tui_app` → **程序自身没接完**

第一版一律报「textual 没装」，而本地 textual 明明装好了——
用户照着提示装一遍，问题原样不变（违反 D-027）。

**静态 import 会被类型检查器当成欠债**：`reportMissingImports` 会把
「TUI 还没做完」这个如实状态变成一个类型错误，
逼着人写 `# type: ignore`（D-018 禁止）或者提前造假壳模块骗过检查器。

## 这里只做转发

实质在 `ui/app.py`。本模块存在的意义就是给 CLI 一个稳定的导入点。
"""

from __future__ import annotations

from .ui.app import ZhengTongApp, 启动

__all__ = ["ZhengTongApp", "启动"]
