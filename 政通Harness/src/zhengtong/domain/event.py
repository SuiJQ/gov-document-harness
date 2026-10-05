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
    """事件类型。命名规则 `领域.动作`（过去式）。

    **本表与架构 §5.4 逐字一致**，合并裁决见 `EVENT_MERGE.md`。
    事件名是日志里的检索锚点：按架构文档 grep 若搜不到，排障时会空手而归。
    故改名字必须两边同时改，且先改本表再改文档。
    """

    # ── 运行 ──
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"
    RUN_RESUMED = "run.resumed"
    #: 收尾时的审计摘要（D-258）。
    #: 此前审计条目只写进 `audit.jsonl`，运行结束后**没有任何出口**
    #: 把「这次调用了多少工具、被拒了多少次」告诉用户——
    #: 落盘了但没人看，与只记在内存里对用户是同一件事。
    #: 界面按需订阅；调试模式据此显示（D-027「尽可能完整输出」）。
    RUN_AUDIT_SUMMARY = "run.audit_summary"
    #: 用户主动取消（TUI `Esc`，协作式）。
    #:
    #: **不是 `run.failed`**：用户中止是预期行为，不是错误。
    #: 把它算失败，会让界面弹出错误提示、让续跑把它当崩溃处理——
    #: 而实际上已完成阶段的产物与状态**原样保住了**，可以直接续跑。
    RUN_CANCELLED = "run.cancelled"

    # ── 阶段 ──
    PHASE_STARTED = "phase.started"
    PHASE_ADVANCED = "phase.advanced"
    PHASE_EXITED = "phase.exited"

    # ── 阶段成果 ──
    # 这两类叫 `phase.artifact_*` 而非 `artifact.*`：它们是**阶段成果**
    # （D-072），不是"产物"——产物落盘走 ARTIFACT_PUBLISHED / ARTIFACT_DRAFTED。
    ARTIFACT_READY = "phase.artifact_ready"
    ARTIFACT_UPDATED = "phase.artifact_updated"

    # ── 方向 ──

    # ── 角色 ──
    ROLE_STATE_CHANGED = "role.state_changed"
    ROLE_TURN = "role.turn"
    # `role.tool_call` 而非 `tool.called`：工具调用必属某角色的派发，
    # 带角色归属是必然的（D-049 责任映射）。
    ROLE_TOOL_CALL = "role.tool_call"
    ROLE_SLEEP = "role.sleep"
    ROLE_WAKE = "role.wake"
    ROLE_REPORT = "role.report"
    # 但 `tool.denied` 独立：它是**守卫拦截**（WorkspaceGuard），
    # 表达"谁尝试越权"这条安全线索，不归属任何角色的正常派发，
    # 也不该被 role.* 检索时顺带淹没。
    TOOL_DENIED = "tool.denied"

    # ── 模型 ──
    MODEL_REQUEST = "model.request"
    MODEL_RESPONSE = "model.response"
    MODEL_ERROR = "model.error"
    MODEL_RETRY = "model.retry"

    # ── 检索 ──
    RETRIEVAL_DONE = "retrieval.done"

    # ── 门禁 ──
    GATE_STARTED = "gate.started"
    GATE_PASSED = "gate.passed"
    GATE_FAILED = "gate.failed"
    GATE_REJECTED = "gate.rejected"
    GATE_REWORK = "gate.rework"
    GATE_ESCALATED = "gate.escalated"

    # ── 上下文 ──
    CONTEXT_COMPOSED = "context.composed"
    CONTEXT_COMPACTED = "context.compacted"

    # ── 提问 ──
    USER_QUESTION_RAISED = "user.question_raised"
    USER_ANSWERED = "user.answered"

    # ── 产物 ──
    ARTIFACT_PUBLISHED = "artifact.published"
    ARTIFACT_DRAFTED = "artifact.drafted"

    # ── 导出 ──
    EXPORT_BLOCKED = "export.blocked"
    EXPORT_DONE = "export.done"

    # ── 错误 ──
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

    `data` 里**只放标量与小结构**，唯一例外是 `ARTIFACT_READY`——
    它按 D-072 携**成果全文**（不是摘要），因为标准模式的成果卡
    直接由它渲染（D-030/D-046）。其余事件都不许放全文。

    理由是结果直传（D-025）：让界面拿到的与落盘的是同一份内容，
    从根上消除"界面显示的和落盘的不是一份"这类最难查的错。
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
        """标准模式是否显示。由 `USER_VISIBLE_KINDS` 白名单单独决定。"""
        return is_user_visible(self.kind)


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
        EventKind.ROLE_TURN,
        EventKind.ROLE_TOOL_CALL,
        EventKind.ROLE_SLEEP,
        EventKind.ROLE_WAKE,
        EventKind.ROLE_REPORT,
        EventKind.TOOL_DENIED,
        EventKind.RETRIEVAL_DONE,
        EventKind.CONTEXT_COMPOSED,
        EventKind.CONTEXT_COMPACTED,
        # `MODEL_RESPONSE` **不在这里**（D-277）：它进了 `USER_VISIBLE_KINDS`，
        # 因为它承载真实 token 用量——那是「用户有权知道花了多少」，
        # 不是「模型间交互」。两张表必须互斥，故此处不能同时列它。
        EventKind.MODEL_REQUEST,
        EventKind.MODEL_ERROR,
        EventKind.MODEL_RETRY,
        #: 审计摘要进**调试**而非标准（D-258）：它含「被拒了多少次越权尝试」，
        #: 那是审核/安全信息。按 D-046「标准模式不展示审核意见」，
        #: 放进 `USER_VISIBLE_KINDS` 会直接违反用户定的展示边界。
        EventKind.RUN_AUDIT_SUMMARY,
    }
)

