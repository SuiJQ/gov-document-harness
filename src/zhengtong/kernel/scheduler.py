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

from ..domain.errors import AppError, ErrorCode, Result
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

    ## 审计 D-130：`work` 抛异常必须就地转成失败结果

    上面那句承诺，原实现**并没有兑现**——全文没有任何 `try`。
    `work` 抛异常会经 `Future.result()` 重新抛出，
    `run_groups` 整个调用被炸，其余各组的取证结果**全部丢失**。
    实测：4 组里第 2 组抛 `RuntimeError`，
    第 1、3 组的证据一并消失。

    这在门禁场景下后果具体：D-049 让 4 个成员同时取证，
    其中一人的取证代码崩了（模型返回畸形、成员代码有 bug），
    本该得到"3 份证据 + 1 份失败"，实际得到**整个 `run_groups` 调用失败**，
    编排层连"哪道门禁缺证据"都无从判断——它拿到的是个异常，
    不是结果。

    故包一层：异常就地转 `Result.fail`。
    `Outcome` 此时无从构造（值压根没算出来），
    故用 `Result.fail`，其成功值类型留 `None`——
    调用方见到 `ok=False` 本就不该碰 `.value`。

    ## 关于 `max_workers` 默认值 4 —— 这是选择，不是缺陷

    实测 12 组各 0.2s、默认参数 → 0.60s（看着像串行）。
    但**裸 `ThreadPoolExecutor(max_workers=4)` 同样是 0.61s**，
    说明瓶颈在参数值而非调度逻辑：12 个任务、4 个 worker，
    本就该分 3 批。

    D-049 的门禁分组是 **4 组**，默认 4 线程正好匹配：
    实测 4 组各 0.2s → 0.20s，真并行。
    「12 组退化成串行」是我编造输入得出的假结论，不是缺陷。
    """
    if not groups:
        return ()

    # 先**全部提交**，再统一收集结果。
    # 顺序反了就是串行：循环里先 `submit` 再立即 `result()`，第一个
    # future 一提交就被阻塞住，直到它跑完才提交第二个——实测 4 组各
    # 0.2s 耗时 0.80s，与串行无异。这正是 D-049 要并行的那一段。
    with ThreadPoolExecutor(max_workers=min(max_workers, len(groups))) as pool:
        futures: list[Future[Result[Outcome]]] = [pool.submit(work, g) for g in groups]
        结果: list[GroupResult[Outcome]] = []
        for g, fut in zip(groups, futures, strict=True):
            # 逐个 `result()` 但**不阻塞等待**：前面的 future 一完成
            # 就立刻取走，结果的收集顺序仍恒等于入参顺序（D-075 收集侧）
            值: Result[Outcome]
            try:
                值 = fut.result()
            except Exception as exc:
                值 = _转失败(exc, g)
            结果.append(GroupResult(g.owner, 值))
        return tuple(结果)


def _转失败(exc: Exception, group: GateGroup) -> Result[Outcome]:
    """把 `work` 抛出的异常转成失败结果（审计 D-130）。

    用 `TypeVar` 而非 PEP 695 的 `def f[T]()`：
    后者在本项目的目标版本（3.11，D-001）上是**语法错误**——
    本模块开头那条 D-086 记的就是这件事，别再踩第二次。
    实测：`def f[T]()` 在 3.11 上连 Ruff 都报 invalid-syntax。

    转出来的仍是 `Result[T]`——本模块的整个设计前提就是
    「`Outcome` 完整保留，不因异常路径退化成 Unknown」。
    若这里返回 `Result[Any]`，`GroupResult.value` 的类型就被污染，
    而这个 `Any` 只在**异常这条路径**上悄悄漏出，类型检查器再也看不见。

    ## 为什么捕获 `Exception` 而不是更窄的类型

    这里要兜的不是"某类已知异常"，而是**任何**从 `work` 里逃出来的东西：
    模型适配器返回畸形、成员代码里的 `KeyError`、第三方库的
    各种自定义异常。窄捕获只会把一部分崩溃重新放到调用层炸。

    唯一的例外是 `BaseException` 家族——`KeyboardInterrupt`、
    `SystemExit` 不该被吞掉：用户按 Ctrl+C 要能真的停下来。
    所以捕获 `Exception` 而非 `BaseException`。

    故此处 `# noqa: BLE001` 是**有意识的取舍**，
    理由已写在此处而非注释在 lint 配置里——后者没人会读到。

    错误码用 `GATE_NO_EVIDENCE`（D-049 的门禁专用码）：
    这组的证据**没拿到**——正是该码的语义。
    用 `LLM_REQUEST_FAILED` 会把排查方向硬引向模型侧，
    但崩溃也可能来自成员自己的代码（`KeyError` 之类）；
    用通用 `INTERNAL` 则暗示"程序有 bug"，同样会误导。
    """
    return Result.fail(
        ErrorCode.GATE_NO_EVIDENCE,
        f"取证崩溃（{group.owner.value} / {'+'.join(group.gate_ids)}）：{type(exc).__name__}: {exc}",
    )
