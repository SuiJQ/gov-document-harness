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

理由：显示一律取自**同一个投影**（D-294 无界面模式已删除）。
一旦视图自己推断，两边就会给出不同结论——
而「三个驱动同源」是这个项目早就确立的纪律（D-063）。

判据 T2 守着这条：同事件喂两个驱动，显示必须逐行相同。

## 关于「正在等模型」

全系统不使用流式（D-026；流式探测已按 D-295 整体删除），
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
        yield TextArea(id="need", placeholder=主题.提示_诉求)
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

    # ── 一个框，两种角色（B3）──────────────────────────────────
    #
    # ## 为什么不是两个框
    #
    # 提过的方案：再加一个插话框。问题很直白——**用户打开程序看到两个输入框，
    # 不知道该往哪个里打字**。要靠标签、靠颜色、靠提示语去区分，
    # 都是在给一个本来不该存在的问题打补丁。
    #
    # 故改成**同一个框变两种角色**：
    # - 未开始：五行多行，标题「诉求」
    # - 运行中：一行单行，标题「插话」
    #
    # 形态变化本身就是状态提示——比任何文字说明都直接，
    # 而且**只有一个焦点落点**，不需要额外的焦点规则。
    #
    # ## 提交后不换框、只变小
    #
    # 提交诉求 → 开始运行 → 框缩成单行。
    # **焦点必须留在框里**：用户提交完通常马上想说点什么，
    # 焦点跳走就得再点一次，那「一个框」的连贯性就断了。

    def 切到插话(self) -> None:
        """变成插话框：单行 + 换标题。"""
        self._设标题(主题.文案_插话)
        self._设高度(1)
        self.query_one("#need", TextArea).placeholder = 主题.提示_插话

    def 切回诉求(self) -> None:
        """回到诉求框。**完成态不调用**——完成后是禁用，不是变回诉求。"""
        self._设标题(主题.文案_诉求)
        self._设高度(5)
        self.query_one("#need", TextArea).placeholder = 主题.提示_诉求

    def 锁定(self) -> None:
        """完成态：禁用并变灰。

        **不做成「再写一份」**——那会引入重跑语义（新建 run？
        覆盖产物？），远超当前范围。禁用是唯一诚实的表达。
        """
        self._设标题(主题.文案_已完成)
        框 = self.query_one("#need", TextArea)
        框.disabled = True
        框.placeholder = 主题.提示_已完成

    def 清空并交焦点(self) -> None:
        """提交后清空，并把焦点留在框里。"""
        框 = self.query_one("#need", TextArea)
        框.text = ""
        框.focus()

    def _设标题(self, 文案: str) -> None:
        for 节点 in self.query(Static):
            if 节点.has_class(主题.色_标题) and 节点.id is None:
                节点.update(文案)
                return

    def _设高度(self, 行: int) -> None:
        self.query_one("#need", TextArea).styles.height = 行


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
        Binding("ctrl+enter", "开始", "提交"),
        Binding("escape", "取消", "取消"),
        Binding("ctrl+o", "退出", "退出"),
        # **覆盖 Textual 默认的 `ctrl+q` → quit**（B3）。
        #
        # 不覆盖的话 `ctrl+o` 和 `ctrl+q` 都能退出，用户得记两个——
        # 而退出是低频动作，记两个比记一个更容易记混。
        #
        # 写法说明：子类 BINDINGS 在同键上**覆盖**父类，
        # 所以绑到一个空动作即可，`show=False` 让它不出现在帮助栏。
        #
        # 曾试 `ENABLED_BINDINGS = []`——**没用**，那个字段管的是
        # 「启用哪些绑定组」，不是「解绑某个键」。判据
        # `test_ctrl_q按了不退` 直接把它顶掉了。
        Binding("ctrl+q", "noop", show=False),
    ]

    def action_noop(self) -> None:
        """**故意什么都不做**。存在只为让 `ctrl+q` 有一个可绑的落点。

        写成 `Binding("ctrl+q", "noop")` 而不加 `show=False` 也有效，
        但帮助栏会多出一条没有意义的键位。
        """

    def action_退出(self) -> None:
        """**退出程序。这是 `ctrl+o` 唯一的落点。**

        ## 这个方法此前不存在

        `Binding("ctrl+o", "退出")` 从建起就在，但 `action_退出`
        **全仓从来没写过**——于是这个绑定指向一个不存在的动作，
        按下去什么也不发生。

        后果不是「少一个功能」，是**程序跑完之后没法退出**：
        实测连发 40 次 `ctrl+o` 进程纹丝不动，只能被 `SIGTERM` 杀。
        而连带后果更大——缺陷 4 修的退出码「永远到不了进程」，
        因为取退出码得先让界面退出。

        ## 退出码不在这里算

        `启动` 在 `run()` 返回后按 `运行失败` 算 0/1。
        这里再传一遍 `result=` 会让退出码有**两个来源**——
        两处一旦分叉，又是一次「声明与实现脱节」。
        故 `action_退出` 只管退出这一件事。
        """
        self.exit()

    def __init__(
        self,
        *,
        桥,
        根: Path | None = None,
        取编排器=None,
        闸=None,
        终端提示: str | None = None,
        推理提示=None,
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
        （没装 textual 的机器上连交互界面都起不来）。

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
        #: **终端字形提示**（D-045）。由装配根算好后注入——
        #: 界面只显示，不自己查。
        self._终端提示 = 终端提示
        #: **推理开关提示**（D-384）。`档位 -> 该不该说一句话`，由装配根注入。
        #:
        #: 为什么走 callable 注入而不是界面自己查：结论在
        #: `system/reasoning.json`，读它要经适配层（`adapters` 不能 import
        #: 适配器同层的其它模块吗——能，但配置解析属于装配根职责）。
        #: 与 `取编排器` 同一形状：**界面只认形状，不认实现**。
        self._推理提示 = 推理提示
        #: 本次运行的编排器。**插话要发给它**，而它由 worker 建出来，
        #: 所以只能存引用——界面侧不 import 编排层（D-063 的依赖方向）。
        self._当前编排器 = None
        self._接上答复方(闸)

    @property
    def 运行失败(self) -> str:
        """最近一次编排的失败原因；空串＝没失败。

        **公开它是为了退出码**：`tui_app.启动` 据此返回非零。
        此前恒返回 0，于是「intake 落盘失败、界面再崩两处」
        也报成功——用户和脚本都以为跑通了。

        转发给桥接而不是自己存：桥接才是收到 `Result` 的那一方，
        界面另存一份就会与桥接分叉（同一类老毛病）。
        """
        return self._桥.末次失败

    def compose(self) -> ComposeResult:
        yield Static(主题.文案_标题, id="app-title")
        with Vertical(id="request-area"):
            yield RequestBox()
            # **键位照 `主题.键位表` 写，不许凭记忆**（缺陷 6）。
            #
            # 此处原写「Ctrl+Q 退出」，而 `ctrl+q` 绑的是 `noop`
            # （故意解绑 Textual 默认退出键，见 BINDINGS 的注释）——
            # 用户照着提示按，按的是一个什么都不干的键。
            # 真退出键是 `ctrl+o`，且**这一行本身就是判据**：
            # 真退出键改了而这里没改，同一个毛病会再犯一次。
            yield Static("[ 开始 ]　Ctrl+Enter　　Ctrl+O 退出", id="start-button")
        yield StatusLine(id="status")
        yield ArtifactPanel(id="artifacts")
        yield PreviewPanel(id="preview")
        yield StatusLine(id="notice", classes=主题.色_需处理)
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
        if self._终端提示:
            # **仅提示、不阻止**（用户明确要求）。
            # 用 `色_需处理`（黄）而不是报错色——它不是错误，
            # 程序照跑，只是显示可能不好看。
            self.query_one("#notice", StatusLine).update(
                f"{主题.符_提示} 终端提示：{self._终端提示}"
            )

    def action_开始(self) -> None:  # Textual 的 action 命名
        """`Ctrl+Enter`。**同一个键，按当前状态做不同的事**。

        - 未运行 → 开始
        - 运行中 → 把框里的内容作为插话送出（D-012）

        ## 为什么共用一个键而不是两个

        两件事都是「提交手上这个框」，用户视角是**同一个动作**。
        分成两个键（开始 / 插话）就是让用户先判断状态再选键——
        而状态在界面上明明写着，那是把判断推给用户。
        """
        if self._跑着:
            self._送插话(self.query_one(RequestBox).诉求())
            return
        self._开始一次()

    def _开始一次(self) -> None:
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
        # **推理开关提示**（D-384）。OFF 档（咨询/Flash）按定义不该付推理的钱，
        # 但没探测过或关不掉时程序**照常按开启跑**——用户以为没花钱，
        # 实际花了。故此处提示一次。只在 UI 线程读一次小文件，可接受。
        if self._推理提示 is not None:
            提示 = self._推理提示(档位)
            if 提示:
                self._说(f"{主题.符_进行中} {提示}")
        # **框变插话框，焦点留在原地**——用户提交后通常马上想补充，
        # 焦点跳走就得再点一次，那「一个框」的连贯性就断了。
        诉求框.切到插话()
        诉求框.清空并交焦点()
        self._说(f"{主题.符_进行中} 诉求已提交，此处改为插话（下一个阶段生效）")
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
        self._当前编排器 = 编排器
        self._桥.刷屏 = lambda: self.call_from_thread(self._刷新)
        try:
            # **订阅与否是装配事实，不由桥接层决定**——
            # 桥接有判据守着「这里不做决策」（test_tui桥接不含任何决策）。
            if 编排器.bus is not None:
                self._桥.订阅(编排器.bus)
            self._桥.在worker里跑(编排器, 档位=档位, 需求=需求)
        finally:
            self._桥.刷屏 = None
            self.call_from_thread(self._收尾)

    def _收尾(self) -> None:
        self._跑着 = False
        self._刷新()
        产出 = self._桥.投影.artifacts
        if 产出:
            self.query_one(RequestBox).锁定()

    def action_取消(self) -> None:
        """`Esc`。**协作式中断**：在下一个阶段边界停下。

        ## 为什么不 `workers.cancel_all()`

        那会把 worker 线程**强杀**，于是 `_跑一轮` 的 `finally` 不会执行——
        `刷屏` 复位、`_收尾` 通知、界面解锁**全都不发生**：
        用户按下 Esc，界面反而永远卡在「运行中」。

        更要紧的是编排状态：已跑完阶段的产物、落盘的状态本来都保住了，
        强杀会让这次运行变成**说不清有没有跑完**的一团。

        故只置取消标志，让编排自己在边界收尾。

        ## 为什么不立刻停

        正在飞行的模型请求杀不掉。明确承诺边界比假装能停诚实：
        用户按了 Esc，看到的是「已收到，当前阶段跑完就停」。

        ## 没在运行时

        **什么都不做，也不提示**——主界面此时无物可取消。
        按了没反应比弹一句「没有可取消的」干净。
        """
        if not self._跑着:
            return
        编排器 = self._当前编排器
        if 编排器 is None:
            self._说(f"{主题.符_失败} 无法取消：编排器还没建出来。")
            return
        if 编排器.已取消:
            # 重复按键**不再提示**：第二次按下去什么都不发生，
            # 与其让用户以为「按坏了」，不如把第一次的提示留住。
            return
        编排器.请求取消()
        self._说(f"{主题.符_进行中} 已收到取消，当前阶段跑完即停（已完成的部分会保留）")

    def _送插话(self, 文本: str) -> None:
        """把用户插话送给编排器。

        **空的不送**：给模型塞一条空消息没有任何用处，
        而界面没提示的话用户会以为刚才那句丢了。
        """
        净 = 文本.strip()
        if not 净:
            self._说("插话是空的，没有送出。")
            return
        if not self._跑着:
            self._说("现在不在运行中，插话无处可送。")
            return
        编排器 = self._当前编排器
        if 编排器 is None:
            self._说("插话无处可送：没有编排器。")
            return
        编排器.收用户指令(净)  # pyright: ignore[reportAttributeAccessIssue]
        self.query_one(RequestBox).清空并交焦点()
        self._说(f"{主题.符_成功} 插话已送出，在下一个阶段生效")

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
        显示走的是投影，一旦分道，TUI 与投影就不一致。

        **直接读字段，不再 `getattr` 兜底**：兜底的名字写错也静默返回
        `()`，最终成果就全空而不报错。而 `Projection` 协议现已逐字段
        声明结构化字段（`kernel/projection.py`），这里读错字段名会在
        类型检查期报错——这正是兜底要换掉的理由。
        """
        产出 = self._桥.投影.artifacts
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
