"""检索端口。

**返回有序候选，不做"必须唯一命中"判断**（D-073）：
检索层不知道下游要什么，它只负责把可能相关的排在前面。
"是不是真相关"由门禁和模型判断——把它们混在一起，
会得到一个既不准、又无法解释的系统。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.errors import Result


@dataclass(frozen=True, slots=True)
class SearchHit:
    """一条检索命中。`score` 越大越相关；同分按 `ref` 排序以保证可重现。"""

    ref: str
    snippet: str
    score: float


@runtime_checkable
class RetrieverPort(Protocol):
    """知识检索。中文主路径为自建 n-gram 倒排（D-073）。"""

    def search(self, query: str, top_k: int = 8) -> Result[tuple[SearchHit, ...]]: ...

    def build(self, docs: tuple[str, ...]) -> Result[None]:
        """建立/重建索引。`docs` 为内容条目。"""
        ...
