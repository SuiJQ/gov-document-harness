"""`userfiles` 的读取器集合（D-032：txt/md/docx，零第三方依赖）。

`pdf` / `doc` / 图片**不实现解析**——只登记文件名并在界面提示。
给它们留个空 `__init__` 导出等于暗示「支持」，故这里只导出三个真实现。
"""

from __future__ import annotations

from . import docx, txt_md

__all__ = ["docx", "txt_md"]
