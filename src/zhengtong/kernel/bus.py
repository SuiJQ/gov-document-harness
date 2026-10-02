"""事件总线（架构 §5.3，D-075）。

**`seq` 为什么必须在泵线程里分配**：D-064 决定门禁走线程池，
于是门禁线程与编排线程会并发调 `emit`。若 `seq += 1` 裸写在
`emit` 里，多线程会读到同一个值再各自自增，产生**重复 seq**——
而 seq 是 trace 与 replay 的唯一排序依据，重复即意味着回放出来的时间线是假的。

架构实测过：4 线程 × 2000 次无锁分配，得 7997 个唯一值、3 个重复。

**为什么用队列而不是加锁**：加锁也能保证唯一，但锁会把并发发射串行化，
而门禁线程在等锁时会阻塞住自己的推进。用队列则发射方**只是入队就返回**，
真正的分配、脱敏、落盘、分发都在泵线程一处完成——这同时满足了
"唯一"、"有序"、"单点脱敏"三个要求。

**分发出去的永远是脱敏后的副本**：落盘的与分发的都是同一个已脱敏对象，
不存在"某条出口忘了脱敏"的可能。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from queue import SimpleQueue
from typing import Final

from ..domain.event import Event
from .journal import JournalPort
from .redactor import Redactor

Sink = Callable[[Event], None]

_SHUTDOWN: Final[object] = object()


def _恢复seq(journal: JournalPort | None) -> int:
    """续跑起点 = journal 里**已落盘的最大 seq**。无 journal 或空 journal 时为 0。

    **为什么取 `max(seq)` 而不是 `len(events)`**：journal 允许坏行
    （D-123 崩溃半行、D-124 未知 kind 都会被跳过），
    所以"条数"≠"这条日志一共发生过多少个事件"。
    拿条数当起点，一旦中间有坏行，新事件就会与后面的历史事件撞 seq——
    而 seq 重复意味着 replay 出来的顺序是假的，正是 D-064 要消灭的东西。

    **为什么初值是 max 而非 max+1**：泵线程里是 `self._seq += 1`
    之后才使用，初值给 max，第一个新事件自然落到 max+1，不会重复。
    也不需要在 pump 启动后再校正——`_seq` 此刻只在构造期被写一次。

    **续跑不需要额外标志位**：拿同一个 journal 路径重建 EventBus
    就是续跑；空 journal 自然得 0，等同全新运行。
    """
    if journal is None:
        return 0
    return max((e.seq for e in journal.read_all()), default=0)


class EventBus:
    """单点发射。**一个程序一个实例。**"""

    def __init__(self, redactor: Redactor, journal: JournalPort | None = None) -> None:
        self._redactor: Redactor = redactor
        self._journal: JournalPort | None = journal
        self._inbox: SimpleQueue[Event | object] = SimpleQueue()
        self._sinks: list[Sink] = []
        self._sinks_lock: threading.Lock = threading.Lock()
        # D-125：续跑时从 journal 恢复 seq 起点，**不恒从 0 开始**。
        # 恢复点放在构造期——此刻泵线程尚未启动，不存在与 `_drain`
        # 争抢 `_seq` 的并发问题；放到别处才需要额外加锁。
        self._seq: int = _恢复seq(journal)
        self._stopped: bool = False
        self._pump: threading.Thread = threading.Thread(
            target=self._drain, name="event-pump", daemon=True
        )
        self._pump.start()

    # ── 发射方（任意线程）──

    def emit(self, event: Event) -> None:
        """任意线程可调用。**只入队**，不分配 seq、不脱敏、不落盘。"""
        if self._stopped:
            # 总线已停还收到事件，说明有关闭后仍在跑的线程。
            # 静默丢弃会让"关了还有东西在跑"这个问题彻底看不见。
            raise RuntimeError("事件总线已停止，仍收到发射请求")
        self._inbox.put(event)

    def subscribe(self, sink: Sink) -> None:
        """注册订阅者。**须在发射开始前调用完**，否则可能漏事件。"""
        with self._sinks_lock:
            self._sinks.append(sink)

    # ── 泵线程（唯一发射点）──

    def _drain(self) -> None:
        while True:
            item = self._inbox.get()
            if item is _SHUTDOWN:
                return
            if isinstance(item, _Probe):
                item.handle()
                continue
            # 已排除 _SHUTDOWN 与 _Probe，剩下只可能是 Event。
            # 这里断言一次而非 ignore：队列只由 emit() 写入，
            # 而 emit() 的签名就收 Event，类型不该有别的可能——
            # 万一将来混入别的东西，这条断言会立刻指出来。
            assert isinstance(item, Event), f"队列混入了非事件对象：{type(item).__name__}"
            safe = self._redactor.scrub(item)
            self._seq += 1
            safe = replace(safe, seq=self._seq)
            if self._journal is not None:
                self._journal.append(safe)  # 落盘的是已脱敏的
            for sink in self._sinks:
                sink(safe)  # 分发的也是已脱敏的

    # ── 生命周期 ──

    def stop(self, drain: bool = True, timeout: float = 2.0) -> None:
        """停止泵线程。

        `drain=True` 时投 `_SHUTDOWN`——队列是 FIFO，
        故它前面已入队的事件会被全部处理完，然后泵线程退出。
        `drain=False` 用于异常退出路径，直接放弃剩余事件。
        """
        self._stopped = True
        if drain:
            # 正常路径：让已入队的事件全部处理完再停。
            self._inbox.put(_SHUTDOWN)
        else:
            # 异常退出路径：不排空，直接把队列换掉。
            # 残留事件指的是"还没落盘"的部分——让它们丢掉，
            # 好过在卡住的程序里继续等日志写完。
            self._inbox = SimpleQueue()
            self._inbox.put(_SHUTDOWN)
        self._pump.join(timeout)

    def flush(self, timeout: float = 2.0) -> bool:
        """等队列里的事件全部被泵线程处理完。**不消费队列。**

        实现要点：不能靠"取一个出来看看空不空"来判断——那样会把事件取走，
        事件就永远不会落盘。正确做法是投一个**信标**进队列，
        等泵线程处理到它就说明前面的都处理完了。
        """
        if self._stopped:
            # 审计 D-136：总线已停时**不能白等一个超时**。
            # 泵线程已退出，_Probe 永远不会被处理，
            # 原实现只会干等到 timeout 耗尽再返回 False——
            # 而 False 与「超时了」在调用方看来完全一样，
            # 没有任何信号说��「这次根本没人在处理」。
            #
            # 为什么这个区分要紧：`driver.py` 有两处 `_ = bus.flush()`，
            # 一处在宣布**任务成功**前，一处在发**失败原因**后。
            # 后者的注释明写着「若这里图省事不 flush，
            # 用户就看不到失败原因」——可 flush 静默失败时，
            # 原因照样看不到，且还要额外白等 2 秒。
            return False
        probe = _Probe()
        self._inbox.put(probe)
        return probe.done.wait(timeout)

    @property
    def current_seq(self) -> int:
        """已分配的最大 seq。**只由泵线程写。**"""
        return self._seq

    def __enter__(self) -> EventBus:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


class _Probe:
    """空操作信标。泵线程遇到它只置位，不做任何其他事。"""

    def __init__(self) -> None:
        self.done: threading.Event = threading.Event()

    def handle(self) -> None:
        self.done.set()
