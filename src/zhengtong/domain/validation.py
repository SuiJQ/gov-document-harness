"""L0 行为层：校验与默认值。

**为什么形状和行为要分两段做**（架构 §15）：
端口签名大量使用领域类型，不先有类型就没什么可签；
但"字段齐了"和"字段对"是两回事——比如 D-Card 的必填字段
可以为空，但**标成 `USER` 来源的空字段是不允许的**，
这条规则得等有了行为才表达得出来。

因此本模块只做**纯校验**：输入领域对象，返回 `Result`。
不改状态、不碰文件、不发事件——那些属于 L1 及以上。
"""

from __future__ import annotations

from .dcard import ALLOWED_INFERRED_FIELDS, DCard, Provenance
from .errors import ErrorCode, Result
from .gate import FORMAL_GATES, GATE_GROUPS, GATES, GateId
from .role import RoleId
from .run_state import RunState
from .task import TaskSpec
from .tier import TIER_BY_ID, TierId


def validate_task(task: TaskSpec) -> Result[None]:
    """校验任务输入。

    档位有效性在类型层已保证（`TierId` 是枚举），
    所以这里只查内容——需求为空是唯一该拦的。
    """
    if not task.requirement.strip():
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "需求不能为空")
    return Result.ok_none()


def validate_dcard(dcard: DCard) -> Result[None]:
    """校验方向确认单（D-069 的机械判据）。

    两条规则，都是**结构上**禁止编造，不看文本内容：

    1. 必填字段（除 D8）为空 → 拦（D-Card 未完成，不得动笔）
    2. 必填字段被标为 `LEAD_INFERRED` → 拦
       （D-040：只有 D8 允许主理人研判，其余字段用户没给就是没给）

    第 2 条是关键：如果允许标成研判，模型只要把字段标成 `LEAD_INFERRED`
    就能自己填上内容——"严禁编造"立刻失效。
    """
    for fid, f in dcard.by_id.items():
        if f.is_missing:
            if fid not in ALLOWED_INFERRED_FIELDS:
                return Result.fail(
                    ErrorCode.DCARD_FIELD_MISSING,
                    f"{f.label}缺失，必须由用户提供",
                )
            continue

        # 已填值：检查出处标注是否越权
        if f.provenance is Provenance.LEAD_INFERRED and fid not in ALLOWED_INFERRED_FIELDS:
            return Result.fail(
                ErrorCode.DCARD_PROVENANCE_INVALID,
                f"{f.label}不允许主理人研判（D-040：仅 D8 可研判）",
            )
    return Result.ok_none()


def validate_gate_balance(tier: TierId) -> Result[None]:
    """校验"编制—门禁配平"（材料原文的铁律）。

    某档门禁含某 Gate，则该 Gate 的取证方必在该档编制内，
    **主理人不得代行取证**（唯一例外是 G3：文择体未编入时降级）。

    这条规则挡的是 D-049 补漏的那类缺陷：门禁存在但没人取证，
    结果就是那道门禁静默通过——比不做门禁更危险。
    """
    spec = TIER_BY_ID[tier]

    if not spec.gates_apply:
        return Result.ok_none()  # 咨询档不适用门禁

    roster = set(spec.roster)
    for gate in FORMAL_GATES:
        owners = GATES[gate].owners
        if not owners:
            continue  # G9 动态取证，另行判定
        if gate is GateId.G3 and RoleId.DOC_ADVISOR not in roster:
            continue  # 唯一例外：文择体未编入时 G3 降级
        missing = [o for o in owners if o not in roster]
        if missing:
            return Result.fail(
                ErrorCode.GATE_RESPONSIBLE_NOT_IN_TIER,
                f"{gate} 的取证方不在本档编制内",
            )

    # 并行分组的成员也必须在编制内（分组是执行单位）。
    # 文择体是唯一可豁免者：他只在 Pro 档编入（对应 G3 的降级例外）。
    for group in GATE_GROUPS:
        for member in group:
            if member not in roster and member is not RoleId.DOC_ADVISOR:
                return Result.fail(
                    ErrorCode.GATE_RESPONSIBLE_NOT_IN_TIER,
                    f"并行组含本档未编入成员：{member.value}",
                )
    return Result.ok_none()


def validate_run_state(state: RunState) -> Result[None]:
    """运行状态自检。

    校验的是**不该发生的状态**，而非格式：
    角色状态里出现本档未编入的成员，说明编排写错了——
    这种错一旦发生，那个成员会收到本不该收的任务。
    """
    roster = set(state.active_roles())
    for role in state.role_states:
        if role not in roster:
            return Result.fail(
                ErrorCode.GATE_RESPONSIBLE_NOT_IN_TIER,
                f"状态含未编入成员：{role.value}",
            )
    for gate in state.gates:
        if gate not in {g.value for g in FORMAL_GATES}:
            return Result.fail(ErrorCode.GATE_NO_EVIDENCE, f"未知门禁：{gate}")
    return Result.ok_none()
