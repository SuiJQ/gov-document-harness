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


#: 一条待索引的文档：**ref 在前**。
#: 顺序与 `(ref, 正文)` 的直觉一致，且解包时写反立刻炸。
文档条目 = tuple[str, str]


@runtime_checkable
class RetrieverPort(Protocol):
    """知识检索。中文主路径为自建 n-gram 倒排（D-073）。"""

    def search(self, query: str, top_k: int = 8) -> Result[tuple[SearchHit, ...]]: ...

    def build(self, docs: tuple[文档条目, ...]) -> Result[None]:
        """建立/重建索引。`docs` 为 `(ref, 正文)` 序列。

        ## 为什么是 `(ref, 正文)` 对，不是两个平行元组

        第一版写成 `build(docs, refs)` 两个平行元组，
        写测试时立刻把 `(ref, 正文)` 整个当正文传——
        报错是「ref 数量与正文数量不符」，**当时差点以为是实现错了**。

        平行元组的代价是**调用方每次都要数参数**，
        错位时报错信息还指向另一个维度。`(ref, 正文)` 对不会错位：
        少写一个就是解包失败，写在错误的位置立刻炸。
        """
        ...
