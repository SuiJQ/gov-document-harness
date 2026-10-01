"""并行门禁调度（D-049，D-064，D-075）。

**并行的是取证，不是判定**：D-049 把 12 道门禁分成 4 组，
组内由不同成员同时取证，组间并行推进。但：

- **门禁判定本身是串行的**——它依赖一个共享的 `RunState`，
  并发改它必然产生竞态。所以本模块只并行"跑一组的取证工作"，
  判定由调用方在**收集完所有组的结果之后**统一做。
- **事件顺序仍与状态机推进顺序一致**——因为 `seq` 由泵线程分配（D-075），
  本模块不管谁先谁后，投递顺序由队列决定。

**为什么不用 `asyncio`**：门禁调用是阻塞的模型请求，套一层 asyncio
只会让"在同步代码里 await 一个阻塞调用"这件事变得更绕，
还得配线程池 executor，等于绕回 `ThreadPoolExecutor`。
直接用线程池更直白。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Generic, TypeVar

from ..domain.errors import AppError, Result
from ..domain.role import RoleId

T = TypeVar("T")
Outcome = TypeVar("Outcome")


@dataclass(frozen=True, slots=True)
class GateGroup:
    """一个并行取证组（D-049）。

    `gate_ids` 里的门禁**全部由 `owner` 一人取证**——
    D-049 的分组就是按"谁负责这些门禁"划的。
    """

    owner: RoleId
    gate_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GroupResult(Generic[T]):
    """一组的取证结果。

    刻意**保留 owner 与 failures**，不只留成功值：
    编排层要靠它决定"哪道门禁由谁退回"，而失败原因得原样带回，
    不能在这里压成一句话。
    """

    owner: RoleId
    value: Result[T]

    @property
    def ok(self) -> bool:
        return self.value.is_ok

    @property
    def error(self) -> AppError | None:
        return self.value.error


def run_groups(
    groups: tuple[GateGroup, ...],
    work: Callable[[GateGroup], Result[Outcome]],
    max_workers: int = 4,
) -> tuple[GroupResult[Outcome], ...]:
    """并行执行各组取证。

    用 `TypeVar` 而非 PEP 695 的 `def f[T]()`：3.11 编译不过（D-086，实测）。

    **`work` 收 Result 而不是裸值**：这是本模块唯一的关键设计。
    一组内部的崩溃如果以异常形式穿出 `Future.result()`，就得在收集处
    `try/except` 兜住，而兜住之后又拿不到 `Outcome` 的类型信息——
    类型被降级成 Unknown，整个 `GroupResult` 的值类型随之塌掉。
    改成让 `work` 自己返回 `Result`，异常在组内就地转成失败结果，
    收集处就只剩纯粹的类型安全的取值，`Outcome` 完整保留。

    **返回顺序恒等于入参顺序**，与线程完成顺序无关——
    编排层据此做确定性归并，不受调度抖动影响。这是"事件顺序与
    状态机推进顺序一致"（D-075）在结果收集侧的对应保证。

    单组失败不炸整个调用：失败作为该组的 `Result` 原样带回。
    理由是门禁组之间本就独立，一组崩了不该让另外三组的证据一起丢掉。
    """
    if not groups:
        return ()

    # 先**全部提交**，再统一收集结果。
    # 顺序反了就是串行：循环里先 `submit` 再立即 `result()`，第一个
    # future 一提交就被阻塞住，直到它跑完才提交第二个——实测 4 组各
    # 0.2s 耗时 0.80s，与串行无异。这正是 D-049 要并行的那一段。
    with ThreadPoolExecutor(max_workers=min(max_workers, len(groups))) as pool:
        futures: list[Future[Result[Outcome]]] = [pool.submit(work, g) for g in groups]
        return tuple(
            GroupResult(g.owner, fut.result()) for g, fut in zip(groups, futures, strict=True)
        )
