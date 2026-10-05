"""门禁编号与取证方映射。

**D-084 是本模块存在的原因**：门禁的取证方**不是** `RoleId` 的
一对一映射，而是 `tuple[RoleId, ...]`，且 G9 是**动态**的
（由所触发的红线决定取证人）。写成单值会在 G13 上直接错。

G0–G13 共 14 项编号，其中 **G0 / G6 为自检**（主理人自查，不占门禁序号），
正式门禁 **12 道**。三者不等价，勿混。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .role import ROLES, RoleId
from .run_state import GateRecord


class GateId(StrEnum):
    """门禁编号。G0 与 G6 虽是编号，但**降为自检**，不计入 12 道。"""

    G0 = "G0"  # 自检：质量恒定（主理人自查）
    G1 = "G1"  # 格式零缺项（GB/T 9704）
    G2 = "G2"  # 事实与合规（三态标注 + 推定词拦截 + 政治表述）
    G3 = "G3"  # 文种零错配
    G4 = "G4"  # 语言零歧义（含去 AI 味）
    G5 = "G5"  # 结构零断裂
    G6 = "G6"  # 自检：受众可追溯（主理人自查）
    G7 = "G7"  # 依据全可溯
    # 「推演」二字不可省（与内容包 gate-manual G8 对齐）：
    # 只判「有没有因果」会放过有因无果、有果无因的空链条，
    # 而 G8 查的正是**推演链是否闭合**。
    G8 = "G8"  # 因果推演零缺项
    G9 = "G9"  # 历史错误零复现（按红线归属退，见下）
    # 「依据」二字不可省：G10 查的是**政策依据**是否齐备，
    # 不是政策文本是否提到过。
    G10 = "G10"  # 政策依据零缺失
    G11 = "G11"  # 硬约束零沉降（D7 量化指标逐条落入成稿）
    G12 = "G12"  # 用户输入回看（对照用户原话终核）
    G13 = "G13"  # 折中论证透明 + 例外接口清单

    @property
    def is_self_check(self) -> bool:
        """是否为主理人自检项。**自检不占门禁序号。**"""
        return self in (GateId.G0, GateId.G6)


#: 正式门禁 12 道（不含 G0 / G6）。
FORMAL_GATES: tuple[GateId, ...] = (
    GateId.G1,
    GateId.G2,
    GateId.G3,
    GateId.G4,
    GateId.G5,
    GateId.G7,
    GateId.G8,
    GateId.G9,
    GateId.G10,
    GateId.G11,
    GateId.G12,
    GateId.G13,
)


#: 并行分组（D-049）：**由 `GATES[...].owners` 按角色归组推导，不另建事实源。**
#:
#: 审计 D-143：本表原先是一份**独立声明**的表，于是变成第二事实源——
#: 它只列了 4 个组，漏掉 TEAM_LEAD 组（G0/G6/G12）与
#: G13（STRUCTURE + COMPLIANCE 双取证方）；
#: 注释还称「组 2 拆出 G13 的结构部分」，而 G13 根本不在表里，
#: **注释描述了一个代码里不存在的行为**。
#: 更糟的是 `validation.py` 曾据此做编制校验（现已删除那段冗余校验），
#: 而当时的测试只断言「每组是单元素」，**从未断言它与 `GATES` 一致**，
#: 两表漂移因此长期无人发现。
#:
#: 故改为**从 `GATES` 推导**，并由 `tests/test_domain_invariants.py`
#: 的 `test_并行分组须与GATES一致` 锁死：改了 `owners` 而忘了这里会红。
#:
#: G9 天然不在任何组里——它的取证方运行期才定（D-084）。
#: G12 与 DG-X 是主理人独占终闸，不与其他门禁并行。
@dataclass(frozen=True, slots=True)
class GateSpec:
    """一道门禁的静态定义。

    `owners` 是**元组**不是单值：
    G13 由陈纲举与骆准绳共同取证（D-084），
    写成单值就丢了一半取证方。
    `dynamic_owners` 为真表示取证方运行期才确定（只有 G9 是这种）。
    """

    id: GateId
    name: str
    owners: tuple[RoleId, ...] = ()
    dynamic_owners: bool = False


GATES: dict[GateId, GateSpec] = {
    GateId.G1: GateSpec(GateId.G1, "格式零缺项", (RoleId.COMPLIANCE,)),
    GateId.G2: GateSpec(GateId.G2, "事实与合规", (RoleId.COMPLIANCE,)),
    GateId.G3: GateSpec(GateId.G3, "文种零错配", (RoleId.DOC_ADVISOR,)),
    GateId.G4: GateSpec(GateId.G4, "语言零歧义", (RoleId.LANGUAGE,)),
    GateId.G5: GateSpec(GateId.G5, "结构零断裂", (RoleId.STRUCTURE,)),
    GateId.G7: GateSpec(GateId.G7, "依据全可溯", (RoleId.COMPLIANCE,)),
    GateId.G8: GateSpec(GateId.G8, "因果零缺项", (RoleId.STRUCTURE,)),
    # G9 不独立取证，按所触发的红线归属退回（D-084）
    GateId.G9: GateSpec(GateId.G9, "历史错误零复现", dynamic_owners=True),
    GateId.G10: GateSpec(GateId.G10, "政策零缺失", (RoleId.COMPLIANCE,)),
    GateId.G11: GateSpec(GateId.G11, "硬约束零沉降", (RoleId.COMPLIANCE,)),
    GateId.G12: GateSpec(GateId.G12, "用户输入回看", (RoleId.TEAM_LEAD,)),
    GateId.G13: GateSpec(
        GateId.G13, "折中论证透明 + 例外接口清单", (RoleId.STRUCTURE, RoleId.COMPLIANCE)
    ),
    GateId.G0: GateSpec(GateId.G0, "质量恒定（自检）", (RoleId.TEAM_LEAD,)),
    GateId.G6: GateSpec(GateId.G6, "受众可追溯（自检）", (RoleId.TEAM_LEAD,)),
}


def gate_owners(gate: GateId) -> tuple[RoleId, ...]:
    """静态取证方。G9 返回空元组——它的取证方运行期才定。"""
    return GATES[gate].owners


def gate_groups() -> tuple[tuple[RoleId, ...], ...]:
    """并行分组：由 `GATES[...].owners` 推导，**不另建事实源**（D-143）。

    惰性求值（函数而非常量）是为避开定义顺序——
    它依赖 `GATES`，而 `GATES` 定义在本文件上方，但它自身是本模块的公开常量，
    改为常量就会在定义时求值，形成前向引用。

    规则：**只收「恰好独占一整组」的角色**（`owners` 为该角色的单元素元组）。

    - G13 是**双取证方**（STRUCTURE + COMPLIANCE），故不在任何组里：
      把它硬并进 STRUCTURE 那一组，会让双取证的门禁与单取证的并行，
      破坏 D-084 定下的「G13 由两人共同取证」语义。
    - G9 的取证方运行期才定（`dynamic_owners`），天然不在任何组里。
    - G12 与 DG-X 是主理人独占终闸，不与其他门禁并行。

    组序取 `ROLES` 的声明顺序，使分组顺序在日志与测试里稳定可比对。
    """
    return tuple(
        (role.id,)
        for role in ROLES
        if any(
            not spec.dynamic_owners and len(spec.owners) == 1 and spec.owners[0] is role.id
            for spec in GATES.values()
        )
    )


def 并行门禁组() -> tuple[tuple[RoleId, ...], ...]:
    """与主理人终闸无关、可真并行的取证组（D-049）。

    **返回的不含 TEAM_LEAD 组**：G0/G6/G12 是主理人独占终闸
    （架构 §8.3 阶段图：主理人独占 DG-X + G12），按定义不与其他门禁并行。
    `gate_groups()` 把终闸组也一并列出（那是对全部独立取证角色的完整枚举，
    审计 D-143 据此补回了 TEAM_LEAD 组）——但**并行调度的调用方要的是
    「哪几组可以同时跑」**，把终闸组混进来会让并行退化成「终闸也和别的组
    一起跑」，违反架构「不与其他门禁并行」的明文。故这里显式剔除终闸组。

    架构 §8.3 的分组是：骆准绳 / 陈纲举 / 辞达意 / 文择体 = 4 组。
    """
    return tuple(组 for 组 in gate_groups() if RoleId.TEAM_LEAD not in 组)


def 终闸组() -> tuple[tuple[RoleId, ...], ...]:
    """主理人独占终闸（架构 §8.3：DG-X + G12），不与其他门禁并行。

    **返回形状与 `gate_groups()` 保持一致（组是元组的元组）**，
    这样 `并行门禁组() | 终闸组() == gate_groups()` 才能做集合比较。
    """
    return ((RoleId.TEAM_LEAD,),)


# ═══ 门禁判定（D-028/D-050/反死循环铁律）═══

#: 同一门禁退回上限（D-050）：退回 2 次后不再自动重写，转用户裁决。
退回上限: Final = 2


class 处置(StrEnum):
    """一道门禁失败（或通过）之后的**唯一合法去向**。

    三个值穷尽架构 §8.3 的门禁失败分支，没有第四条路——
    尤其**没有「警告后放行」**：那正是 D-028 禁止的绕过出口。
    """

    放行 = "放行"
    退回 = "退回"
    升级裁决 = "升级裁决"


def 门禁判定(记录: GateRecord, 环境性缺失: bool) -> 处置:
    """把一道门禁的取证结论映射为**唯一**处置（纯函数，可单测）。

    规则（架构 §8.3 + D-050 + gate-manual 反死循环铁律）：

    1. `记录.passed` → **放行**。建议类不阻断，归档即可（D-016）。
    2. `环境性缺失`（无库/无网导致取证失败）→ **一次性升级裁决**，
       **不消耗退回数**。反复退回本身就是缺陷，这是代码里的显式分支，
       不靠提示词记得。
    3. 不通过且 `记录.attempt >= 退回上限` → **升级裁决**（D-050）。
    4. 其余不通过 → **退回**重写（红线无绕过出口，D-028）。

    `环境性缺失` 是显式入参而非从 `记录` 推断：无网络与「事实编造」
    在文字上无法可靠区分，混在一起判就会把环境问题当成文稿问题退 2 次。
    """
    if 记录.passed:
        return 处置.放行
    if 环境性缺失:
        return 处置.升级裁决
    if 记录.attempt >= 退回上限:
        return 处置.升级裁决
    return 处置.退回
