"""内容包端口。

**代码不得硬编码任何公文内容**（D-055）：文种定义、红线 E1–E14、
门禁细则、D-Card 字段契约全在内容包里，代码只负责按需装载。
内容包是只读的，且可整体替换——换内容包就能换一套写作标准，
不需要改一行代码。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.errors import Result
from ..domain.role import RoleId


@runtime_checkable
class ContentPackPort(Protocol):
    """本地内容包 + manifest 校验。"""

    def load_role_prompt(self, role: RoleId) -> Result[str]:
        """按需装载角色提示词正文。**不做热重载**（D-044）。"""
        ...

    def load_reference(self, ref: str) -> Result[str]:
        """装载参考材料（如 GB/T 9704 全文）。"""
        ...

    def verify_manifest(self) -> Result[None]:
        """校验清单完整性。**启动时执行一次。**"""
        ...

    def version(self) -> str:
        """内容包版本。进日志，便于复现问题时对齐版本。"""
        ...
