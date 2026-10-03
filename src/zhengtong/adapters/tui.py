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

from concurrent.futures import Future
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, RadioButton, RadioSet, Static, TextArea

from ..domain.tier import TIERS, TierId
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
    RequestBox RadioSet { height: auto; }
    """

    def compose(self) -> ComposeResult:
        yield Static(f"{主题.符_标题} {主题.文案_诉求}", classes=主题.色_标题)
        yield TextArea(id="need")
        yield Static(f"{主题.文案_档位}（开始前选定，程序不自动判断）", classes=主题.色_标题)
        # **档位表从 `TIERS` 生成，不在这里另写一份**（D-067：三档是数据）。
        # 另写一份就会出现「代码说三档、领域说三档、内容包说两档」——
        # 而三者不一致时**任何一侧的单侧检查都查不出来**。
        yield RadioSet(
            *(RadioButton(档.display_name) for 档 in TIERS),
            id="tier",
        )

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        #: 选中的档位下标。**默认第一档（咨询）**，与 CLI 的默认档位一致。
        self._档位下标 = 0

    def on_mount(self) -> None:
        """预选第一档。

        ## 这一段踩了三个坑，全是「文档不写、只能起一次才知道」

        1. `RadioSet.__init__` **没有 `value` 参数**——给了就 `TypeError`。
        2. `RadioSet(*buttons)` 收 `str | RadioButton`，**不是 `(标签, 值)` 元组**；
           传元组会在挂载时抛「expects a str, a Text instance…」。
        3. **`RadioSet` 是布尔语义**：按钮的 `value` 是 `bool`（同一时刻只有一个 True），
           选中态记在 `_selected: int` 上，**根本没有字符串值的 `value`**。

        第 3 条最要紧：我第一版给 `RadioButton` 传了字符串当 value，
        运行时不报错（存根与运行时不一致），但**类型检查直接报 4 处**——
        而本项目禁 `# type: ignore`（D-018），也没有理由为一个第三方控件的
        存根缺陷去破例。

        故按 `RadioSet` 的本意用：**只认下标**，下标经 `Changed.index` 拿到，
        再查 `TIERS` 取档位。全程只碰 `int`，类型干净。
        """
        self._预选(0)

    def _预选(self, 下标: int) -> None:
        self._档位下标 = 下标
        self.query_one("#tier", RadioSet)._selected = 下标  # Textual 私有字段，只此一处
        for 位, 钮 in enumerate(self.query(RadioButton)):
            钮.value = 位 == 下标

    def on_radio_set_changed(self, 事件: RadioSet.Changed) -> None:
        self._档位下标 = 事件.index

    def 当前档位(self) -> TierId:
        """读用户选的档位。**读不到就明说，不静默回落**（D-082）。

        回落看着像体贴，实际是最糟的一种：用户选了 Pro 却跑成咨询，
        成文质量直接降一档，而界面不会有任何提示。
        """
        下标 = self._档位下标
        if not 0 <= 下标 < len(TIERS):
            raise ValueError(f"档位下标越界：{下标}")
        return TIERS[下标].id

    def 诉求(self) -> str:
        return self.query_one("#need", TextArea).text.strip()


class _提问Modal(ModalScreen[str]):
    """硬停车的表单：模型一次问完九项，用户一次答完。

    ## 为什么是模态

    提问期间编排 worker **正阻塞着**（闸.问 里 `Event.wait`），
    界面此刻**没有任何别的事可做**。
    模态把这件事说清楚了：挡住输入就是语义，不是限制。

    ## 九项一次发完

    用户定：一次任务一个输入框，不分批挤牙膏；
    模型侧也是「九项一次性发完」（团队主理人提示词）。
    故这里**不做分页**，只有「提交」与「跳过」两个出口。
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "跳过", "跳过", show=True),
    ]

    DEFAULT_CSS = """
    #ask-box { align: center middle; }
    #ask-box Vertical {
        width: 80%; max-width: 100; height: 80%;
        border: thick $warning; background: $surface; padding: 1 2;
    }
    #ask-box Label { height: auto; }
    #ask-box TextArea { height: 1fr; }
    """

    def __init__(self, 字段: tuple[dict[str, object], ...]) -> None:
        super().__init__()
        self._字段 = 字段

    def compose(self) -> ComposeResult:
        with Vertical(id="ask-box"):
            yield Static(f"{主题.符_进行中} 需要你确认以下信息", classes=主题.色_需处理)
            yield Static(self._问题全文(), markup=False)
            yield TextArea(id="ask-answer")
            yield Static(
                "Ctrl+Enter 提交　Esc 跳过（未答项一律按占位符处理，绝不编造）",
                classes=主题.色_静默,
            )

    def _问题全文(self) -> str:
        """把九项拼成一段可读文本。**逐项都列，缺项不跳过**。

        空问题也要列出来并标明——那说明模型传了残缺字段，
        藏起来等于让用户以为自己看全了（D-007：残缺材料要标明）。
        """
        if not self._字段:
            return "（模型没有给出任何问题）"
        行 = []
        for 项 in self._字段:
            键 = str(项.get("key", "?")).strip() or "?"
            题 = str(项.get("question", "")).strip()
            行.append(f"{键}. {题}" if 题 else f"{键}. （模型未给出问题原文）")
        return "\n".join(行)

    def action_跳过(self) -> None:
        """跳过 = 空串。

        **空串不会被当成「用户没意见」**——闸.问 对空答复走超时失败
        （gate.py：拒绝「无人应答就返回空串」，那是最危险的一种失败）。
        而未答项会按 D-007 变成占位符。
        """
        self.dismiss("")

    def on_key(self, 事件) -> None:
        if 事件.key == "ctrl+enter":
            事件.stop()
            self.dismiss(self.query_one("#ask-answer", TextArea).text.strip())


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

    def __init__(
        self,
        *,
        桥,
        根: Path | None = None,
        取编排器=None,
        闸=None,
        **kw,
    ) -> None:
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
        #: 编排器工厂。**存工厂不存实例**——
        #: 一次运行一个实例，存实例就得管它什么时候过期。
        #: 没有它时界面**明说跑不了**，不假装（D-281 追加二）。
        self._取编排器 = 取编排器
        self._跑着 = False
        self._接上答复方(闸)

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
        """校验 → 取编排器 → 起 worker。

        ## 空诉求不启动

        空诉求会被编排器收下，然后卡在提问上等一个没有上下文的答复。
        **当场拒绝比让它跑起来再失败省用户十几分钟。**
        """
        诉求框 = self.query_one(RequestBox)
        需求 = 诉求框.诉求()
        if not 需求:
            self._说(f"{主题.符_失败} 请先写下诉求，再按 Ctrl+Enter。")
            return
        if self._跑着:
            self._说(f"{主题.符_进行中} 已在运行，本次请求未启动。")
            return
        if self._取编排器 is None:
            self._说(f"{主题.符_失败} 运行未接上：没有可用的编排器。")
            return
        档位 = 诉求框.当前档位()
        self._跑着 = True
        self._跑一轮(档位, 需求)

    @work(thread=True, exclusive=True, group="run")
    def _跑一轮(self, 档位: TierId, 需求: str) -> None:
        """**worker 线程**：整段编排都在这里跑（D-024 / 架构 §8.1）。

        模型请求实测几十秒量级，跑在 UI 线程里界面会**完全卡死**。

        界面更新一律走 `call_from_thread`——那是 Textual 唯一认可的
        「从别的线程改界面」的方式。
        """
        编排器 = self._取编排器()  # pyright: ignore[reportOptionalCall]
        self._桥.刷屏 = lambda: self.call_from_thread(self._刷新)
        try:
            self._桥.在worker里跑(编排器, 档位=档位, 需求=需求)
        finally:
            self._桥.刷屏 = None
            self.call_from_thread(self._收尾)

    def _收尾(self) -> None:
        self._跑着 = False
        self._刷新()

    def _说(self, 文案: str) -> None:
        self.query_one("#status", StatusLine).update(文案)

    def _刷新(self) -> None:
        """把投影结果刷到屏上。**只渲染，不推断**（D-063）。"""
        行 = self._桥.行()
        self.query_one(ArtifactPanel).显示(行)
        self.query_one(PreviewPanel).显示(self._最终正文())
        if 行:
            self.query_one("#status", StatusLine).update(行[0])
        产出 = self._桥.投影.artifacts
        if 产出 and not self._跑着:
            self._说(f"{主题.符_成功} 已完成：{产出[-1][0]}")

    def _最终正文(self) -> str:
        """最终成果全文。**从投影取，不从编排结果取**。

        取结果对象就等于让界面多一条通编排层的路——
        无界面驱动走的是投影，两边一旦分道，显示就会不一致（D-063）。
        """
        产出 = getattr(self._桥.投影, "artifacts", ())
        return 产出[-1][1] if 产出 else ""

    # ── 硬停车：把闸门的答复方接上（D-287 答复侧）──────────────
    def _答复(self, 字段: tuple[dict[str, object], ...]) -> str:
        """**worker 线程调用**，阻塞直到用户答完。

        阻塞在这里就是硬停车的语义：车停了就是停了（D-287）。

        ## 为什么不用 `push_screen_wait`（实测踩出来的）

        第一版写的是 `call_from_thread(self.push_screen_wait, 表单)`，
        运行时报
        `NoActiveWorker: push_screen must be run from a worker when wait_for_dismiss is True`。

        原因：`push_screen_wait` 要求**当前就在 worker 上下文里**，
        而 `call_from_thread` 只把回调搬进事件循环，**不建立 worker 上下文**。

        形态上看这像「本来就在 worker 里啊」——**编排确实跑在 worker 里**，
        但 worker 上下文是 Textual 的 `@work` 建立的，
        `call_from_thread` 会**另起一段上下文**跑回调，worker 身份不跟着走。

        故改用 `push_screen` + 回调：它只需要「把消息投递到事件循环」，
        不要求 worker 身份。答案由 dismiss 回调兑现给 `Future`，
        本线程在 `未来.result()` 上阻塞——**这就是硬停车**。
        """
        未来: Future[str] = Future()

        def 收(答案: object) -> None:
            if not 未来.done():
                未来.set_result(str(答案))

        self.call_from_thread(self.push_screen, _提问Modal(字段), 收)
        return 未来.result()

    def _接上答复方(self, 闸) -> None:
        if 闸 is not None:
            闸.挂答复(self._答复)

    # ── 供判据与后续阶段使用 ──
    def feed(self, 事件们) -> None:
        """把事件喂进桥接层并刷新。**判据与 worker 都用它。**"""
        self._桥.喂全部(事件们)
        self._刷新()

    def projection_lines(self) -> tuple[str, ...]:
        return self._桥.行()
