"""领域事件。

三处必须记牢，都是踩过坑定下来的：

**一、`Event.seq` 与 `Event.call_id` 不是一回事，不得混用。**

- `seq`：事件**发生顺序**，由 EventBus 单点递增分配。
  它回答"第几个发生的事件"。
- `call_id`：一次**副作用的语义身份**，由 D-076 定义
  （`sha256(kind|run_id|phase_id|discriminators)`），
  **与角色、轮次、位置序号无关**。它回答"这是不是同一次副作用"。

  混用会直接毁掉断点续跑：若把轮次编进 call_id，
  续跑时轮次变了，同一件事就被当成新事重做一遍。

**二、事件经队列由泵线程单点发射（D-075）。**
不在产生事件的地方直接分发，否则并行门禁（4 组线程）
的事件顺序会随线程调度抖动，与状态机推进顺序不一致。

**三、`Audience` 决定标准模式能否"安静"。**
标准模式只显示总体进度一行，不显示成员状态（D-046）——
靠 `Audience.USER` 与 `Audience.DEBUG` 分离：标准模式只消费前者。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from .ids import RunId


class EventKind(StrEnum):
    """事件类型。命名规则 `领域.动作`（过去式）。"""

    RUN_STARTED = "run.started"
    RUN_RESUMED = "run.resumed"
    RUN_FINISHED = "run.finished"

    PHASE_STARTED = "phase.started"
    PHASE_ADVANCED = "phase.advanced"

    ROLE_STATE_CHANGED = "role.state_changed"

    ARTIFACT_READY = "artifact.ready"
    ARTIFACT_UPDATED = "artifact.updated"

    DIRECTION_CONFIRMED = "direction.confirmed"
    USER_QUESTION_RAISED = "user.question_raised"

    GATE_STARTED = "gate.started"
    GATE_PASSED = "gate.passed"
    GATE_FAILED = "gate.failed"
    GATE_REJECTED = "gate.rejected"

    RETRIEVAL_DONE = "retrieval.done"
    TOOL_CALLED = "tool.called"
    TOOL_DENIED = "tool.denied"

    ERROR_RAISED = "error.raised"


class Audience(StrEnum):
    """事件受众。**投影层据此决定要不要显示。**"""

    USER = "user"  # 标准模式可见：进度、阶段成果、最终成果
    INTERNAL = "internal"  # 仅状态机消费，不显示
    DEBUG = "debug"  # 仅调试模式可见：成员交互、审核意见、成员状态（D-046 禁止）


class Level(StrEnum):
    """事件级别。"""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Span:
    """来源信息，便于从一条事件回溯到具体调用。

    `internal_role` 是**英文内部标识**——即便在调试模式也可用，
    因为调试模式是为定位文件存在的（D-080 明确允许）。
    """

    role: str | None = None  # 内部标识
    gate: str | None = None
    round_no: int | None = None


@dataclass(frozen=True, slots=True)
class Event:
    """一条不可变领域事件。

    `data` 用 `object` 而非 `Any` 标值：载荷确实异构，
    但 `Any` 会把下游所有类型检查一并放行，等于没有标注。
    `object` 逼着消费方先判断类型再用——这正是我们要的。

    `data` 里**只放标量与小结构**，不放正文全文——
    `phase.artifact_ready` 按架构要求载全文时，
    那是特例，且应走 `artifact.ready` 事件而非塞进 progress 事件。
    """

    seq: int
    kind: EventKind
    run_id: RunId
    phase: str
    level: Level = Level.INFO
    audience: Audience = Audience.USER
    call_id: str = ""
    span: Span | None = None
    data: dict[str, object] = field(default_factory=dict)

    @property
    def is_user_visible(self) -> bool:
        """标准模式是否显示。**标准模式绝不显示 DEBUG 类。**"""
        return self.audience is not Audience.DEBUG


def make_call_id(kind: str, run_id: str, phase_id: str, **discriminators: str) -> str:
    """生成副作用的语义标识（D-076）。

    刻意**不接收**角色、轮次、序号——那些是"第几次发生"，
    不是"哪一件事"。重试与续跑要能认出同一件事，靠的就是这里。

    `discriminators` 用于区分同类动作的不同实例，
    例如 `gate="G2"` 区分不同门禁；**同名参数即不同事件**。
    """
    parts = [kind, run_id, phase_id, *(f"{k}={v}" for k, v in sorted(discriminators.items()))]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


#: 调试可见的事件类别（成员状态、审核意见等）。
DEBUG_ONLY_KINDS: Final[frozenset[EventKind]] = frozenset(
    {
        EventKind.ROLE_STATE_CHANGED,
        EventKind.TOOL_CALLED,
        EventKind.TOOL_DENIED,
        EventKind.RETRIEVAL_DONE,
    }
)
