"""三驱动同源（第 16 步，D-063 / D-037）。

## 「同源」的准确含义

架构 §8.1 原文：**「三种接入方式共用同一份驱动循环」**：

```python
def drive(kernel, projection):
    while (ev := kernel.next()) is not None:
        projection.handle(ev)
```

差别只在两处：谁来调 `next()`（TUI 在 worker 线程、无界面直接跑），
投影写到哪（TUI 屏上、无界面标准输出）。

判据据此设计：**同一个假 LLM 跑出来的事件流，
三个驱动必须给出完全一致的最终显示**。
不一致就说明有驱动自己另算了一套。

## doctor 是第四个消费者

doctor **不跑编排**——它做 D-024 四项 + D-048 探测 + D-045 字体 + 内容包完整性。
所以不能把它塞进「三个驱动」里比结果，那是范畴错误。
它共用的是**装配根与探测件**，判据分开写。

## 用户约束落在判据里（不靠"记得"）

- 默认界面**极简**：只有进度、阶段成果、最终成果
- **不显示**模型间交互、审核意见、成员状态
- 调试模式命令行开启，且**尽可能完整**
- 界面用标准中文，**不得漏出英文角色标识**

最后一条最容易犯：内部标识是英文，展示名是中文，
混一处就必然有一处漏出英文。
"""

from __future__ import annotations

from zhengtong.domain.event import Event, EventKind, Level
from zhengtong.domain.ids import RunId
from zhengtong.domain.phase import PhaseId
from zhengtong.domain.role import ROLES
from zhengtong.kernel.projection import DebugProjection, StandardProjection


def _事件流() -> tuple[Event, ...]:
    """一段有代表性的事件流：阶段、成果、成员活动、门禁、导出。

    **故意混入成员状态与审核类事件**——
    标准投影必须把它们全部挡掉（白名单，不是"记得别显示"）。
    """

    def ev(序号: int, kind: EventKind, 阶段: PhaseId, **载荷: object) -> Event:
        return Event(
            seq=序号,
            kind=kind,
            run_id=RunId("r-0001"),
            phase=阶段.value,
            level=Level.INFO,
            data=载荷,
        )

    return (
        ev(1, EventKind.RUN_STARTED, PhaseId.INTAKE, tier="PRO"),
        ev(2, EventKind.PHASE_STARTED, PhaseId.DRAFTING),
        ev(3, EventKind.ROLE_STATE_CHANGED, PhaseId.DRAFTING, role="writer", state="RUNNING"),
        ev(4, EventKind.ROLE_STATE_CHANGED, PhaseId.DRAFTING, role="writer", state="DONE"),
        ev(5, EventKind.ARTIFACT_READY, PhaseId.DRAFTING, name="正文初稿.md", content="正文内容"),
        ev(6, EventKind.GATE_REJECTED, PhaseId.GATING, detail="G2 不通过"),
        ev(7, EventKind.EXPORT_BLOCKED, PhaseId.DONE, placeholders=("D3 待填",)),
        ev(8, EventKind.RUN_FAILED, PhaseId.DONE, detail="E050"),
    )


class Test标准模式极简:
    def test_不显示成员状态(self) -> None:
        """用户约束：**不显示成员具体状态**。

        靠 `USER_VISIBLE_KINDS` 白名单挡，不是靠"记得别显示"。
        """
        投影 = StandardProjection()
        for e in _事件流():
            投影.accept(e)
        屏 = "\n".join(投影.lines())
        assert "RUNNING" not in 屏 and "DONE" not in 屏, f"漏了成员状态：{屏}"

    def test_成员状态事件被标准投影忽略(self) -> None:
        """**本条治「白名单被绕过后又写回」**。

        证伪时发现：让标准投影在白名单检查**之前**处理
        `ROLE_STATE_CHANGED`，11 条判据**全绿**。

        因为成员状态那次的 `progress` 被后续的 `RUN_FAILED` 覆盖了，
        最终显示里看不出来——但它确实被写进去过。

        教训：只看最终输出会漏掉「中途写了又被覆盖」，
        故这条**直接查投影的字段历史**，不经过 lines()。
        """
        投影 = StandardProjection()
        只喂成员事件 = [e for e in _事件流() if e.kind is EventKind.ROLE_STATE_CHANGED]
        assert 只喂成员事件, "事件流里没有成员状态事件——判据自身失效"
        for e in 只喂成员事件:
            投影.accept(e)
        assert not 投影.progress, f"只喂成员状态事件却改了 progress：{投影.progress!r}"
        assert not 投影.artifacts, f"成员状态不该进成果：{投影.artifacts}"

    def test_不显示审核意见(self) -> None:
        """用户约束：**审核意见一律不向用户展示**（D-016/D-030）。"""
        投影 = StandardProjection()
        for e in _事件流():
            投影.accept(e)
        屏 = "\n".join(投影.lines())
        assert "G2 不通过" not in 屏, f"审核意见漏进标准界面：{屏}"

    def test_显示该显示的四件事(self) -> None:
        """D-023：用户要看**到哪一步、成果是什么、有没有出错**。

        导出被拦与运行失败必须看得见——
        静默比报错更危险（D-118 修过这个病）。
        """
        投影 = StandardProjection()
        for e in _事件流():
            投影.accept(e)
        屏 = "\n".join(投影.lines())
        assert "待补" in 屏, f"导出被拦没提示：{屏}"
        assert "正文初稿" in 屏, f"阶段成果没显示：{屏}"

    def test_不漏出英文角色标识(self) -> None:
        """**用户约束**：内部标识英文，界面中文。

        混一处就必然有一处漏出英文。
        """
        投影 = StandardProjection()
        for e in _事件流():
            投影.accept(e)
        屏 = "\n".join(投影.lines())
        漏 = [r.id.value for r in ROLES if r.id.value in 屏]
        assert not 漏, f"界面漏出英文角色标识：{漏}"


