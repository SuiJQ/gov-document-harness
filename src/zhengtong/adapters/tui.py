"""交互界面：极简三块（诉求 / 单行状态 / PreviewPanel）。

## 界面为什么长这样

用户 2026-10-03 定：默认只显示**总体进度、阶段性成果、最终成果**，
不显示模型间交互、审核意见、成员具体状态。

而实测（见 D-288）：真实一轮只发 **7 种事件**
（RUN/PHASE/ARTIFACT_READY/MODEL_*），
其余 30 种**三档全跑一次都没发过**，其中包括门禁 6 种、角色 6 种、提问 2 种。

> **所以「极简」不只是审美，它是当前后端唯一诚实能做的界面。**
> 想显示的多数东西，压根没有数据。

## 硬约束：视图不推断状态（D-063）

本模块**只做一件事**：把 `StandardProjection.lines()` 给出的行**渲染出来**。

- 不自己判断「到哪一步了」
- 不自己数产物、不自己猜进度
- 不读事件流来推状态

理由：无界面模式用的是**同一个投影**。
一旦视图自己推断，两边就会给出不同结论——
而「三个驱动同源」是这个项目早就确立的纪律（D-063）。

判据 T2 守着这条：同事件喂两个驱动，显示必须逐行相同。

## 关于「正在等模型」

流式能力按用户要求**只检测不使用**（D-024），
所以每次模型调用结束前屏幕必然静止。

**界面必须如实显示「正在等模型」，不能装作很快。**
让用户在干等时看到进度条乱动，比诚实的静止更糟。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Static, TextArea

from . import tui_theme as 主题

#: 预览区最多渲染多少字。**超出只显示截断提示，不静默截断**
#: （D-007 的同类纪律：截断了就要说）。
预览上限字 = 20000


class RequestBox(Vertical):
    """诉求输入。**含档位选择**——用户开始前选定，程序不自动判断。"""

    DEFAULT_CSS = """
    RequestBox { height: auto; padding: 0 1; }
    RequestBox Static { height: auto; }
    RequestBox TextArea { height: 5; }
    """

    def compose(self) -> ComposeResult:
        yield Static(f"{主题.符_标题} {主题.文案_诉求}", classes=主题.色_标题)
        yield TextArea(id="need")
        yield Static(f"{主题.文案_档位}：咨询 / flash / pro　（默认咨询）")


class StatusLine(Static):
    """**单行**总体状态。刻意只有一行——用户要的是「总体进度」。"""

    DEFAULT_CSS = """
    StatusLine { height: 1; padding: 0 1; background: $panel; }
    """

    DEFAULT = "尚未开始"


class ArtifactPanel(VerticalScroll):
    """阶段性成果 + 最终成果。**只渲染投影给的行。**"""

    DEFAULT_CSS = """
    ArtifactPanel { height: 1fr; border: round $primary; padding: 0 1; }
    """

    def 显示(self, 行: tuple[str, ...]) -> None:
        self.remove_children()
        if not 行:
            self.mount(Static("（尚无成果）", classes=主题.色_静默))
            return
        for 一行 in 行:
            self.mount(Static(一行, classes=主题.色_已完成, markup=False))


class PreviewPanel(VerticalScroll):
    """最终成果预览。**带上限，且截断时明说。**"""

    DEFAULT_CSS = """
    PreviewPanel { height: 2fr; border: round $success; padding: 0 1; }
    """

    def 显示(self, 正文: str) -> None:
        self.remove_children()
        if not 正文.strip():
            self.mount(Static("（暂无成稿）", classes=主题.色_静默))
            return
        if len(正文) > 预览上限字:
            self.mount(
                Static(
                    f"⚠ 正文 {len(正文)} 字，预览只显示前 {预览上限字} 字。"
                    "完整内容见 data/<run-id>/ 下的成品文件。",
                    classes=主题.色_需处理,
                )
            )
            正文 = 正文[:预览上限字]
        self.mount(Static(正文, markup=False))


class ZhengTongApp(App[None]):
    """政通交互界面。

    B1 只做「能启动 + 极简布局 + 接事件流」。
    硬停车（Ctrl 提交诉求后模型提问时的表单）、插话、逐键位、
    字形检测、大稿虚拟滚动都在 B2 及以后——**那些不是漏做**。
    """

    TITLE = 主题.文案_标题
    SUB_TITLE = "启动即用 · 单任务"

    CSS = """
    Screen { layout: vertical; }
    #request-area { height: auto; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "退出"),
        Binding("ctrl+enter", "开始", "开始"),
    ]

    def __init__(self, *, 桥, 根: Path | None = None, **kw) -> None:
        """`桥`（`presentation.tui.事件桥接`）**由装配根注入，不在这里 import**。

        ## 为什么绕这一下

        架构有两处规定在本处相撞：

        - §6：第三方库**只允许出现在适配层** → textual 的界面代码归 L2
        - DECISIONS「TUI 桥接不 import textual」→ 桥必须保持纯净

        若适配器直接 `from ..presentation.tui import 事件桥接`，
        就是 **adapters → presentation**，那是**向上依赖**，
        分层判据立刻拦下（本次实测拦下 3 条）。

        而反过来让桥去 import textual，会毁掉便携运行时
        （没装 textual 的机器上连无界面模式都起不来）。

        故只剩注入这一条：**适配器只认「有个桥」这个形状**，
        是谁造的不关心——这本来就是依赖倒置（D-015）。
        """
        super().__init__(**kw)
        self._根 = 根
        self._桥 = 桥

    def compose(self) -> ComposeResult:
        yield Static(主题.文案_标题, id="app-title")
        with Vertical(id="request-area"):
            yield RequestBox()
            yield Static("[ 开始 ]　Ctrl+Enter　　Ctrl+Q 退出", id="start-button")
        yield StatusLine(id="status")
        yield ArtifactPanel(id="artifacts")
        yield PreviewPanel(id="preview")
        yield StatusLine(id="workspace")
        yield Footer()

    def on_mount(self) -> None:
        """起手两件事：状态行给默认值，页脚报清楚**在哪个工作区**。

        ## 为什么页脚要显示工作区

        不是为了让 `_根` 这个字段「有消费」而硬塞的。
        用户按 `--tui` 起来之后**首先需要知道一件事**：
        程序正在动他的哪个工作区。写错了地方，
        后面生成的文件落到别处去，还很难察觉。

        页脚常驻可见，比塞进标题栏更不打扰（D-281 追加三：别拿判据当挡箭牌）。

        ## 关于 `_编排器`

        B1 **不存**它。B2 接真实运行时才建，现在放个 `self._编排器 = None`
        就是「存了从不读」——`check_wiring` 会报，而且报得对。
        判据不是障碍，是提醒这里还欠着东西。
        """
        self.query_one("#status", StatusLine).update(StatusLine.DEFAULT)
        工作区 = self._根 if self._根 is not None else Path.cwd()
        self.query_one("#workspace", StatusLine).update(f"工作区：{工作区}")

    def action_开始(self) -> None:  # Textual 的 action 命名
        """B1 只给一个诚实的占位动作。

        **不假装能跑**——真实编排要等 B2 之后接上问答闸门与硬停车。
        这里明说「尚未接上」，而不是启动一个注定卡在提问上的流程。
        """
        self.query_one("#status", StatusLine).update(
            f"{主题.符_进行中} 真实运行尚未接上——硬停车与提问表单属 B2 之后"
        )

    # ── 供判据与后续阶段使用 ──
    def feed(self, 事件们) -> None:
        """把事件喂进桥接层并刷新。**B2 之后由 worker 线程调用。**"""
        self._桥.喂全部(事件们)
        行 = self._桥.行()
        self.query_one(ArtifactPanel).显示(行)
        self.query_one("#status", StatusLine).update(行[0] if 行 else StatusLine.DEFAULT)

    def projection_lines(self) -> tuple[str, ...]:
        return self._桥.行()


def 启动(根=None) -> int:
    """TUI 入口。被 `cli/main.py --tui` 按需导入。

    返回退出码，`sys.exit` 留给最外层（与 CLI 一致）。
    """
    界面 = ZhengTongApp(根=根)
    界面.run()
    return 0
