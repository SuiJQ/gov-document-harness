"""`userfiles` 适配层：自动扫描登记 + 按需读取（D-022 / D-032）。

模块树（架构 §13）：`adapters/userfiles/{scanner.py, readers/}`。
"""

from __future__ import annotations

from . import scanner

__all__ = ["scanner"]
