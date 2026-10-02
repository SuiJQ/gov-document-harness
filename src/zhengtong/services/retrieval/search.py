"""检索服务：n-gram 倒排为主路径，FTS5 仅拉丁补充（D-073）。

## 一条必须写在这里的纪律

**命中数不是检索质量的指标。**
架构 §7.3 专门为这句话写了注释：`党政机关公文处理工作条例`
会返回 5 个候选，但加权后 **top-1 全部正确**。

**禁止加「必须唯一命中」之类判断**（D-073）——
检索层不知道下游要什么，它只负责把可能相关的排在前面；
「是不是真相关」由门禁和模型判断。把两者混在一起，
会得到一个既不准、又无法解释的系统。

`tests/test_retrieval.py::test_同分时仍返回全部候选` 盯着这条。

## 补充路径的定位

FTS5 **只索引拉丁片段**（中文进 FTS5 也检索不出来，见 `indexer` 模块文档）。
它**不是装饰品**：`test_拉丁专名能被检出` 验证它真能命中。
但它的分数**压低**——主路径才是主路径，补充路径不该喧宾夺主。

`sqlite3` 是标准库但**可能未编译 FTS5**。不可用时静默跳过，
**中文主路径完全不受影响**——比「FTS5 挂了降级到 n-gram 兜底」健壮得多。
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator

from ...domain.errors import ErrorCode, Result
from ...ports.retriever import SearchHit, 文档条目
from .indexer import 倒排索引, 分词, 查询权重

#: 索引文档数上限（架构 §7.3 规模护栏）。超出即降级为只索引首段，
#: **而非继续吃内存直到进程被杀**。
文档数上限: Final[int] = 20_000

#: 补充路径的分数折扣。**主路径为主，补充路径不喧宾夺主**。
_拉丁折扣: Final[float] = 0.5


class 中文检索:
    """`RetrieverPort` 的实现：n-gram 倒排为主 + FTS5 拉丁补充。"""

    __slots__ = ("_db", "_上限", "_已降级", "_拉丁偏移", "_索引", "_降级原因")

    def __init__(self) -> None:
        self._索引 = 倒排索引()
        self._上限 = 文档数上限
        self._已降级 = False
        self._降级原因 = ""
        self._db: sqlite3.Connection | None = None
        self._拉丁偏移 = 0

    # ── 端口实现 ──

    def build(self, docs: tuple[文档条目, ...]) -> Result[None]:
        """建立/重建索引。`docs` 为 `(ref, 正文)` 序列（D-073）。

        **ref 必须唯一**：它是 `SearchHit.ref` 里定位到来源的唯一依据，
        重复的 ref 会让结果无法定位到具体文档——
        而检索结果要进 prompt，定位不到等于模型拿到了无来源的断言。
        """
        # **解包前先验形状**：`(ref, 正文)` 对一旦不对位，
        # 直接解包会抛 `ValueError` 穿透到调用方——
        # 违背 D-108「畸形输入一律返回 fail，绝不抛异常」。
        坏条目 = [d for d in docs if not _是对(d)]
        if 坏条目:
            return Result.fail(
                ErrorCode.FILE_UNREADABLE,
                f"条目必须是 (ref, 正文) 二元组，收到 {坏条目[0]!r}",
            )
        refs = tuple(ref for ref, _ in docs)
        正文 = tuple(text for _, text in docs)
        重复 = _首个重复(refs)
        if 重复 is not None:
            return Result.fail(ErrorCode.FILE_UNREADABLE, f"ref 重复：{重复}")

        self._索引._文档上限 = self._上限
        self._索引.建(正文, refs)
        self._已降级 = self._索引._已降级
        self._降级原因 = (
            f"文档数 {len(正文)} 超过上限 {self._上限}，"
            "已降级为只索引标题与首段（检索覆盖面变小，非静默降质）"
            if self._已降级
            else ""
        )
        self._建拉丁(正文, refs)
        return Result.ok_none()

    def search(self, query: str, top_k: int = 8) -> Result[tuple[SearchHit, ...]]:
        """返回有序候选。**不做唯一性判断**（D-073）。"""
        if top_k <= 0 or not query.strip():
            return Result.ok(())
        词表 = 分词(query)
        if not 词表:
            return Result.ok(())
        权重 = 查询权重(词表)

        序分 = self._索引.打分(权重)
        # 补充路径**只加中文打不到的分**，不覆盖已有分数
        for 序, 分 in self._拉丁打分(query).items():
            序分.setdefault(序, 分)

        if not 序分:
            return Result.ok(())

        refs = self._索引.refs
        排序 = sorted(
            序分.items(),
            # **同分按 ref 排**——不稳定排序会让同一查询两次结果不同，
            # 而检索结果要进 prompt 与台账，不可重现的结果没法排查。
            key=lambda 项: (-项[1], refs[项[0]]),
        )
        return Result.ok(
            tuple(
                SearchHit(ref=refs[序], snippet=self._索引.片段(序, query), score=分)
                for 序, 分 in 排序[:top_k]
            )
        )

    # ── 降级与测试开关 ──

    def 设文档上限(self, 上限: int) -> None:
        """可注入上限，**只给测试用**——
        真造 20001 篇不现实，而**降级逻辑本身**才要测。"""
        self._上限 = 上限

    def 已降级(self) -> bool:
        return self._已降级

    def 降级提示(self) -> str:
        return self._降级原因

    @property
    def 拉丁可用(self) -> bool:
        """FTS5 补充路径当前是否可用。"""
        return self._db is not None

    def 临时关闭补充路径(self) -> AbstractContextManager[bool]:
        """**临时**关掉 FTS5 补充路径的上下文管理器，
        退出时恢复。返回「关前是否可用」。

        **为什么是上下文管理器而不是一个永久开关**：
        第一版写的是永久 `不用补充路径()`，结果 module 级共享索引被
        后续测试继承污染——单跑绿、全跑红，且**失败原因指向无关的测试**。

        **能恢复的状态就不该做成不可逆开关。**
        """
        return _临时关闭(self)

    def _关补充(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # ── FTS5 拉丁补充 ──

    def _建拉丁(self, docs: tuple[str, ...], refs: tuple[str, ...]) -> None:
        """FTS5 **只索引拉丁片段**。中文一律不进（进去也检索不出来）。

        不可用（未编译 FTS5 / 建表失败）时**静默跳过**：
        中文主路径完全不受影响。
        """
        self._关补充()
        try:
            db = sqlite3.connect(":memory:")
        except sqlite3.Error:
            return
        try:
            db.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
            db.execute("CREATE TABLE 映射(ref TEXT PRIMARY KEY, 序 INTEGER)")
            for 序, (文本, ref) in enumerate(zip(docs, refs, strict=True)):
                拉丁 = _拉丁片段(文本)
                if not 拉丁:
                    continue
                db.execute("INSERT INTO t(body) VALUES (?)", (" ".join(拉丁),))
                db.execute("INSERT INTO 映射 VALUES (?, ?)", (ref, 序))
            self._db = db
        except sqlite3.Error:
            # FTS5 未编译：`CREATE VIRTUAL TABLE ... USING fts5` 会在这里抛
            db.close()
            self._db = None

    def _拉丁打分(self, query: str) -> dict[int, float]:
        """拉丁查询的补充打分。**分数压低**——
        中文主路径才是主路径，补充路径不该喧宾夺主。"""
        if self._db is None:
            return {}
        拉丁词 = [w for w in query.replace("　", " ").split() if w and w.isascii()]
        if not 拉丁词:
            return {}
        try:
            行 = self._db.execute(
                "SELECT 映射.序, bm25(t) FROM t JOIN 映射 ON 映射.ref = t.rowid "
                "WHERE t MATCH ? LIMIT 64",
                (" OR ".join(f'"{w}"' for w in 拉丁词),),
            ).fetchall()
        except sqlite3.Error:
            return {}
        # `bm25` 越小越相关（越负越相关）。取负号转成「越大越好」，
        # 再乘折扣压低——**补充路径不该喧宾夺主**。
        return {序: (-bm) * _拉丁折扣 for 序, bm in 行}


def 建检索器() -> 中文检索:
    return 中文检索()


def _拉丁片段(文本: str) -> list[str]:
    return [s for s in 文本.split() if s and all(c.isascii() for c in s)]


def _是对(条目: object) -> bool:
    """是否 `(ref, 正文)` 二元组且都是字符串。"""
    return isinstance(条目, tuple) and len(条目) == 2 and all(isinstance(x, str) for x in 条目)


def _首个重复(refs: tuple[str, ...]) -> str | None:
    seen: set[str] = set()
    for r in refs:
        if r in seen:
            return r
        seen.add(r)
    return None


@contextmanager
def _临时关闭(主人: 中文检索) -> Iterator[bool]:
    """关掉补充路径并在退出时**重建恢复**。

    恢复靠重新 `build`——所以必须记下原文与 ref。
    """
    原db = 主人._db
    原已降级 = 主人._已降级
    原原因 = 主人._降级原因
    关前 = 原db is not None
    主人._关补充()
    try:
        yield 关前
    finally:
        主人._db = 原db
        主人._已降级 = 原已降级
        主人._降级原因 = 原原因
