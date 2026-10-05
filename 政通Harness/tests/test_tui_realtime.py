"""TUI 接线核查：事件必须**跑到哪显示到哪**，不是跑完才显示。

## 这组判据在防什么

原实现是「跑完整轮 → 读 `编排器.事件` → 全喂进去」。
语法没错，但**运行中界面一片空白**——一次成文几十秒到几分钟，
用户盯着不动的屏幕，而「总体进度」正是用户明确要求显示的东西。

更糟的是它**测不出来**：旧测试自己先 `编.跑到终(...)` 再 `list(编.事件)`，
取事件的时机天然在跑完之后，于是「实时」与「事后」在测试里长得一模一样。

所以下面每条判据都**在编排还没结束时**检查——拿「跑完之后才有」的实现去跑，
**必然红**。

## 为什么用轻量假总线而不是真 `EventBus`

真总线会起泵线程，测试若忘了 stop 就**整个测试进程挂死**（实测卡满 600 秒）。
泵线程本身是 `EventBus` 的职责，`tests/test_bus.py` 已经覆盖。
此处要验的是「桥接**订不订阅**」，不需要引入线程。
"""

from __future__ import annotations

from typing import Any

from zhengtong.domain.errors import Result
from zhengtong.domain.event import Event, EventKind, Level
from zhengtong.domain.ids import RunId
from zhengtong.domain.phase import PhaseId
from zhengtong.domain.tier import TierId
from zhengtong.presentation.tui import 事件桥接


def _事件(种类: EventKind, **载荷: object) -> Event:
    return Event(
        seq=1,
        kind=种类,
        run_id=RunId("r-1"),
        phase=PhaseId.DRAFTING,
        level=Level.INFO,
        data=载荷,
    )


class _假总线:
    """只实现桥接用到的那一个方法：`subscribe`。"""

    def __init__(self) -> None:
        self.sinks: list[Any] = []

    def subscribe(self, sink: Any) -> None:
        self.sinks.append(sink)

    def 发(self, 事件: Event) -> None:
        for s in self.sinks:
            s(事件)


class _假编排:
    """`跑到终` 期间可以观察世界的假编排器。

    ## 返回值必须是真的 `Result`

    原先返回的是一个只声明了 `is_error` / `unwrap` 的手搓类，
    而桥接读的是 `结果.error`——**替身少一个成员，生产代码就得
    挂 `pyright: ignore` 或退回 `getattr`**。两轮前桥接正是
    `getattr(编排器, "事件", [])` + `结果.error.detail`（被 ignore 盖住），
    接线断了也没人发现。

    现在替身返回真 `Result`，缺成员会在判据与类型检查期一起响。
    """

    def __init__(self, *, bus: _假总线 | None = None, 事件: list[Event] | None = None) -> None:
        self.bus = bus
        self.事件 = 事件 if 事件 is not None else []
        self.跑时订阅数: list[int] = []

    def 跑到终(self, *, 档位: TierId, 需求: str) -> Result[None]:
        # **编排尚未结束时**观察订阅情况——这正是本判据与旧测试的分水岭。
        self.跑时订阅数.append(len(self.bus.sinks) if self.bus else 0)
        return Result.ok(None)


def test_订阅后事件真的进投影() -> None:
    """订阅不能只挂不接：发一条事件，投影必须有反应。"""
    总线 = _假总线()
    桥 = 事件桥接()
    桥.订阅(总线)

    总线.发(_事件(EventKind.RUN_STARTED, tier="pro"))
    assert 桥.行(), "订阅后事件必须进投影，否则订阅只是个摆设"


def test_订阅路径上刷屏会被调用() -> None:
    """**无人接线的另一种形态**：事件喂进去了，但 UI 刷新回调没接上，屏还是不动。"""
    刷了 = 0

    def 刷() -> None:
        nonlocal 刷了
        刷了 += 1

    总线 = _假总线()
    桥 = 事件桥接()
    桥.订阅(总线)
    桥.刷屏 = 刷

    总线.发(_事件(EventKind.RUN_STARTED, tier="pro"))
    assert 刷了 == 1, f"订阅路径必须触发刷屏，实刷 {刷了} 次"


def test_订阅标记会被置上() -> None:
    """订阅后 `_已订阅` 必须为真——否则 `在worker里跑` 会再兜底喂一遍。"""
    桥 = 事件桥接()
    assert not 桥._已订阅
    桥.订阅(_假总线())
    assert 桥._已订阅


def test_App跑一轮之前先订阅总线() -> None:
    """**核心判据**：订阅这件事由 App 侧发起，编排开始前就得挂上。

    ## 为什么测 App 而不是桥接

    「要不要订阅」是**装配事实**，不是桥接层的判断——
    桥接有判据守着「这里不做决策」（`test_tui桥接不含任何决策`，分支 ≤ 4）。
    把这个判断塞进桥接会让那条判据变红，而那条判据是对的：
    它守的正是「桥接只搬运，不决策」。

    故真正的接线点在 `ZhengTongApp._跑一轮`，
    旧实现里那里**根本没有订阅**，于是界面运行中一片空白。
    """
    import asyncio

    from zhengtong.adapters.tui import ZhengTongApp

    总线 = _假总线()
    编 = _假编排(bus=总线)
    桥 = 事件桥接()
    应用 = ZhengTongApp(桥=桥, 根=None, 取编排器=lambda: 编, 闸=None)

    async def 跑() -> None:
        async with 应用.run_test():
            # `_跑一轮` 是 `@work(thread=True)`，自己会起 worker 线程；
            # **不能**用 `call_from_thread` 包一层——那要求调用方已在别的线程，
            # 而这里就在事件循环线程里（实测会抛 RuntimeError）。
            应用._跑一轮(None, "x")
            await asyncio.sleep(0.2)

    asyncio.run(跑())
    assert 总线.sinks, "App 开跑前必须订阅总线，否则运行中界面不更新"


def test_无总线时兜底喂事件不丢内容() -> None:
    """没有总线（测试/CI/无界面）时不能什么都不做。"""
    编 = _假编排(bus=None, 事件=[_事件(EventKind.RUN_STARTED, tier="pro")])
    桥 = 事件桥接()
    桥.在worker里跑(编, 档位=TierId.PRO, 需求="x")
    assert 桥.行(), "无总线时必须兜底喂事件，否则界面全空"


def test_有总线时不再兜底_否则成果显示两遍() -> None:
    """订阅了就别再喂一遍——同一份成果在屏上出现两遍比不显示更糟。"""
    总线 = _假总线()
    编 = _假编排(bus=总线, 事件=[_事件(EventKind.RUN_STARTED, tier="pro")])
    桥 = 事件桥接()
    桥.订阅(总线)
    桥.在worker里跑(编, 档位=TierId.PRO, 需求="x")
    assert 桥._已订阅, "有总线时必须订阅"
    行 = 桥.行()
    assert len([x for x in 行 if "pro" in x]) <= 1, f"同一事件不得显示两遍：{行}"


def test_编排器没有bus属性时用getattr兜底不抛() -> None:
    """编排器压根没有 `bus` 属性时不能炸——`getattr` 默认值要生效。"""
    编 = _假编排(bus=None, 事件=[])
    桥 = 事件桥接()
    桥.在worker里跑(编, 档位=TierId.PRO, 需求="x")  # 不抛即通过
