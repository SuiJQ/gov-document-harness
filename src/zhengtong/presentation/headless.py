"""无界面模式：直接跑，投影写到标准输出（D-037）。

## 与 TUI 的差别只有两处

1. 谁调 `next()` —— 这里在主线程直接跑，TUI 在 worker 线程
2. 显示写到哪 —— 这里写标准输出，TUI 写 Textual 屏

**其余完全一致**：同一个 `编排器`、同一个 `drive`、同一个 `StandardProjection`。

这正是架构 §8.1 说的「共用同一套编排内核，只换交互适配层」。
所以本模块刻意**很薄**——它薄才说明同源是真的；
一旦它开始解释事件、重算进度，同源就废了。
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass

from ..domain.event import Event
from ..kernel.projection import DebugProjection, Projection, StandardProjection


@dataclass(frozen=True, slots=True)
class 无界面结果:
    """一次无界面运行的显示结果。

    `最终显示` 存的是**投影自己算出来的**那几行，
    不是驱动另行拼的——判据靠它比对「三个驱动是否同源」。

    ## `失败` 为什么是字段而不是让调用方去猜（D-283）

    我第一版在 CLI 里写的是：

    ```python
    return 0 if not 显示.最终显示.endswith("运行失败：") else 1
    ```

    **实测这是错的**：真实失败时末行是
    「运行失败：<详细原因>」，`endswith("运行失败：")` 判 False，
    于是**运行失败却返回退出码 0**。
    脚本调用会以为跑成功了，白拿一份没有成文的文档——
    这比崩掉更糟，因为它不报错。

    故成败做成**显式字段**：驱动知道结果，就该把结果交出去，
    不该让调用方去 `endswith` 猜。
    """

    最终显示: str
    投影类: type[Projection]
    #: 空串表示成功。非空是失败原因。
    失败: str = ""

    @property
    def 成功(self) -> bool:
        return not self.失败

    def 打印(self, *, 流=None) -> None:
        目标 = 流 or sys.stdout
        目标.write(self.最终显示 + "\n")


class 无界面驱动:
    """喂事件 → 取显示。**没有编排逻辑**，只有搬运。"""

    def __init__(self, *, 调试: bool = False) -> None:
        #: 标准模式固定用 `StandardProjection`（用户约束：默认极简）。
        #: 调试模式换 `DebugProjection`，但**换成哪个类由调用方给**，
        #: 不在这里写 `if 调试: 显示更全` 的第二套逻辑——
        #: 那会让「调试版」变成一条独立分支，与标准版分叉。
        self._调试 = 调试

    @property
    def 投影类(self) -> type[Projection]:
        return DebugProjection if self._调试 else StandardProjection

    def 跑(self, 事件们: Iterable[Event]) -> 无界面结果:
        """喂完事件流，取最终显示。"""
        投影 = self.投影类()
        for 事件 in 事件们:
            投影.accept(事件)
        return 无界面结果(最终显示="\n".join(投影.lines()), 投影类=self.投影类)

    def 跑完整流程(self, 编排器: object, *, 档位: object, 需求: str) -> 无界面结果:
        """从编排器跑到底并显示。

        **复用编排器的 `跑到终`**——无界面模式自己实现一遍编排
        就等于放弃 D-037。参数名与编排器签名对齐，
        用 `object` 是为了不让接入层 import 编排层
        （依赖方向：接入层在编排层之上，但不该绑死它的类型）。
        """
        事件们: list[Event] = []
        结果 = 编排器.跑到终(档位=档位, 需求=需求)  # pyright: ignore[reportAttributeAccessIssue]
        事件们.extend(getattr(编排器, "事件", []))
        显示 = self.跑(事件们)
        if 结果.is_error:
            # **失败原因同时进显示与 `失败` 字段**（D-283）：
            # 给人看的在显示里，给调用方（退出码、脚本）在字段里。
            原因 = 结果.error.detail
            显示 = 无界面结果(
                最终显示=f"{显示.最终显示}\n运行失败：{原因}",
                投影类=显示.投影类,
                失败=原因,
            )
        return 显示
