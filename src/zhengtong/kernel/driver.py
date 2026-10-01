"""`drive()` 驱动循环（D-023）。

**为什么一个循环要管三种形态**：架构要求 TUI、无界面模式、测试三者
共用同一个 `drive()`。理由是它们必须**看到完全相同的事件序列**——
若 TUI 走一套逻辑、无界面走另一套，两者行为迟早分叉，
届时"调试模式复现不了"就无从谈起了。

故本模块**不碰界面**：它只负责

1. 启动：发 `run.started`，把 `resume_from` 喂进去（断点续跑，D-027）
2. 推进：反复从 `step()` 取一步，发 `phase.*` 事件
3. 收尾：成功发 `run.finished`，失败发 `run.failed`
4. **保证停得干净**：总线必须 flush 完才退出

**第 4 点是最容易被忽略、后果最严重的一条**。`drive()` 返回时若总线里
还有未处理的事件，它们会随进程退出一起消失——用户看到"导出被拦截"没弹，
日志里也没有。这一步不做，前面所有脱敏与落盘都白搭。
"""

# 放宽 Unknown 类规则，理由同 domain/errors.py 的 D-085：
# `Result.ok(...)` 的返回类型是 `Result[Any]`（三方案实测后确认这是唯一可行解），
# 故 `DriveOutcome` 那一处赋值类型为 Unknown。只影响静态提示，不影响运行行为。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar, cast

from ..domain.errors import ErrorCode, Result
from ..domain.event import Event, EventKind, Level
from ..domain.ids import RunId
from ..domain.phase import PhaseId
from .bus import EventBus

#: 驱动流程中流转的状态类型（由调用方决定，通常是 `RunState`）。
StateT = TypeVar("StateT")
#: `Step.state` 携带的状态类型。**通常与 `StateT` 相同**，
#: 故默认实现会原样回传；分开声明是因为 `Step` 也要能独立用于
#: "只产出下一步、不改变状态"的场景（如仅推进阶段）。
StepT = TypeVar("StepT")


@dataclass(frozen=True, slots=True)
class Step(Generic[StepT]):
    """驱动的一步。

    `phase` / `state` 用于发 `phase.started` / `phase.advanced` 事件；
    `done=True` 表示本次运行到此结束（不再继续取步）。
    """

    phase: PhaseId
    state: object = None
    done: bool = False
    data: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DriveOutcome:
    """一次 `drive()` 的终态。**调用方据此判断成败，不靠异常。**"""

    run_id: RunId
    ok: bool
    detail: str = ""
    steps: int = 0


