"""TUI 桥接：worker 线程跑编排，事件经 `call_from_thread` 丢回 UI 侧（D-037）。

## 为什么必须是 worker 线程

架构 §8.1 原文：

> TUI 跑这段循环在 **worker 线程**，通过 `call_from_thread`
> 把事件丢给 asyncio 侧。67 秒的模型请求不会冻住界面
> （D-024 实测已证明这个量级真实存在）。

这不是洁癖：一次模型请求实测就是几十秒量级，
跑在 UI 线程里界面会**完全卡死**。

## 为什么桥接这么薄

架构同节：「TUI 侧要做的桥接只有约 20 行」。
桥接层只负责搬运事件与把投影结果刷到屏上，
**不解释事件、不判断进度、不重算成果**——
那些是 `StandardProjection` 的事。

判据里有一条专门量这个：`test_tui桥接不超过几十行`。
薄不是美学，是同源的证据。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from ..domain.event import Event
from ..kernel.projection import DebugProjection, Projection, StandardProjection

#: Textual 的 UI 线程回调。由 App 侧注入，桥接层自己不碰 asyncio。
刷屏 = Callable[[], None]


class 可订阅(Protocol):
    """只要能挂 sink 就行。

    ## 为什么定协议而不是直接用 `EventBus`

    桥接层**只需要「挂一个回调」这一件事**。写死 `EventBus` 会带来两个坏处：
    ① 测试要造真总线，而真总线会起泵线程——测试忘了 stop 就**整个进程挂死**
       （实测卡满 600 秒）；② 换实现就得改这里。

    依赖最小面正是本项目反复强调的那条：**端口存在的意义是「实现可换」**，
    签名写死实现等于废掉端口。
    """

    def subscribe(self, sink: Callable[[Event], None]) -> None:
        """注册订阅者。"""
        ...


@dataclass(slots=True)
class 事件桥接:
    """事件搬运桥。

    ## 为什么不自己继承 Textual 的 App

    继承 App 就要 import textual 运行时依赖，
    而**便携运行时**必须能跑在没有 textual 的环境里
    （开发依赖不进运行时，架构 §15 依赖策略）。
    故桥接层是纯搬运，Textual 的 App 放在可选模块里。
    """

    投影: Projection
    刷屏: 刷屏 | None = None
    #: 是否已挂上总线。见 `在worker里跑`——订阅了就不能再兜底喂一遍，
    #: 否则同一条事件进投影两次，阶段成果会重复显示。
    #:
    #: **必须是声明字段**：`@dataclass(slots=True)` 重建类时 slots 只收
    #: 声明过的字段，在 `__init__` 里凭空 `self._已订阅 = False`
    #: 会抛 `AttributeError`——slots 不给未声明的名字留位置。
    _已订阅: bool = False

    def __init__(self, *, 调试: bool = False) -> None:
        self.投影 = DebugProjection() if 调试 else StandardProjection()
        self.刷屏 = None
        self._已订阅 = False

    @property
    def 投影类(self) -> type[Projection]:
        return type(self.投影)

    def 喂(self, 事件: Event) -> None:
        """worker 线程或泵线程调用它。

        ## 线程边界在这里

        `accept` 是纯内存操作（改几个字段），可以安全地
        在任何线程调用。**但刷屏不行**——Textual 要求
        UI 更新走 `call_from_thread`，所以只有刷屏才需要回调。

        故刷屏是可选的：不在 UI 环境里（比如测试、CI），
        本桥接照样能用，只是不刷新屏幕。
        """
        self.投影.accept(事件)
        if self.刷屏 is not None:
            self.刷屏()

    def 订阅(self, bus: 可订阅) -> None:
        """挂到事件总线上，让事件**跑到哪显示到哪**。

        ## 为什么必须订阅，而不是跑完再一次性取

        此前 `在worker里跑` 是「跑完整轮 → 读 `编排器.事件` → 全喂进去」。
        语法上没毛病，**实际上运行中界面一片空白**：
        一次成文实测几十秒到几分钟，用户盯着一个不动的屏幕，
        而「总体进度」是用户明确要求显示的东西。

        更隐蔽的是它**测试测不出来**：`test_tui_b1` 自己先
        `编.跑到终(...)` 再 `list(编.事件)`，取事件的时机天然在跑完之后，
        于是「实时」与「事后」在测试里长得一模一样。

        总线本来就是为这个准备的（`EventBus.subscribe`），
        装配层也**早把真总线交给了编排器**（`app.py` 传 `bus=self.bus`）——
        接好了、没人订阅，与本项目反复出现的那类缺口同型。

        ## 为什么「要不要订阅」由调用方决定

        桥接层有一条判据守着「这里不做决策」（`test_tui桥接不含任何决策`，
        条件分支不超过 4 处）。「有没有总线」是**装配**的事实，不是桥接该问的，
        故 `订阅` 不接 `None`、不自己判断，由 App 侧决定调不调。
        """
        bus.subscribe(self.喂)
        self._已订阅 = True

    def 喂全部(self, 事件们: Iterable[Event]) -> None:
        for 事件 in 事件们:
            self.喂(事件)

    def 行(self) -> tuple[str, ...]:
        """屏上该显示什么。**与无界面模式走同一个投影**。"""
        return self.投影.lines()

    def 在worker里跑(self, 编排器: object, *, 档位: object, 需求: str) -> None:
        """在 worker 线程里跑完整个编排，逐条把事件喂进来。

        用法（TUI 侧）：

        ```python
        桥 = 事件桥接()
        桥.刷屏 = lambda: self.call_from_thread(self.refresh)
        await asyncio.to_thread(桥.在worker里跑, 编排器, 档位=档, 需求=需求)
        ```
        """
        结果 = 编排器.跑到终(档位=档位, 需求=需求)  # pyright: ignore[reportAttributeAccessIssue]
        if not self._已订阅:
            # **只在没订阅时才兜底喂一遍**：已订阅的话事件早就实时喂过了，
            # 这里再喂一次会让同一份成果在屏上出现两遍。
            # 没总线时（测试、CI、无界面调用）才走这条兜底路。
            self.喂全部(getattr(编排器, "事件", []))
        if 结果.is_error:
            self.投影.errors = (*self.投影.errors, 结果.error.detail)  # pyright: ignore[reportAttributeAccessIssue]
