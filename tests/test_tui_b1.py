"""TUI 骨架真的能启动（T1 / T2 / T6，B1）。

## 本文件守的是三件「不测就不知道」的事

### T1 · 能启动

D-281 的教训：`pyproject.toml` 声明了入口而模块从不存在，
**装出来的命令一启动就崩**，而全仓没有一条判据问过「能不能启动」。

本文件**起真 App**（`App.run_test()`），不只测「模块能导入」——
导入通过和界面能跑是两件事。

### T2 · 与无界面跑出同一份显示（D-063）

> 「三个驱动同源」是本项目早就确立的纪律。

**最容易破这条纪律的方式，就是让视图自己推断状态**——
自己数产物、自己猜进度、自己判断到哪一步。一旦那样做，
界面和无界面模式就会给出不同结论。

故断的是：**同一串事件喂给两个驱动，显示必须逐行相同**，
且**投影给的行真的渲染出来了**。

### T6 · 默认界面不含审核意见 / 成员状态

用户 2026-10-03 定：
> 默认启动界面只显示总体进度、阶段性成果和最终成果，
> **不显示模型间交互、审核意见或成员具体状态**。

「模型间交互」这条目前是**结构性成立**的——
`ROLE_TURN` 等角色事件**从不发射**（30 种死事件之一），界面想显示也没有数据。

> **但这不等于安全**：哪天补了角色事件，这条约束会无声失效。
> 故必须有判据盯着，而不是指望「反正没数据」。

## Textual 的三个硬约束（实测踩出来的）

1. **控件类名不能是中文**——`BadWidgetName: should be capitalized`
2. **CSS 选择器不能是中文**——`TokenError: Expected selector or end of file`
3. **`id` 不能是中文**——`BadIdentifier: identifiers must contain only letters…`

**结论：标识符一律英文，中文只留在显示文本里。**
用户看到的仍是全中文界面——那才是他们要的东西。

## 为什么不用 async 测试插件

**环境里没有 `pytest-asyncio`，也不打算为了几条判据新增一个依赖**
（D-003：依赖越少越好）。故一律用 `asyncio.run` 包同步壳。

> ⚠️ 本文件第一版用正则批量改写 async→同步，**结果 7 个测试的同步壳被弄丢，
> pytest 只收集到 3 条**——而那 7 条**从来没跑过**。
>
> 发现它的不是肉眼，是**三次证伪全部无效**：
> 判据抓不住东西，往往是因为判据压根没在跑。
> 故本文件用显式同步壳，不用正则生成。
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest
from textual.widgets import Static, TextArea

from zhengtong.adapters.tui import ArtifactPanel, StatusLine
from zhengtong.domain.event import Event
from zhengtong.domain.tier import TierId
from zhengtong.kernel.projection import StandardProjection
from zhengtong.presentation.tui import 事件桥接
from zhengtong.tui_app import ZhengTongApp

仓库根 = Path(__file__).resolve().parents[1]


def 跑一轮真实编排() -> list[Event]:
    """跑一次真实编排，把事件捞出来。

    **不自己造事件**——造出来的可能缺真实流程才有的那些，那是自欺欺人。
    """
    sys.path.insert(0, str(仓库根 / "tests"))
    import test_step14_orchestrate_gate as M

    编 = M._编排()
    编.跑到终(档位=TierId.PRO, 需求="写一份关于防汛工作的通知")
    return list(编.事件)


def _渲染文本(应用, 选择器: type) -> str:
    """读**控件里真正渲染出来的文字**。

    ## 为什么不能读 `projection_lines()`

    第一版 T6 断的是 `应用.projection_lines()`，
    那读的是**桥接层自己的投影输出**，
    而界面显示的文本是**渲染进控件之后**的东西。

    于是证伪时注入的「审核意见」进了控件、判据却读投影输出——
    **判据查错了对象，全程无效而不自知。**

    这条辅助存在本身就是提醒：断界面要断**界面**。

    ## 为什么 `feed` 之后必须 `await 试点.pause()`

    `ArtifactPanel.显示` 里的 `remove_children()` 和 `mount()`
    **都是异步的**——不泵一次事件循环，控件子节点挂没挂上是看运气的。

    实测表现为**竞态**：同一个文件里
    「逐行相同且真的渲染了」通过、T6 却读到空内容，
    因为两条断言落在挂载完成的前后。

    这不是测试不稳定，是**被测代码本身不确定**。
    修法是在喂完事件后显式 `pause()`，让状态确定下来。
    """
    面板 = 应用.query_one(选择器)
    return "\n".join(str(w.content) for w in 面板.children if isinstance(w, Static))


def _全部文档字符串(树: ast.AST) -> set[int]:
    """收集所有 docstring 常量节点的 `id`。

    ## 为什么用 `id()` 而不是值

    值相同的 docstring 会互相误伤——而 `id()` 精确指向节点本身。
    """
    出: set[int] = set()
    for 节点 in ast.walk(树):
        if not isinstance(节点, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        体 = getattr(节点, "body", [])
        if not 体:
            continue
        首 = 体[0]
        if (
            isinstance(首, ast.Expr)
            and isinstance(首.value, ast.Constant)
            and isinstance(首.value.value, str)
        ):
            出.add(id(首.value))
    return 出


pytest.importorskip("textual", reason="TUI 需要 textual")


class TestT1能启动:
    def test_App真能跑起来(self) -> None:
        async def 跑() -> None:
            async with ZhengTongApp(桥=事件桥接()).run_test() as 试点:
                assert 试点.app is not None

        asyncio.run(跑())

    def test_六个控件都挂上了(self) -> None:
        """极简三块 + 标题 + 状态 + 成果 + 预览。

        断的是「都挂上了」而不是「长什么样」——布局细节 B 阶段再调，缺件是真缺。
        """

        async def 跑() -> None:
            应用 = ZhengTongApp(桥=事件桥接())
            async with 应用.run_test():
                实际 = {w.id for w in 应用.screen.children if w.id}
                assert {
                    "app-title",
                    "request-area",
                    "status",
                    "artifacts",
                    "preview",
                    "workspace",
                } <= 实际, 实际

        asyncio.run(跑())

    def test_ctrl_enter有反应(self) -> None:
        """键位不是「写在 BINDINGS 里就算数」——**按下去要有反应**。"""

        async def 跑() -> None:
            应用 = ZhengTongApp(桥=事件桥接())
            async with 应用.run_test() as 试点:
                前 = str(应用.query_one("#status", StatusLine).content)
                await 试点.press("ctrl+enter")
                await 试点.pause()
                后 = str(应用.query_one("#status", StatusLine).content)
                assert 前 != 后, f"按 Ctrl+Enter 状态行没变：{前}"

        asyncio.run(跑())

    def test_没编排器就明说跑不了(self) -> None:
        """没给编排器工厂时，界面**必须明说跑不了**，不能装作在跑。

        ## B1 与 B2 的这条判据不是同一条

        B1 时它断的是「界面说『尚未接上』」；
        B2 接上真实运行后那句话就该消失了——
        留着它，等于要求界面**永远说自己没做完**。

        换成 B2 真正要断的东西：**能力缺失时如实报**。
        用户看到「没有可用的编排器」，知道该去查配置；
        看到空白或转圈，以为程序卡了（D-027：别让用户白折腾）。
        """

        async def 跑() -> None:
            应用 = ZhengTongApp(桥=事件桥接())
            async with 应用.run_test() as 试点:
                # **先填诉求**——否则空诉求分支先拦下，测不到「没有编排器」那层
                应用.query_one("#need", TextArea).text = "写一份防汛通知"
                await 试点.pause()
                await 试点.press("ctrl+enter")
                await 试点.pause()
                文案 = str(应用.query_one("#status", StatusLine).content)
                assert "未接上" in 文案 or "没有可用" in 文案, f"没说清为什么跑不了：{文案}"
                assert "运行中" not in 文案, f"没编排器却装作在跑：{文案}"

        asyncio.run(跑())

    def test_空诉求不启动(self) -> None:
        """空诉求**当场拒绝**。

        不拒绝的话，编排会跑起来、卡在提问上，等一个没有上下文的答复——
        用户白等十几分钟才知道没填。
        """

        async def 跑() -> None:
            被调 = []
            应用 = ZhengTongApp(桥=事件桥接(), 取编排器=lambda: 被调.append(1))
            async with 应用.run_test() as 试点:
                await 试点.press("ctrl+enter")
                await 试点.pause()
                文案 = str(应用.query_one("#status", StatusLine).content)
                assert "诉求" in 文案, f"没提示要填诉求：{文案}"
                assert not 被调, "空诉求却取了编排器——流程已经起了"

        asyncio.run(跑())

    def test_页脚报出工作区(self, tmp_path: Path) -> None:
        """用户按 ---tui 起来后**首先要知道程序在动哪个工作区**。

        写错了地方，后面生成的文件落到别处去，还很难察觉。
        这条也是「`_根` 不是死字段」的证明（D-289）。
        """

        async def 跑() -> None:
            # 根用 `tmp_path` 而非写死 `/tmp/…`：
            # 后者在 Windows 上会静默变成 `\tmp\…`——不报错，
            # 但下面那句 `in 文案` 的断言就换了含义。
            工作区 = tmp_path / "测试工作区"
            应用 = ZhengTongApp(桥=事件桥接(), 根=工作区)
            async with 应用.run_test():
                文案 = str(应用.query_one("#workspace", StatusLine).content)
                assert "工作区" in 文案, f"页脚没报工作区：{文案}"
                assert str(工作区) in 文案, f"页脚工作区不是传进来的那个：{文案}"

        asyncio.run(跑())

    def test_没传根时用当前目录(self) -> None:
        """**不许空白页脚**——用户必须始终知道工作区在哪。"""

        async def 跑() -> None:
            应用 = ZhengTongApp(桥=事件桥接())
            async with 应用.run_test():
                文案 = str(应用.query_one("#workspace", StatusLine).content)
                assert "工作区：" in 文案, f"页脚空白：{文案}"
                assert str(Path.cwd()) in 文案, f"没传根时应显示当前目录：{文案}"

        asyncio.run(跑())

    def test_标识符全是英文(self) -> None:
        """**Textual 不认中文标识符**（实测三种错法）。

        这条防回退：将来谁加个中文 id，在这里是红的，
        而不是等到运行时才发现启动崩溃。
        """

        async def 跑() -> None:
            应用 = ZhengTongApp(桥=事件桥接())
            async with 应用.run_test():
                for w in 应用.screen.children:
                    if w.id:
                        assert w.id.isascii(), f"中文 id 会让程序启动即崩：{w.id}"
                    assert type(w).__name__.isascii(), (
                        f"中文类名会被 Textual 拒收：{type(w).__name__}"
                    )

        asyncio.run(跑())


class TestT2与无界面同源:
    def test_显示与无界面逐行相同且真的渲染了(self) -> None:
        async def 跑() -> None:
            事件们 = 跑一轮真实编排()
            投影 = StandardProjection()
            for e in 事件们:
                投影.accept(e)
            assert 投影.lines(), "前置条件：投影确实产出了行"

            应用 = ZhengTongApp(桥=事件桥接())
            async with 应用.run_test() as 试点:
                应用.feed(事件们)
                await 试点.pause()
                assert 应用.projection_lines() == 投影.lines(), (
                    "TUI 与无界面的显示不一致——说明视图自己推断状态了（D-063 不允许）"
                )
                面板 = 应用.query_one(ArtifactPanel)
                渲染文本 = "\n".join(str(w.content) for w in 面板.children if isinstance(w, Static))
                assert 投影.lines()[0] in 渲染文本, (
                    f"投影首行没渲染出来：{投影.lines()[0]!r} vs {渲染文本!r}"
                )

        asyncio.run(跑())

    def test_适配器不import呈现层(self) -> None:
        """适配器只认「有个桥」这个形状，**不许自己去拿**。

        `adapters → presentation` 是向上依赖，分层判据本就拦它；
        但那条判据只查 `KNOWN_LAYERS` 里的层名，
        而「适配器不该知道谁造了桥」这件事值得单独钉住——
        它是 D-015 依赖倒置在本处的落法（D-290）。
        """
        import ast
        from pathlib import Path

        源 = Path(__file__).resolve().parents[1] / "src" / "zhengtong" / "adapters" / "tui.py"
        树 = ast.parse(源.read_text(encoding="utf-8"))
        导入 = [
            节点.module
            for 节点 in ast.walk(树)
            if isinstance(节点, ast.ImportFrom) and 节点.module and "presentation" in 节点.module
        ]
        assert not 导入, f"适配器不该 import 呈现层，桥由装配根注入：{导入}"

    def test_视图不自己数事件(self) -> None:
        """**架构判据**：视图不许统计事件或推断进度，只渲染投影给的行。

        ## 判据要断 AST，不是断文本

        第一版直接 `assert "ARTIFACT_READY" not in 源文本`，
        结果命中的是**我自己写的注释**——
        那条判据实际上在保护「注释不许写」。

        故改为解析 AST，**只看代码**：
        不许出现具体事件名的常量、不许访问事件类型属性。
        """
        import ast

        源 = 仓库根 / "src" / "zhengtong" / "adapters" / "tui.py"
        树 = ast.parse(源.read_text(encoding="utf-8"))
        文档 = _全部文档字符串(树)
        违规: list[str] = []
        for 节点 in ast.walk(树):
            if isinstance(节点, ast.Attribute) and 节点.attr in ("ARTIFACT_READY", "EventKind"):
                违规.append(f"第 {节点.lineno} 行访问了 {节点.attr}")
            if isinstance(节点, ast.Constant) and isinstance(节点.value, str):
                # **docstring 不算**：本判据第一版只查字符串字面量，
                # 于是模块文档里那句「真实一轮只发 7 种事件（…/ARTIFACT_READY/…）」
                # 直接把它判红——那条判据实际在**保护注释不许写**（D-288）。
                # 第二次又栽在文档里提到 `gate.py` 上。
                if id(节点) in 文档:
                    continue
                文 = 节点.value
                if any(k in 文 for k in ("artifact_ready", "role.turn", "gate.")):
                    违规.append(f"第 {节点.lineno} 行硬编码了事件名 {文!r}")
        assert not 违规, f"视图不该认识具体事件——只该消费投影结果：{违规}"


class TestT6默认不显示不该显示的:
    def test_成果区不含审核意见字样(self) -> None:
        async def 跑() -> None:
            应用 = ZhengTongApp(桥=事件桥接())
            async with 应用.run_test() as 试点:
                应用.feed(跑一轮真实编排())
                await 试点.pause()
                文本 = _渲染文本(应用, ArtifactPanel)
                assert 文本, "前置条件：成果区确实渲染出了东西"
                assert "审核意见" not in 文本, f"默认界面出现「审核意见」：{文本}"

        asyncio.run(跑())

    def test_默认不含成员状态描述(self) -> None:
        """成员状态**结构上显示不了**（角色事件从不发射），
        这条防的是将来补了事件后无声失效。"""

        async def 跑() -> None:
            应用 = ZhengTongApp(桥=事件桥接())
            async with 应用.run_test() as 试点:
                应用.feed(跑一轮真实编排())
                await 试点.pause()
                文本 = _渲染文本(应用, ArtifactPanel)
                for 禁用 in ("陈纲举", "文择体", "辞达意", "正在处理", "成员状态"):
                    assert 禁用 not in 文本, f"默认界面出现成员状态类内容「{禁用}」"

        asyncio.run(跑())

    def test_标准投影本身就不含审核意见(self) -> None:
        """**结构性保障**：不是界面藏了，是投影根本不产出行。"""
        投影 = StandardProjection()
        for e in 跑一轮真实编排():
            投影.accept(e)
        文本 = "\n".join(投影.lines())
        assert "审核意见" not in 文本, f"标准投影不该含审核意见：{文本}"