def drive(
    *,
    run_id: RunId,
    bus: EventBus,
    initial_state: StateT,
    step: Callable[[StateT], Result[Step[StateT]]],
    resume_from: int = 0,
    max_steps: int = 10_000,
) -> Result[DriveOutcome]:
    """驱动到结束。

    `step` 收一个状态、返回一个 `Result[Step]`。它**不得抛异常**——
    要用 `Result.fail` 返回；真抛了说明实现有错，本层不兜。

    ## 审计 D-137：step 抛异常时**先留痕再抛**

    原实现对 `step(state)` 不设防，异常直接穿透 `drive()`。
    实测后果（journal 落盘验证）：

        走 Result.fail → ['run.started', 'run.failed']   ← 留痕完整
        走 raise       → ['run.started']                   ← 原因消失

    而本模块开头第 4 点写的正是「**保证停得干净**：
    总线必须 flush 完才退出」，理由是「用户看到『导出被拦截』没弹，
    **日志里也没有**」——而异常穿透时正是日志里什么都没有。
    **注释担心的那个场景，正在它自己守护的这行代码里发生。**

    故此处 `try/except`：发 `run.failed` 并 flush 后**原样重抛**。
    既不吞异常（栈照常抛到调用方，调试时能看到确切位置），
    又不留死角（用户排查时能看到失败原因）。

    **不重抛才是错的**：吞掉异常会让编排层的 bug 变成
    「程序莫名其妙不写了」，那是比崩溃更难查的问题。

    `max_steps` 是**死循环保险丝**。编排层一旦写成"总是返回同一步"，
    没有上限就会挂死用户界面；有了它至少能在有界时间内退出并留痕。
    默认 10000 步对真实任务绰绰有余（一个公文流程不超过几十步）。

    `resume_from` 是断点续跑起点（D-027）：由调用方从持久化的
    `RunState.last_seq` 算出，`drive()` 自己不读盘——
    读盘是 L2 的事，内核不碰文件系统。

    用 `TypeVar` 而非 PEP 695 的 `def drive[T]()`：3.11 编译不过（D-086）。
    """
    bus.emit(
        Event(
            seq=0,
            kind=EventKind.RUN_STARTED,
            run_id=run_id,
            phase=PhaseId.INTAKE,
            data={"resume_from": resume_from} if resume_from else {},
        )
    )

    state = initial_state
    count = 0

    while True:
        if count >= max_steps:
            detail = f"达到步数上限 {max_steps}，疑似死循环"
            _emit_failed(bus, run_id, detail)
            return Result.fail(ErrorCode.KERNEL_STALLED, detail)

        try:
            got = step(state)
        except Exception as 错:
            # 审计 D-137：见上文。这里刻意用 Exception 而非具体类型——
            # `step` 是任意用户回调，能抛出的类型不可穷举，
            # 而 `BaseException` 里的 KeyboardInterrupt/SystemExit
            # 属于「用户主动中断」，不该被包装成 run.failed。
            _emit_failed(bus, run_id, f"编排层抛出未处理异常：{错!r}")
            raise
        if got.is_error:
            err = got.error
            assert err is not None  # is_error 为 True 时 error 必非 None
            # detail 进事件前会经过 Redactor；此处**不做第二次脱敏**，
            # 否则会把证据洗掉、且掩盖"总线脱敏被绕过"这类问题。
            _emit_failed(bus, run_id, err.detail or err.code.value)
            # `fail` 收 code+detail 而非 AppError，故拆开传。
            return Result.fail(err.code, err.detail)

        # 用 `unwrap()` 而非 `got.value`：`value` 标为 `T | None`，
        # 直接取会因可空而报错。`unwrap()` 在失败时抛 RuntimeError——
        # 这里已判过 `is_error`，故不会走到那条分支。
        one = got.unwrap()
        count += 1
        if one.done:
            bus.emit(
                Event(
                    seq=0,
                    kind=EventKind.RUN_FINISHED,
                    run_id=run_id,
                    phase=one.phase,
                    data={"steps": count},
                )
            )
            _ = bus.flush()
            return Result.ok(DriveOutcome(run_id=run_id, ok=True, steps=count))

        # `one.state` 是 StepT、`state` 是 StateT，两个 TypeVar 不同。
        # 显式 cast 而非 ignore：驱动循环的前提就是"step 回传的状态
        # 类型与传入的一致"，真不一致会在下一轮 `step(state)` 调用时
        # 报 ArgumentTypeError，位置清晰，不必在这里放宽类型检查。
        state = cast("StateT", one.state)
        bus.emit(
            Event(
                seq=0,
                kind=EventKind.PHASE_ADVANCED,
                run_id=run_id,
                phase=one.phase,
                data=one.data,
            )
        )


def _emit_failed(bus: EventBus, run_id: RunId, detail: str) -> None:
    """发 `run.failed` 并**等总线排空**。

    这是 D-023 白名单里四类事之一（运行失败），必须真的发出去——
    `EVENT_MERGE.md` 记着这个事件此前从未被发射过，
    若这里图省事不 flush，用户就看不到失败原因。
    """
    bus.emit(
        Event(
            seq=0,
            kind=EventKind.RUN_FAILED,
            run_id=run_id,
            phase=PhaseId.INTAKE,
            level=Level.ERROR,
            data={"detail": detail},
        )
    )
    _ = bus.flush()
