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
from .gate import FORMAL_GATES, GATES, GateId
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

    # 审计 D-143：这里原先还有一段遍历 `GATE_GROUPS` 的相同校验，
    # 已删除。两个原因：
    #
    # 1. **完全冗余**。穷举 2^5 全部 32 种编制组合实测：
    #    该段独有的拦截 0 例、误拦 0 例——上面那段按
    #    `FORMAL_GATES` + `GATES[gate].owners` 已经覆盖了全部 12 道门禁。
    # 2. **它还是第二事实源**（D-055/D-008 禁止的正是这个）。
    #    `GATE_GROUPS` 只声明了 4 个组，漏掉 TEAM_LEAD 组（G0/G6/G12）
    #    与 G13（STRUCTURE + COMPLIANCE 双取证方）；
    #    它的注释还称「组 2 拆出 G13 的结构部分」，
    #    而代码里 G13 完全不在该表中——注释描述了一个不存在的行为。
    #
    # **判据**：同一份事实只应存在于一处。
    # 并行分组不是独立信息，它就是 `owners` 按角色归组的结果；
    # 要用时从 `GATES` 推导，不要另建一张表。
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
                ErrorCode.STATE_ROLE_NOT_IN_TIER,
                f"状态含未编入成员：{role.value}",
            )
    for gate in state.gates:
        if gate not in {g.value for g in FORMAL_GATES}:
            return Result.fail(ErrorCode.STATE_UNKNOWN_GATE, f"未知门禁：{gate}")
    return Result.ok_none()


def g3降级标注(tier: TierId) -> tuple[str, ...]:
    """G3 降级时**必须展示**给用户的标注（D-116）。

    ## 架构原文（§8.7）

    > 唯一例外是 G3（Flash 时降级给主理人，且成果里**必须带
    > "文种由主理人研判"这一行，要向用户展示**，因为文种判错整篇翻车）。

    同节强调：「这条**要写成代码里的显式分支，不能靠提示词记得**」。

    ## 为什么必须是函数而不是提示词里的一句话

    提示词里的「记得标注」对本项目无效——
    本项目反复吃亏的地方正是「靠模型自觉」：
    换个模型、降个档、上下文一压缩，那句话就没了。
    而文种判错整篇翻车，是全流程代价最大的单点。

    故**降级这个事实必须由代码算出来**，
    标注是这个计算的输出，不是模型的可选项。
    调用方（编排层/接入层）拼进成果即可。

    ## 与 `validate_gate_balance` 的关系

    那里的 `continue` 负责**放行**（否则门禁配平校验会误报 Flash 档）；
    本函数负责**留痕**。两处必须一致：
    放行却不留痕 = 悄悄降级；留痕却不放行 = 门禁卡死。
    `tests/test_step11_debts.py::test_编排用得上这个判定` 双向锁死。

    ## 返回空元组而非空串

    拼接方可以直接 `*标注` 展开，不必写 `if 标注:`——
    少一个分支就少一处「忘了拼」。
    """
    spec = TIER_BY_ID[tier]
    if not spec.gates_apply:
        return ()  # 咨询档不过门禁，无从降级
    if RoleId.DOC_ADVISOR in spec.roster:
        return ()  # 文择体在编制内，G3 正常取证
    return (f"文种由主理人研判（{spec.display_name} 档未编入文择体，G3 降级由主理人取证）",)
