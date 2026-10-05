"""时钟端口。

看着像多此一举，实则是断点续跑与重试逻辑的**可测性**前提：

- 重试递增间隔（D-027）要测"间隔确实递增"，真 sleep 会让测试慢到不可接受
- 运行标识 `run-YYYYMMDD-HHMMSS` 要可复现
- 门禁耗时统计在测试里必须是确定的

所以时间**必须**经由端口注入。直接调 `time.time()` 的地方，
在测试里就只能靠真等——久而久之就没人为这些逻辑写测试了。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ClockPort(Protocol):
    def now(self) -> float:
        """当前时间戳（秒）。"""
        ...

    def sleep(self, seconds: float) -> None:
        """等待。测试实现里应当**不真等**，只记账。"""
        ...

    def stamp(self) -> str:
        """`20261001-143000` 形式的时间戳，用于运行标识。"""
        ...
