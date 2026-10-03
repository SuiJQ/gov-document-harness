"""导出层：导出前的机械拦截 + 多份产出落盘（D-034 / D-038 / D-044）。

## 这个模块为什么存在

架构 §9.1 原文：「这是『把提示词约束变成机械检查』的落地。」

「严禁编造」写在提示词里是**不可靠**的——模型会编。
所以导出前必须有一道机械检查：稿子里还留着占位符，就**不许导出**。

原文还写着拦截信息要同时走 `export.blocked` 事件（`audience=USER`），
所以标准界面也能提示。那两个事件（`EXPORT_BLOCKED`/`EXPORT_DONE`）
在事件层与投影层都已就位，**此前没有任何生产代码发过它们**——
投影层里的两个分支因此永不可达，D-118 修过一次同样的病。

## 扫描什么格式的占位符

按**实现真格式**扫：`【待填 · D3】`、`【待核实 · D7】`
（`_渲染事实块` 实际产出的形式）。

架构 §9.1 表格里写的是 `〔待填：…〕`，与实现对不上。
扫一个代码从不产出的格式，等于扫了个寂寞——
本项目已因「实现与文档分叉」吃过亏（D-148 阶段编号三方三套）。

## 不扫什么

架构 §9.1 还有两行：

- 「必填字段 `provenance != USER` 且无值（D4/D5/D7）」→ 禁止导出
- 「D-Card 降级字段 `degraded=True`」→ 允许导出但首行提示

这两行**目前没有载体**：`DCardField` 没有 `degraded` 属性，
必填校验走的是另一条路径（`validate_dcard`）。
硬造字段会让领域层背上它并不需要的概念。
故本步只做**有载体的那条**，另两条记为遗留。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.artifact import ArtifactKind
from ..domain.errors import ErrorCode, Result

#: 占位符：`【待填 · D3】` / `【待核实 · D7】`。
#:
#: **按实现真格式匹配**，不是架构表格里的 `〔待填：…〕`——
#: 后者代码从不产出，扫它等于给判据一个假绿。
#: 允许中间有空格差异：`【待填·D3】` 也认。
占位符格式 = re.compile(r"【\s*(待填|待核实)\s*[·:：]\s*(D\d+)\s*】")


@dataclass(frozen=True, slots=True)
class 扫描结论:
    """一次扫描的结果。**可导出与待补清单必须一起看**——
    只看 `可导出` 不足以回答「还差什么」。"""

    可导出: bool
    #: 待补的字段号，形如 `D3 待填`。**去重且保持出现顺序**——
    #: 用户要照着清单一条条补，顺序乱了会漏。
    待补: tuple[str, ...] = ()
    原文出处: tuple[str, ...] = field(default=())


def 扫描产出(段们: Sequence[tuple[str, str]]) -> 扫描结论:
    """扫一遍产出文本里的残留占位符（D-034）。

    ## 入参是 `(文件名, 内容)` 对，不是裸文本（D-263）

    此前签名是 `Sequence[str]`，而**文件名在调用处就被丢掉了**——
    `导出交付物` 手里明明有 `导出文件.文件名`，却只传了 `内容`。
    于是扫描结论只能报「D3 待填」，报不出「哪个文件的哪一处」，
    用户拿到清单仍要一份份翻、一段段找。

    **收「对」而不是收两个平行序列**：两个平行列表一旦长度对不上，
    报错的位置就会指到别的文件去——**那比不报更坏**。

    ## `待补` 与 `原文出处` 是两件事，分开装

    | | 回答 | 去重键 |
    |---|---|---|
    | `待补` | **要补哪几个字段** | 字段号（同一字段补一次） |
    | `原文出处` | **去哪个文件的哪一行找** | 文件 + 行号（同一处只列一次） |

    合成一份会让清单翻倍——用户明确要求界面「不要占太大空间」
    （D-038）。分开装则两边各自紧凑。

    ## 行号怎么算

    占位符**可能跨行**（用户手改过稿子），故按 `finditer` 的
    `start()` 偏移数前面的换行数，而不是按行遍历——
    后者遇到超长单行会退化成 O(n²)。
    """
    见过: list[str] = []
    出处: list[str] = []
    for 名称, 文本 in 段们:
        for 匹配 in 占位符格式.finditer(文本):
            键, 号 = 匹配.group(1), 匹配.group(2)
            条 = f"{号} {键}"
            if 条 not in 见过:
                见过.append(条)
            位置 = f"{名称} 第{_行号(文本, 匹配.start())}行"
            if 位置 not in 出处:
                出处.append(位置)
    return 扫描结论(可导出=not 见过, 待补=tuple(见过), 原文出处=tuple(出处))


def _行号(文本: str, 偏移: int) -> int:
    """偏移处是第几行（**从 1 起**）。

    从 1 起是因为给人看的行号都从 1 起，
    显示「第 0 行」只会让人以为自己算错了。
    """
    return 文本.count("\n", 0, 偏移) + 1


@dataclass(frozen=True, slots=True)
class 导出文件:
    """一份待落盘的交付物。"""

    文件名: str
    内容: str
    种类: ArtifactKind


class 导出交付物:
    """导出执行器。**命名空间式**：拦截事件与完成事件都在这里取，
    因为它们与放行判定是同一件事的两面——
    拆到别处就会出现「判了不报」的老毛病。"""

    def __init__(self, 根: Path, *, 运行号: str = "r-0001") -> None:
        #: `data/` 本身。**运行目录这层由本类自己拼**——
        #: 交给调用方拼就等于把「两次运行不覆盖」这条约束
        #: 散到每个调用点，漏一处就覆盖一次
        #: （与「AI 只能写 data/」「路径必须过守卫」同理，
        #: 约束该落在产生它的那一层）。
        self.根 = 根
        self.运行号 = 运行号

    @property
    def 输出目录(self) -> Path:
        """`data/<run-id>/output/`（架构 §9.3）。

        **必须带 run 目录**：两次运行若共用 `output/` 会互相覆盖。
        """
        return self.根 / self.运行号 / "output"

    def __call__(self, *, 文件: Sequence[导出文件]) -> Result[tuple[str, ...]]:
        """扫 → 判 → 落盘。**任一步不过就不写任何文件。**

        ## 顺序是硬要求

        先扫全部文本再落盘。若边扫边写，扫到第二段才发现问题，
        第一段已经落盘——用户目录里留下半套交付物，
        比什么都不写更难收拾（D-034 的拦截就形同虚设）。
        """
        可导 = [f for f in 文件 if f.种类.is_deliverable]
        if not 可导:
            return Result.fail(
                ErrorCode.UNSOURCED_ASSERTION,
                "没有可导出的交付物（初稿与门禁报告不是交付物）",
            )
        主件 = [f for f in 可导 if f.种类 is ArtifactKind.MAIN_DOCUMENT]
        if len(主件) > 1:
            return Result.fail(
                ErrorCode.UNSOURCED_ASSERTION,
                f"主件必须唯一，收到 {len(主件)} 份：{[f.文件名 for f in 主件]}",
            )
        结论 = 扫描产出(tuple((f.文件名, f.内容) for f in 可导))
        if not 结论.可导出:
            return Result.fail(
                ErrorCode.PLACEHOLDER_REMAIN,
                f"残留占位符，禁止导出：{'、'.join(结论.待补)}",
            )
        try:
            self.输出目录.mkdir(parents=True, exist_ok=True)
            for f in 可导:
                (self.输出目录 / f.文件名).write_text(f.内容, encoding="utf-8")
        except OSError as exc:
            return Result.fail(ErrorCode.STORAGE_WRITE_FAILED, f"导出失败：{exc}")
        return Result.ok(tuple(f.文件名 for f in 可导))

    @staticmethod
    def 取拦截事件(结论: 扫描结论) -> dict[str, object] | None:
        """`export.blocked` 的载荷（投影层读 `placeholders` 与 `原文出处`）。

        投影层已就位但此前永不可达——D-118 修过同样的病
        （白名单里有、却没有分支）。这里补上发的一端。

        **`原文出处` 一并带上**（D-263）：只有「D3 待填」的话，
        用户不知道该翻哪份文件；这一项此前**连构造时都是空的**，
        字段声明了却从没填过值。
        """
        if 结论.可导出:
            return None
        return {"placeholders": 结论.待补, "原文出处": 结论.原文出处}

    @staticmethod
    def 取完成事件(结论: 扫描结论, 文件名们: Sequence[str]) -> dict[str, object] | None:
        """`export.done` 的载荷（投影层读 `files`）。"""
        if not 结论.可导出:
            return None
        return {"files": tuple(文件名们)}
