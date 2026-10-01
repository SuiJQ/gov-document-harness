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

from .role import RoleId


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

#: 并行分组（D-049）。组 1 补入了 G11，组 2 拆出 G13 的结构部分。
#: G9 与 G12 不在此表：G9 动态、G12 与 DG-X 是主理人独占终闸。
GATE_GROUPS: tuple[tuple[RoleId, ...], ...] = (
    (RoleId.COMPLIANCE,),  # G1 G2 G7 G10 G11
    (RoleId.STRUCTURE,),  # G5 G8
    (RoleId.LANGUAGE,),  # G4（+ E10，见 D-084）
    (RoleId.DOC_ADVISOR,),  # G3（仅 Pro 档编入）
)


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
