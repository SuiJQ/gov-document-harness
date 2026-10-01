"""档位数据化（D-067）。

三档是**数据**，不是散落在 `if` 里的分支。
把"哪档有谁、哪档查不查、哪档能不能退"集中成一张表，
是为了让"编制—门禁配平铁律"能被机械校验（见 `tests/` 配平测试）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .role import RoleId


class TierId(StrEnum):
    """档位内部标识（英文，仅用于配置键与日志）。"""

    CONSULT = "consult"  # 咨询
    FLASH = "flash"  # 成文 Flash
    PRO = "pro"  # 成文 Pro


@dataclass(frozen=True, slots=True)
class TierSpec:
    """一个档位的全部行为参数。

    `requires_user_questions` 单独成字段而不用 `len(roster) > 1` 推断：
    咨询与 Flash 都是"人少"，但 Flash **必须问全部 D1–D9**（D-029/D-040，
    用户明确"Flash 不缩水提问，只减人数"）。推断式写法迟早会写错成人数判断。
    """

    id: TierId
    display_name: str
    roster: tuple[RoleId, ...]
    requires_user_questions: bool
    policy_retrieval: bool  # 是否做政策依据检索
    gates_apply: bool  # 门禁是否适用
    allows_rewrite: bool  # 是否允许退回重写

    @property
    def is_consult(self) -> bool:
        return self.id is TierId.CONSULT


#: 三档定义（D-029）。顺序即档位递增顺序。
TIERS: tuple[TierSpec, ...] = (
    TierSpec(
        id=TierId.CONSULT,
        display_name="咨询",
        roster=(RoleId.TEAM_LEAD,),
        requires_user_questions=False,  # 咨询不触发提问
        policy_retrieval=False,
        gates_apply=False,  # 门禁不适用
        allows_rewrite=False,
    ),
    TierSpec(
        id=TierId.FLASH,
        display_name="成文 Flash",
        # D-029：骆准绳必须出面——G1/G2/G7/G10 四道门禁的取证方正是他
        roster=(
            RoleId.TEAM_LEAD,
            RoleId.STRUCTURE,
            RoleId.LANGUAGE,
            RoleId.COMPLIANCE,
        ),
        requires_user_questions=True,  # 问全部要素，不缩水
        policy_retrieval=True,  # 简单检索
        gates_apply=True,
        allows_rewrite=True,
    ),
    TierSpec(
        id=TierId.PRO,
        display_name="成文 Pro",
        roster=(
            RoleId.TEAM_LEAD,
            RoleId.STRUCTURE,
            RoleId.LANGUAGE,
            RoleId.COMPLIANCE,
            RoleId.DOC_ADVISOR,
        ),
        requires_user_questions=True,
        policy_retrieval=True,
        gates_apply=True,
        allows_rewrite=True,
    ),
)

TIER_BY_ID: dict[TierId, TierSpec] = {t.id: t for t in TIERS}


def tier_by_name(name: str) -> TierSpec | None:
    """按配置里的档位名取档。认内部标识与中文名两种写法。"""
    for spec in TIERS:
        if name == spec.id.value or name == spec.display_name:
            return spec
    return None
