"""存储端口。

**写操作一律原子**（先写临时文件再 `replace`）：断点续跑依赖状态文件，
写到一半被打断会同时毁掉状态和产物，而那正是最难查的一类故障。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.artifact import ArtifactRef
from ..domain.errors import Result
from ..domain.ids import RunId
from ..domain.run_state import RunState


@runtime_checkable
class StoragePort(Protocol):
    """原子读写 + 草稿纸上限（D-042）。"""

    def write_atomic(self, rel_path: str, content: str) -> Result[None]:
        """原子写文本。**不得**原地覆盖写。"""
        ...

    def read_text(self, rel_path: str) -> Result[str]: ...

    def exists(self, rel_path: str) -> bool: ...

    def list_dir(self, rel_path: str) -> tuple[str, ...]:
        """列出条目名。**不递归**，也不返回绝对路径。"""
        ...

    def save_run_state(self, state: RunState) -> Result[None]: ...

    def load_run_state(self, run_id: RunId) -> Result[RunState]: ...

    def put_artifact(self, ref: ArtifactRef, content: str) -> Result[None]: ...

    def draft_paper_ok(self, role: str, current_count: int) -> bool:
        """草稿纸是否还能再放一个文件（D-042 上限 3 个）。"""
        ...
