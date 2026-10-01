"""`kernel/bus.py` 测试 —— 队列 + 泵线程单点发射（D-072）。

## 为什么这个文件的判据是「单点」

架构修复期发现过一个真实缺陷：并行退化成串行、
`wait_idle` 吞事件——根因都是**发射逻辑散落在多处**。

D-072 定的方案是「队列 + 泵线程**单点发射**」：
`emit` 只入队，**不分配 seq、不脱敏、不落盘**，
这三件事全部只在泵线程里做一次。

故判据不是"事件能发出去"（那太弱），而是：

> **同一件事只能发生在一个地方。**
> 一旦 seq 分配或脱敏散到第二处，两处就会各算各的。

另外：脱敏与落盘的**顺序**也有讲究——
落盘的必须是脱敏后的（D-030），落盘发生在分发之前还是之后，
决定了日志里记录的是不是用户最终看到的那份。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from zhengtong.domain.event import Event, EventKind
from zhengtong.domain.ids import RunId
from zhengtong.domain.phase import PhaseId
from zhengtong.kernel.bus import EventBus, Sink
from zhengtong.kernel.journal import JsonlJournal
from zhengtong.kernel.redactor import Redactor

KEY = "sk-proj-ABCDEFGH12345678XYZ"


# ═══ 单点发射 ═══


def test_emit不分配seq由泵线程分配(tmp_path: Path) -> None:
    """**D-072 的核心判据**：seq 只能由泵线程分配。

    若 `emit` 也分配，就会出现两个计数器各算各的，
    落盘的 seq 与分发的 seq 对不上。
    """
    收到: list[Event] = []
    b = _总线(tmp_path, sinks=[收到.append])
    _ = b.emit(_事件(call_id="x"))
    assert b.flush() is True
    b.stop()
    assert [e.seq for e in 收到] == [1]


def test_seq按实际处理顺序连续(tmp_path: Path) -> None:
    """D-072：`Event.seq` 表示事件**发生顺序**，不得跳号。"""
    收到: list[Event] = []
    b = _总线(tmp_path, sinks=[收到.append])
    for i in range(1, 6):
        _ = b.emit(_事件(call_id=str(i)))
    _ = b.flush()
    b.stop()
    assert [e.seq for e in 收到] == [1, 2, 3, 4, 5]


def test_emit返回前不落盘(tmp_path: Path) -> None:
    """`emit` 只入队——它是「任意线程可调用」的入口，必须轻。"""
    j = JsonlJournal(tmp_path / "ev.jsonl")
    b = EventBus(Redactor(), j)
    _ = b.emit(_事件())
    # 不 flush 不 join，落盘与否取决于泵线程是否已跑到，不可断言为"未落盘"，
    # 但 flush 之后**必须**落盘——这是可断言的。
    assert b.flush() is True
    b.stop()
    assert len(j.read_all()) == 1


def test_多个订阅者都收到同一份已脱敏事件(tmp_path: Path) -> None:
    """分发的是**脱敏后**那份（D-030）：落盘的、分发的必须是同一份。"""
    a: list[Event] = []
    c: list[Event] = []
    b = _总线(tmp_path, sinks=[a.append, c.append])
    _ = b.emit(_事件(data={"key": KEY}))
    _ = b.flush()
    b.stop()
    assert a[0] == c[0]
    assert KEY not in str(a[0].data), "分发的是未脱敏的事件"


def test_落盘的是脱敏后那份(tmp_path: Path) -> None:
    """密钥绝不能进日志（D-030）——落盘发生在分发前后都要脱敏，这里锁死落盘。"""
    j = JsonlJournal(tmp_path / "ev.jsonl")
    b = EventBus(Redactor(), j)
    _ = b.emit(_事件(data={"请求体": f"Bearer {KEY}"}))
    _ = b.flush()
    b.stop()
    全部 = str(j.read_all())
    assert KEY not in 全部


def test_落盘seq与分发seq一致(tmp_path: Path) -> None:
    """**单点的直接体现**：两个出口的 seq 必须是同一个值。"""
    收到: list[Event] = []
    j = JsonlJournal(tmp_path / "ev.jsonl")
    b = EventBus(Redactor(), j)
    b.subscribe(收到.append)
    for i in range(1, 4):
        _ = b.emit(_事件(call_id=str(i)))
    _ = b.flush()
    b.stop()
    assert [e.seq for e in j.read_all()] == [e.seq for e in 收到]


def test_无journal时也能分发() -> None:
    """journal 是可选依赖。"""
    收到: list[Event] = []
    b = EventBus(Redactor(), None)
    b.subscribe(收到.append)
    _ = b.emit(_事件())
    assert b.flush() is True
    b.stop()
    assert len(收到) == 1


# ═══ 并发 ═══


def test_多线程并发发射不丢事件(tmp_path: Path) -> None:
    """`emit` 是"任意线程可调用"的入口。"""
    收到: list[Event] = []
    b = _总线(tmp_path, sinks=[收到.append])

    def 发(起: int) -> None:
        for i in range(20):
            _ = b.emit(_事件(call_id=f"{起}-{i}"))

    线程 = [threading.Thread(target=发, args=(n,)) for n in range(5)]
    for t in 线程:
        t.start()
    for t in 线程:
        t.join()
    assert b.flush() is True
    b.stop()
    assert len(收到) == 100
    assert [e.seq for e in 收到] == list(range(1, 101)), "seq 必须无缺口无重复"


def test_并发发射不崩() -> None:
    """多个线程并发调用 `emit` 不得抛异常、不得死锁。"""
    b = EventBus(Redactor(), None)
    错: list[BaseException] = []

    def 发() -> None:
        try:
            for i in range(30):
                _ = b.emit(_事件(call_id=str(i)))
        except BaseException as e:
            错.append(e)

    线程 = [threading.Thread(target=发) for _ in range(4)]
    for t in 线程:
        t.start()
    for t in 线程:
        t.join()
    _ = b.flush()
    b.stop()
    assert not 错, f"并发发射抛异常：{错}"


# ═══ flush 信标 ═══


def test_flush不消费事件(tmp_path: Path) -> None:
    """**flush 的实现要点**（源注释）：不能靠"取一个看看空不空"判断——
    那样会把事件取走，事件就永远不会落盘。故投信标而非取事件。"""
    收到: list[Event] = []
    j = JsonlJournal(tmp_path / "ev.jsonl")
    b = EventBus(Redactor(), j)
    b.subscribe(收到.append)
    for i in range(1, 4):
        _ = b.emit(_事件(call_id=str(i)))
    assert b.flush() is True
    assert b.flush() is True  # 再 flush 一次，事件不该消失
    b.stop()
    assert len(收到) == 3
    assert len(j.read_all()) == 3


def test_flush在队列为空时立即返回(tmp_path: Path) -> None:
    b = _总线(tmp_path)
    assert b.flush() is True
    b.stop()


def test_flush超时返回False(tmp_path: Path) -> None:
    """返回值有意义：调用方要能据此判断"没处理完"。"""
    b = _总线(tmp_path)
    b.stop()
    assert b.flush(timeout=0.05) is False, "泵线程已停，flush 不该再报成功"


# ═══ 生命周期 ═══


def test_stop排空队列(tmp_path: Path) -> None:
    """`drain=True`（默认）：队列是 FIFO，投 `_SHUTDOWN` 后
    它前面已入队的事件会被全部处理完。"""
    收到: list[Event] = []
    j = JsonlJournal(tmp_path / "ev.jsonl")
    b = EventBus(Redactor(), j)
    b.subscribe(收到.append)
    for i in range(1, 11):
        _ = b.emit(_事件(call_id=str(i)))
    b.stop()
    assert len(收到) == 10, "stop(drain=True) 没能排空队列"
    assert len(j.read_all()) == 10


def test_stop不排空则放弃剩余(tmp_path: Path) -> None:
    """异常退出路径：残留事件指的是"还没落盘"的部分，
    让它们丢掉，好过在卡住的程序里继续等日志写完。"""
    收到: list[Event] = []
    b = _总线(tmp_path, sinks=[收到.append])
    for i in range(1, 21):
        _ = b.emit(_事件(call_id=str(i)))
    b.stop(drain=False)
    assert len(收到) < 20, "drain=False 却把队列全处理了"
    assert 收到, "第一条通常已被泵线程取走，不该一条都没有"


def test_stop后发射须报错(tmp_path: Path) -> None:
    """**静默丢弃会让"关了还有东西在跑"这个问题彻底看不见**（源注释）。"""
    b = _总线(tmp_path)
    b.stop()
    with pytest.raises(RuntimeError, match="已停止"):
        _ = b.emit(_事件())


def test_stop可重复调用(tmp_path: Path) -> None:
    """`__exit__` 与显式 stop 可能都调，不能因此炸。"""
    b = _总线(tmp_path)
    b.stop()
    b.stop()


def test_上下文管理器自动停止(tmp_path: Path) -> None:
    收到: list[Event] = []
    j = JsonlJournal(tmp_path / "ev.jsonl")
    with EventBus(Redactor(), j) as b:
        b.subscribe(收到.append)
        _ = b.emit(_事件())
    assert len(收到) == 1, "退出 with 时应自动 stop 并排空"
    assert len(j.read_all()) == 1


def test_上下文管理器退出时抛异常也能排空(tmp_path: Path) -> None:
    """`__exit__` 吞不吞异常都不该影响落盘——异常路径同样要留痕。"""
    收到: list[Event] = []
    j = JsonlJournal(tmp_path / "ev.jsonl")
    with pytest.raises(ValueError, match="业务异常"), EventBus(Redactor(), j) as b:
        b.subscribe(收到.append)
        _ = b.emit(_事件())
        raise ValueError("业务异常")
    assert len(j.read_all()) == 1, "业务异常导致事件丢失"


def test_current_seq反映已处理数(tmp_path: Path) -> None:
    b = _总线(tmp_path)
    for i in range(1, 4):
        _ = b.emit(_事件(call_id=str(i)))
    _ = b.flush()
    assert b.current_seq == 3
    b.stop()


def test_新建总线seq从零开始(tmp_path: Path) -> None:
    """**这不是缺陷，是待补约束**（第 9 步续跑时须处理）。

    架构未规定续跑时 `seq` 如何恢复。当前实现总是从 0 开始，
    故续跑场景下同一份日志里会出现重复 seq。
    第 9 步实现续跑时必须处理，否则无法判断事件先后。
    """
    j = JsonlJournal(tmp_path / "ev.jsonl")
    第一个 = EventBus(Redactor(), j)
    for i in range(1, 4):
        _ = 第一个.emit(_事件(call_id=str(i)))
    第一个.stop()

    第二个 = EventBus(Redactor(), j)
    assert 第二个.current_seq == 0, "当前实现确实从 0 开始（第 9 步须处理）"
    第二个.stop()


# ═══ 夹具 ═══


def _事件(call_id: str = "", data: dict[str, object] | None = None) -> Event:
    return Event(
        seq=0,  # 刻意给 0：由泵线程改写，验证 seq 不在 emit 处分配
        kind=EventKind.PHASE_STARTED,
        run_id=RunId("r-总线测试"),
        phase=PhaseId.INTAKE,
        call_id=call_id,
        data=data if data is not None else {},
    )


def _总线(tmp_path: Path, sinks: list[Sink] | None = None) -> EventBus:
    b = EventBus(Redactor(), JsonlJournal(tmp_path / "ev.jsonl"))
    for s in sinks or []:
        b.subscribe(s)
    return b
