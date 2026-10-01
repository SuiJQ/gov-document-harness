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

from .role import ROLES, RoleId


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
    G8 = "G8"  # 因果零缺项
    G9 = "G9"  # 历史错误零复现（按红线归属退，见下）
    G10 = "G10"  # 政策零缺失
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
