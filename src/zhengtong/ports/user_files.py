"""用户文件端口。

两条纪律直接体现在签名上：

**一、`list_registered` 只回元信息，不回内容**（D-023）。
AI 初始只获得文件名，内容必须通过 `read_text` 按需读取。
这条如果漏了，等于让模型开局就把所有材料塞进上下文——
既烧 token，又违背"按需读取"的设计。

**二、没有任何写方法**（D-023）。
程序只读 `user/`，不得移动、改写、归档用户文件。
不给写端口，是让"做不到"比"不该做"更可靠。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.errors import Result
from ..domain.task import UserFile


@runtime_checkable
class UserFilesPort(Protocol):
    """`user/` 目录的只读访问 + 自动登记（D-022）。"""

    def scan_and_register(self) -> tuple[UserFile, ...]:
        """自动扫描并登记。**不逐项向用户确认**（D-022）。"""
        ...

    def list_registered(self) -> tuple[UserFile, ...]:
        """已登记文件的元信息。**不含内容。**"""
        ...

    def read_text(self, name: str) -> Result[str]:
        """按需读取内容。截断到上限由适配层负责（D-053 两万字）。"""
        ...
