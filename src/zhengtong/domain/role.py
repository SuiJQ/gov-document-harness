"""角色标识与六态。

**D-080 的实现点之一**：本模块同时提供内部标识（英文）
与界面文案（中文），且映射**只在这里**——架构与决策都要求
映射唯一，禁止各处另建对照表。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RoleId(StrEnum):
    """角色内部标识。

    **英文，只用于目录名、日志、事件 `data`、`manifest.json`。**
    界面上出现角色名的地方一律用 `RoleSpec.display_name`（中文），
    一个英文都不许出现（D-080）。
    """

    TEAM_LEAD = "team-lead"  # 范正章
    STRUCTURE = "structure"  # 陈纲举
    LANGUAGE = "language"  # 辞达意
    COMPLIANCE = "compliance"  # 骆准绳
    DOC_ADVISOR = "doc-advisor"  # 文择体


class RoleState(StrEnum):
    """成员状态机六态（D-042）。"""

    IDLE = "空闲"
    RUNNING = "运行中"
    AWAITING_USER = "等待用户"
    AWAITING_CONDITION = "等待条件"
    SUSPENDED = "挂起"
    DONE = "完成"

    @property
    def is_terminal(self) -> bool:
        """是否终态。终态不再接受任何流转。"""
        return self in (RoleState.DONE,)


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """一个角色的完整定义。

    `id` 是内部标识，`display_name` 是界面文案——
    这两者分开是 D-080 的核心：混成一个字段就必然有一处漏出英文。
    """

    id: RoleId
    display_name: str
    duty: str

    def __str__(self) -> str:
        return self.display_name


#: 全部五个角色。顺序即材料的原始顺序（D-029 编制范围依此判定）。
ROLES: tuple[RoleSpec, ...] = (
    RoleSpec(RoleId.TEAM_LEAD, "范正章", "主理人 / 方向锁 / 终审"),
    RoleSpec(RoleId.STRUCTURE, "陈纲举", "因果与结构"),
    RoleSpec(RoleId.LANGUAGE, "辞达意", "语言起草"),
    RoleSpec(RoleId.COMPLIANCE, "骆准绳", "合规与格式"),
    RoleSpec(RoleId.DOC_ADVISOR, "文择体", "文种判定"),
)

ROLE_BY_ID: dict[RoleId, RoleSpec] = {r.id: r for r in ROLES}


def display_name(role: RoleId) -> str:
    """界面文案的唯一取法。

    界面上**只能**通过这个函数拿角色名，不得直接用 `role.value`
    ——那会漏出英文（D-080）。
    """
    return ROLE_BY_ID[role].display_name
