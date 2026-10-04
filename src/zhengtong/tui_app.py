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

## 装配在这里，不在适配器里（B2）

适配器**不许 import 装配根**——那是另一种倒挂，
而且会让「按需导入 textual」失效：真要在适配器里造 App，
就得在模块顶层 import `zhengtong.app`，于是连 `--run` 也会被拖进来。

故：**适配器只认「给我一个工厂」这个形状**，
由本模块（入口，按职责属装配根）把真件注入进去。

## 装配失败时返回 2 而不是抛异常

目录缺失、配置读不出来、内容包校验不过——这些都在**起界面之前**发生。
界面一旦起来，报错就变成屏上的一行字，用户得自己判断什么意思。
直接退出并打印更省事（与 `--run` 的处置一致）。

## 实质在 `adapters/tui.py`。本模块存在的意义就是给 CLI 一个稳定的导入点。
"""

from __future__ import annotations

from pathlib import Path

from .adapters.terminal import 查终端显示能力
from .adapters.tui import ZhengTongApp
from .presentation.tui import 事件桥接

__all__ = ["ZhengTongApp", "启动"]


def 启动(根: Path | None = None) -> int:
    """起界面并跑到用户退出。返回退出码（`sys.exit` 留给最外层）。"""
    import sys

    from .app import build_app_from_workspace, 工作区

    目标 = 根 if 根 is not None else Path.cwd()
    got = build_app_from_workspace(工作区(目标))
    if got.is_error:
        错 = got.error
        assert 错 is not None
        sys.stderr.write(f"装配失败：{错.detail}\n")
        return 2

    应用 = got.unwrap()
    try:
        界面 = ZhengTongApp(
            桥=事件桥接(),
            根=目标,
            取编排器=应用.编排器,
            # **同一个闸门实例**：提问走工具层（`闸.问`）、
            # 答复走界面（`闸.挂答复`）。两侧不握同一个闸，
            # 提问方就永远等不到（D-287）。
            闸=应用.问答,
            # **字形提示（D-045，B4）**。在**装配根**算而不让界面自己去查——
            # 界面只负责显示，查不查是装配的事。
            # `默认 None`：测试与无界面模式不必关心终端能力。
            终端提示=查终端显示能力().提示 or None,
        )
        界面.run()
        return 0
    finally:
        # **必须收尾**（D-087）：总线不排空，事件随进程退出消失，
        # 用户看不到、日志里也没有；端口持有的连接池不关则耗尽。
        应用.close()
