"""L4 工具层（架构 §7.4，D-042 / D-046 / D-070）。

**工具全集 16 个**（架构 §7.4 表格逐行落实；正文说的「14」是表格行数）。

| 文件 | 职责 |
|---|---|
| `spec.py` | `ToolSpec` 数据定义 + `Effect` 副作用类别 |
| `registry.py` | 16 个工具清单 + 按角色过滤（**白名单第一重**） |
| `handlers.py` | 16 个工具的实现。**不拼路径**（D-076） |
| `runner.py` | 三重检查 + 一条审计的派发入口 |
| `audit.py` | 审计记录，只追加不改写 |
| `factory.py` | **构造入口**。接线在此，不在别处（D-195） |

**已砍除且代码里不允许复活**（D-042）：`fs.list` / `fs.write` / `fs.delete`。
"""

from __future__ import annotations

from .audit import 审计器, 审计条目
from .factory import 建工具运行器
from .handlers import 处理器表, 工具依赖
from .registry import TOOL_SPECS, 取工具, 该角色可调
from .runner import 工具运行器
from .spec import Effect, ToolSpec

__all__ = [
    "TOOL_SPECS",
    "Effect",
    "ToolSpec",
    "取工具",
    "处理器表",
    "审计器",
    "审计条目",
    "工具依赖",
    "工具运行器",
    "建工具运行器",
    "该角色可调",
]