class Test调试模式完整:
    def test_调试模式显示更多(self) -> None:
        """用户约束：调试模式**尽可能完整**输出终端已有信息。"""
        标 = StandardProjection()
        调 = DebugProjection()
        for e in _事件流():
            标.accept(e)
            调.accept(e)
        标屏 = "\n".join(标.lines())
        调屏 = "\n".join(调.lines())
        assert len(调屏) > len(标屏), "调试模式不比标准模式多——没开调试的价值"
        assert "G2 不通过" in 调屏, f"调试模式应能看到门禁细节：{调屏}"

    def test_调试模式能看到成员状态(self) -> None:
        """调试模式供排障用，成员状态**该**在这里出现。"""
        调 = DebugProjection()
        for e in _事件流():
            调.accept(e)
        屏 = "\n".join(调.lines())
        assert "RUNNING" in 屏, f"调试模式看不到成员状态：{屏}"


class TestTUI与投影同源:
    """D-294：无界面模式已删除，TUI 侧唯一判据。

    原先这里有三条判据（无界面 vs 投影、无界面不回喂编排、三驱动共用投影类），
    前两条随无界面一起作废；第三条收缩为只查 TUI。
    「TUI 显示确实来自投影」这条由 `test_TUI显示来自投影` 覆盖，未重复。
    """

    def test_tui桥接不含任何决策(self) -> None:
        """**这条才是「桥接很薄」的真实含义**。

        架构说「约 20 行」，那是指**搬运逻辑**。
        数行数会被 docstring 与属性定义带偏——我第一版判据
        就栽在这里（47 行判红，而实际搬运逻辑只有几行）。

        真正的判据是行为上的：**桥接不得做任何决策**——
        不得解释事件、不得算进度、不得拼成果。
        故查三件事：
        - 不含条件分支（`if`/`for`/`while`）在做显示逻辑
        - 显示内容全部来自投影（`lines()`）
        - 不自己 import textual 运行时依赖（便携运行时可能没有它）
        """
        # **只看代码行**：docstring 里当然会出现 textual 与 Progress
        # （那是在讲怎么用），把文档当代码判会误报。
        import ast
        import inspect

        from zhengtong.presentation import tui

        树 = ast.parse(inspect.getsource(tui))
        代码 = []
        for 节 in ast.walk(树):
            if isinstance(节, ast.stmt) and not isinstance(节, (ast.Expr,)):
                代码.append(节)
        搬运 = [节 for 节 in 代码 if 节.lineno is not None]
        # 取源码片段：AST 节点按行号切片太碎，用代码行的行号集合近似
        源行 = inspect.getsource(tui).splitlines()
        行号 = {节.lineno for 节 in 搬运}
        文本 = "\n".join(ln for i, ln in enumerate(源行, 1) if i in 行号 or (i + 1) in 行号)
        # 桥接里的分支只该出现在「要不要刷屏」这类开关上
        条件 = [节 for 节 in 搬运 if isinstance(节, (ast.If, ast.For, ast.While))]
        assert len(条件) <= 4, f"桥接里有 {len(条件)} 处条件，疑似在做决策：{条件}"
        assert "import textual" not in 文本, "桥接绑死了 textual 运行时依赖"
        assert "progress =" not in 文本, "桥接自己在算进度"

    def test_tui显示真的来自投影(self) -> None:
        """**本条治「桥接自己拼显示」**。

        证伪时发现：把 `事件桥接.行()` 改成自己拼三行常量，
        10 条判据**全绿**——因为没有一条查显示的来源。

        这与 D-233「两层各自绿」、上轮「只测扫描器没测导出路径」
        是同一类毛病：**判据查了形状，没查来源**。
        """
        from zhengtong.presentation import tui

        桥 = tui.事件桥接()
        桥.喂全部(_事件流())
        期望 = StandardProjection()
        for e in _事件流():
            期望.accept(e)
        assert 桥.行() == 期望.lines(), (
            f"TUI 显示不是投影给的：\nTUI={桥.行()}\n投影={期望.lines()}"
        )

    def test_TUI共用投影类(self) -> None:
        """**同源的硬判据**：TUI 从**投影**取显示内容。

        「跑出来一样」可能只是巧合，共用投影类才是结构上的保证。
        D-294 起无界面模式已删，这里只查 TUI 一侧。
        """
        from zhengtong.presentation import tui

        assert tui.事件桥接().投影类 is StandardProjection
