"""L4 上下文管理（架构 §7.2，D-025 / D-039 / D-051 / D-077）。

**核心：上下文是「视图」，不是「累积的消息列表」。**

| 文件 | 职责 |
|---|---|
| `view.py` | `ContextView`：五字段全 tuple，真正不可变 |
| `composer.py` | 纯函数组装，不做判断 |
| `compactor.py` | 85% 触发 + D-039 保留优先级；摘要器注入 |
| `estimate.py` | **估算系数只此一处**（D-025 实测基线 ÷1.6） |
"""

from __future__ import annotations

from .compactor import 压缩, 压缩结果, 需要压缩
from .composer import 组装
from .estimate import 估算文本, 触发阈值
from .view import ContextView, 估算视图

__all__ = [
    "ContextView",
    "估算文本",
    "估算视图",
    "压缩",
    "压缩结果",
    "组装",
    "触发阈值",
    "需要压缩",
]
