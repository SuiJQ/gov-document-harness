"""全量可持久化运行状态。

**这是断点续跑的唯一真相来源**（D-064）。
判据：进程被杀后，只靠这个对象 + `data/` 下的产物，就能原样续跑。

三条纪律：

**一、只放值对象。** 不放文件句柄、不放模型客户端、不放 callable。
跨层只传值对象（D-070），这条是它在本层的体现。

**二、`schema_version` 必须随字段变更递增，且提供迁移链。**
改动字段后静默用旧状态续跑，比续跑失败更糟——
它会拿缺失字段的默认值继续，把一份残缺的状态当成完整的跑下去。

**三、进度只追加，不回写历史。** `completed_call_ids` 记录
`call_id` 而非"第几步"，因为 D-076 的 `call_id` 才是跨重启稳定的身份。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .artifact import ArtifactRef
from .dcard import DCard, empty_dcard
from .errors import ErrorCode
from .event import Event
from .ids import RunId
from .phase import PhaseId, PhaseState
from .role import RoleId, RoleState
from .task import TaskSpec


@dataclass(frozen=True, slots=True)
class GateRecord:
    """一道门禁的当前结论。

    `attempt` 记返工次数，D-050 上限 2 次；
    超限不再是"再试一次"，而是转人工。
    """

    gate: str
    passed: bool = False
    attempt: int = 0
    evidence: tuple[str, ...] = ()
    rejection_reason: str = ""

    @property
    def rejected(self) -> bool:
        return self.rejection_reason != ""


@dataclass(frozen=True, slots=True)
class RunState:
    """一次运行的全部状态。

    全字段有默认值：能从一份**空**状态构造出合法初值，
    这样"新开一次运行"不需要特殊分支。
    """

    schema_version: int
    run_id: RunId
    task: TaskSpec
    phase: PhaseId = PhaseId.INTAKE
    phase_state: PhaseState = PhaseState.PENDING
    dcard: DCard = field(default_factory=empty_dcard)
    role_states: dict[RoleId, RoleState] = field(default_factory=dict)
    artifacts: dict[str, ArtifactRef] = field(default_factory=dict)
    gates: dict[str, GateRecord] = field(default_factory=dict)
    completed_call_ids: tuple[str, ...] = ()
    last_seq: int = 0  # 已发射事件的最后序号
    last_error: str = ""

    def is_call_completed(self, call_id: str) -> bool:
        """该副作用是否已完成——断点续跑的判定入口（D-065 幂等重放）。

        用 `call_id` 而非位置序号：重试与续跑都必须认得出"同一件事"。
        """
        return call_id in self.completed_call_ids

    def mark_completed(self, call_id: str) -> RunState:
        """记下一个完成的副作用，返回**新**状态。"""
        if call_id in self.completed_call_ids:
            return self
        return replace(self, completed_call_ids=(*self.completed_call_ids, call_id))

    def with_phase(self, phase: PhaseId, state: PhaseState) -> RunState:
        """推进阶段，返回**新**状态。"""
        return replace(self, phase=phase, phase_state=state)

    def set_role_state(self, role: RoleId, state: RoleState) -> RunState:
        return replace(self, role_states={**self.role_states, role: state})

    def put_artifact(self, ref: ArtifactRef) -> RunState:
        return replace(self, artifacts={**self.artifacts, str(ref.id): ref})

    def put_gate(self, record: GateRecord) -> RunState:
        return replace(self, gates={**self.gates, record.gate: record})

    def with_error(self, code: ErrorCode, detail: str = "") -> RunState:
        return replace(self, last_error=f"[{code}] {detail}".rstrip())

    def with_dcard(self, dcard: DCard) -> RunState:
        """更新 D-Card，返回新状态。"""
        return replace(self, dcard=dcard)

    def active_roles(self) -> tuple[RoleId, ...]:
        """本档编制内的角色（D-029）。"""
        return self.task.tier_spec.roster


def new_run_state(run_id: RunId, task: TaskSpec) -> RunState:
    """构造一次新运行的初值状态。

    角色状态初始化为 `IDLE`——只有本档编制内的角色才有状态，
    不编入的成员连状态都不存在（D-029 配平铁律的一部分）。
    """
    from . import SCHEMA_VERSION

    return RunState(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        task=task,
        role_states=dict.fromkeys(task.tier_spec.roster, RoleState.IDLE),
    )


__all__ = [
    "Event",
    "GateRecord",
    "RunState",
    "new_run_state",
]
