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
    #: 本角色的采样温度（用户约束：基准 0.3，各角色可不同）。
    #:
    #: **不可由用户配置**，理由见 D-243：温度填错不报错，
    #: 只让文稿质量悄悄下降，用户看不出来（D-146 的坑）。
    温度: float = 0.3

    def __str__(self) -> str:
        return self.display_name


#: 全部五个角色。顺序即材料的原始顺序（D-029 编制范围依此判定）。
#:
#: ## 温度取值（用户定的原则：严肃的调低、有创造性的相对高、总体偏低）
#:
#: | 角色 | 职责 | 温度 | 理由 |
#: |---|---|---|---|
#: | 骆准绳 | 合规与格式 | 0.1 | 终审要的是**稳定**，任何发挥都是风险 |
#: | 陈纲举 | 因果与结构 | 0.2 | 结构错一点全文就塌，不该有发挥空间 |
#: | 范正章 | 主理人/终审 | 0.2 | 方向锁须稳定；他不是在创作 |
#: | 文择体 | 文种判定 | 0.3 | 判定要稳，但文种选择本身需一点判断力 |
#: | 辞达意 | 语言起草 | 0.5 | 起草需要变化；仍是全文最高的 |
#:
#: **总体偏低**：公文不是创意写作，0.5 是上限而非常态。
ROLES: tuple[RoleSpec, ...] = (
    RoleSpec(RoleId.TEAM_LEAD, "范正章", "主理人 / 方向锁 / 终审", 0.2),
    RoleSpec(RoleId.STRUCTURE, "陈纲举", "因果与结构", 0.2),
    RoleSpec(RoleId.LANGUAGE, "辞达意", "语言起草", 0.5),
    RoleSpec(RoleId.COMPLIANCE, "骆准绳", "合规与格式", 0.1),
    RoleSpec(RoleId.DOC_ADVISOR, "文择体", "文种判定", 0.3),
)

ROLE_BY_ID: dict[RoleId, RoleSpec] = {r.id: r for r in ROLES}


def 取温度(role: RoleId) -> float:
    """该角色的采样温度。**唯一取法**。

    找不到角色时**回落到全局基准 0.3**——
    这里与 D-146 的口径不同：角色是**代码内固定的五项**，
    不是用户可填的输入，故不存在「填错」这个入口，
    回落只防将来加角色时漏配，不会把用户的输入静默改掉。
    """
    规 = ROLE_BY_ID.get(role)
    return 规.温度 if 规 is not None else 0.3


def display_name(role: RoleId) -> str:
    """界面文案的唯一取法。

    界面上**只能**通过这个函数拿角色名，不得直接用 `role.value`
    ——那会漏出英文（D-080）。
    """
    return ROLE_BY_ID[role].display_name
