"""工具审计记录（架构 §7.4「每次调用三重检查 + 一条审计记录」）。

## 为什么每条都要有裁决

一次工具调用要么被允许、要么被拒绝，两种都留痕。
**只记成功不记失败，等于让越权尝试凭空消失**——
而越权尝试恰恰是最该被看见的东西（D-042 的白名单是安全边界，不是性能优化）。

## 参数为什么要脱敏

审计记录最终进 journal（D-035要求）。参数里可能出现
文件路径、用户材料内容、甚至不该进日志的片段。
`Redactor` 是事件总线那一侧的脱敏器，
**审计这条路径不经过事件总线**，所以必须自己过一遍。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...domain.role import RoleId


@dataclass(frozen=True, slots=True)
class 审计条目:
    """一次工具调用的完整留痕。"""

    工具名: str
    角色: RoleId
    允许: bool
    裁决: str  # 人可读的中文裁决说明（D-030：用户交互用严谨中文）
    时间戳: float
    参数摘要: str = ""
    结果摘要: str = ""
    错误码: str = ""

    @property
    def 是拒绝(self) -> bool:
        return not self.允许


class 审计器:
    """审计收集器。**只追加，不改写**——审计记录本身也遵循「只追加」纪律（D-065）。

    ## 为什么自己落盘，而不只发事件（D-258）

    架构 §7.4 要求审计「全部进 journal」。此前只靠事件总线那条路
    （`EventKind.ROLE_TOOL_CALL` / `TOOL_DENIED`），
    实测暴露两个洞：

    1. **留痕会整个消失**：直接调 `工具层.调用()` 时不经过编排，
       事件回调没接或没跑，`audit.jsonl` **一条都不写**。
    2. **失败尝试恰恰最该留，却最容易丢**：越权/未知工具的拒绝走的是
       另一条分支，落盘更没有保证。

    审计是**安全边界**的留痕，不是「给界面看的事件流」。
    事件流允许在没订阅者时丢弃，**审计不允许**——
    所以它自己持有一个可选落盘口，由装配层显式给路径。
    """

    def __init__(
        self,
        *,
        时钟: Callable[[], float] | None = None,
        落盘: Path | None = None,
    ) -> None:
        #: 默认用真实时钟。测试注入假时钟以获得可断言的时间戳——
        #: 真实时间戳无法写确定性断言，「有审计」就会退化成「长度大于零」。
        self._时钟: Callable[[], float] = 时钟 or time.monotonic
        self._条目: list[审计条目] = []
        self._落盘 = 落盘

    def 记(
        self,
        *,
        工具名: str,
        角色: RoleId,
        允许: bool,
        裁决: str,
        参数摘要: str = "",
        结果摘要: str = "",
        错误码: str = "",
    ) -> 审计条目:
        条目 = 审计条目(
            工具名=工具名,
            角色=角色,
            允许=允许,
            裁决=裁决,
            时间戳=self._时钟(),
            参数摘要=_截断(参数摘要),
            结果摘要=_截断(结果摘要),
            错误码=错误码,
        )
        self._条目.append(条目)
        if self._落盘 is not None:
            _写一行(self._落盘, 条目)
        return 条目

    @property
    def 条目(self) -> tuple[审计条目, ...]:
        """全部审计记录。只读快照——返回 tuple 防调用方改坏。"""
        return tuple(self._条目)

    def 该角色的条目(self, role: RoleId) -> tuple[审计条目, ...]:
        return tuple(e for e in self._条目 if e.角色 == role)

    @property
    def 拒绝数(self) -> int:
        return sum(1 for e in self._条目 if e.是拒绝)


def _写一行(路径: Path, 条目: 审计条目) -> None:
    """把一条审计追加进 JSONL。**每条都刷盘**（与 `JsonlJournal.append` 同纪律，D-135）。

    ## 为什么这么啰嗦

    写完不刷，内容只在 page cache 里——**断电即丢**。
    而审计留痕的全部价值就在「出事之后还能查」，
    那正是断电之后。缓冲里的审计等于没有审计。

    ## 为什么是独立文件而不是 `journal.jsonl`

    `journal` 存的是**领域事件**，由事件泵线程单点写入；
    审计走的是**工具层自己的安全留痕**，写入时机与线程都不同。
    混进同一个文件会产生两个写入者，追加写一旦交错就可能串行损坏——
    `JsonlJournal` 自己的注释就写明「单线程调用」。

    审计与事件是**同一件事的两种用途**：事件给界面与续跑看，
    审计给事后追责看。前者可以丢，后者不能。
    """
    路径.parent.mkdir(parents=True, exist_ok=True)
    行 = json.dumps(
        {
            "工具名": 条目.工具名,
            "角色": str(条目.角色),
            "允许": 条目.允许,
            "裁决": 条目.裁决,
            "时间戳": 条目.时间戳,
            "参数摘要": 条目.参数摘要,
            "结果摘要": 条目.结果摘要,
            "错误码": 条目.错误码,
        },
        ensure_ascii=False,
    )
    with 路径.open("a", encoding="utf-8") as fh:
        _ = fh.write(行 + "\n")
        _ = fh.flush()
        os.fsync(fh.fileno())


def _截断(文本: str, *, 上限: int = 200) -> str:
    """审计摘要截断。

    **参数可能被截断，但不得被改写**——审计要保真是第一位的，
    截断时明确标出「还有多少」，
    免得后来人把一条被截断的摘要当成完整摘要读。
    """
    if len(文本) <= 上限:
        return 文本
    return f"{文本[:上限]}…（共 {len(文本)} 字，已截断）"
