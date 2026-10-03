"""交互界面（L6 接入层的一部分）。

**本文件刻意为空，不重新导出任何东西。**

## 为什么不在这里导出 `ZhengTongApp`

第一版写了 `from .app import ZhengTongApp, 启动`，
类型检查直接报**导入环**：`ui/__init__` → `app` → `from . import theme` → `ui/__init__`。

而这个导出**本来就没人用**——本文件开头的文档已经写明：
textual 是重依赖，**只有 `tui_app.py`（入口）按需导入**，
CLI 的 `--tui` 走 `importlib.import_module`，
正是为了区分「没装依赖」与「程序没接完」（D-287 的教训）。

故正确做法是**删掉**这个导出，而不是调换导入顺序去绕开环——
绕开的只是类型检查器的抱怨，依赖关系还在。
"""
