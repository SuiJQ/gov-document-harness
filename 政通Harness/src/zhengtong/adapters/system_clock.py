"""系统时钟适配器（`adapters/system_clock.py`）。

## 为什么单独一个文件

`ClockPort` 是端口（第 2 步），此前**只有测试用的假时钟**。
真运行需要真时钟，而它必须实现端口——否则 `HttpLlm` 的重试
要么没有时钟、要么直接依赖 `time.sleep`，测试就没法验递增间隔。

## 唯一允许直接调 `time` 的地方

全项目**只有本文件**碰 `time`。
这不只是整洁问题：重试递增间隔（D-027）要测「间隔确实递增」，
若某处直接 `time.sleep(2**n)`，测试只能靠真等或 mock 全局函数——
前者让测试慢到不可接受，后者改的是全局状态。

## stamp 的格式

`20261002-143000`。**本地时区**——运行标识是给人看的，
用户看到自己所在时区的时间才有意义（D-012：界面中文、日期本地）。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import final


@final
class SystemClock:
    """真时钟。`ClockPort` 的生产实现。"""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        # 负数或非数会抛 `ValueError`——那是调用方的 bug，不该静默吞掉
        time.sleep(seconds)

    def stamp(self) -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S")