#: **标准模式可见的事件类别**（架构 §5.4，对齐 D-023 / D-046 / D-030）。
#:
#: 原注释写「唯一可见的五类」，早已过时：之后按用户要求先后加入过
#: `run.started`（告知扫到几个用户文件）、`model.response`（token 账单，
#: D-277）、`run.cancelled`（用户按 Esc 要有回音）以及
#: `phase.artifact_updated`（重写只显示最新版）。数量以本集合为准。
#:
#: 合并裁决（`EVENT_MERGE.md`）：`export.blocked` 与 `export.done`
#: 属同一族，分开计为两项。故表内 5 项、语义上仍是 D-023 说的"四件事"：
#: 三份阶段成果、导出结果、运行失败、需要用户答。
#:
#: 白名单**不是**整个 `phase.*`——进度类事件在标准界面上
#: 只用来渲染"总体进度"那一行（D-046），不需要单独成卡；
#: 真正要给用户"看内容"的只有三份阶段成果，由 ARTIFACT_READY 承载全文。
#:
#: 这张表是 D-046「不显示成员状态、不展示审核意见」的机械落点：
#: 成员状态与门禁明细都不在其中，故它们进不了标准界面。
#:
#: 注意 `RUN_FINISHED` 不在其中：完成由界面自行收束，
#: 不额外弹事件卡片，避免和成果卡抢注意力。
#: 标准模式可见的事件类别（D-023/D-046）。
#:
#: 用户明确要求（新增 `RUN_STARTED`）：
#: > 「界面上能不能告诉用户扫描到了几个用户给 AI 的文件，
#: >  相当于告诉用户它识别到的，而不是不知道。」
#:
#: **这不违背 D-023。** D-023 限制的是"内部过程噪声"
#: （成员状态、审核意见、模型间交互），而"程序读到了哪些用户文件"
#: 是**系统对用户输入的确认**——用户有权知道。
#:
#: 用户明确要求（新增 `MODEL_RESPONSE`，D-277）：
#: > 「标准模式也显示 Token」
#:
#: **同样不违背 D-023。** 模型间交互指「谁问了什么、谁答了什么」，
#: 那是要藏的；而「这一稿花了多少 token」是**账单**——
#: 架构 §686 明写「界面展示用量 → API 返回的真实值」。
#:
#: **边界**：进白名单的是**这一个 kind**，且只带数字与 `finish_reason`，
#: **不带模型正文**。正文进了事件就是「模型间交互」，D-046 会失效。
USER_VISIBLE_KINDS: Final[frozenset[EventKind]] = frozenset(
    {
        EventKind.RUN_STARTED,
        EventKind.ARTIFACT_READY,
        # `ARTIFACT_UPDATED` 必须可见（用户裁决：门禁打回重写后
        # **只显示第 2 版**）。
        #
        # 它进白名单**不违背 D-046**——那张表禁的是「模型间交互」
        # 与「审核意见」，而重写后的正文**就是那份阶段成果本身**，
        # 是用户要拿的东西。放在 INTERNAL 里的话，投影第 179 行
        # `if not is_user_visible(...): return` 会把它直接丢掉，
        # 界面于是停在**第一版**——恰恰是用户明确不要的那个结果。
        EventKind.ARTIFACT_UPDATED,
        EventKind.EXPORT_BLOCKED,
        EventKind.EXPORT_DONE,
        EventKind.RUN_FAILED,
        EventKind.USER_QUESTION_RAISED,
        EventKind.MODEL_RESPONSE,
        # 用户按 Esc 是**用户自己的动作**，界面必须给回音——
        # 屏幕毫无变化会让用户以为程序死了，于是再按几次。
        # 它进标准界面不违反 D-046：那张表禁的是「模型间交互与审核意见」，
        # 用户自己的动作不在其列。
        EventKind.RUN_CANCELLED,
    }
)


def is_user_visible(kind: EventKind) -> bool:
    """标准模式是否显示该事件。

    **只看 `kind`，不看 `audience`**：白名单是唯一判据。
    若两者都看，就得同时保证"事件被标成 USER"且"在白名单里"——
    两个条件任一漏掉都会让该显示的东西消失，而这种 bug 只在
    真实运行时才看得见。故让 `audience` 不参与这个判断，
    白名单说了算（D-046）。
    """
    return kind in USER_VISIBLE_KINDS
