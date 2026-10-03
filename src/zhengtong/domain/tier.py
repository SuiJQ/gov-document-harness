"""档位数据化（D-067）。

三档是**数据**，不是散落在 `if` 里的分支。
把"哪档有谁、哪档查不查、哪档能不能退"集中成一张表，
是为了让"编制—门禁配平铁律"能被机械校验（见 `tests/` 配平测试）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .role import RoleId


class ReasoningPolicy(StrEnum):
    """档位的深度思考策略（用户定：**由档位决定，不由用户手填**）。

    ## 为什么不给用户手填

    D-146 的教训：新增配置项必须先想清楚「填错了会静默变成什么」。
    思考深度若做成可手填的开关，填错就是**静默降级**——
    用户以为在深度思考，实际没有，而界面与日志都不会提示。

    而档位本来就是用户已经明确选的，思考策略随档位自动确定，
    就没有这个入口，也就没有这个风险。

    ## 与 D-052 的分工

    本枚举只回答「**要不要**深度思考」；
    「**怎么**开关」由 `adapters/llm/reasoning_probe.py` 按后端烙印处理
    （架构 §6.2 探测四种写法）。

    而无论开关成功与否，**推理内容一律不进上下文**（D-052）：
    `reasoning_content` 丢弃，不写进成员消息历史、不落盘。
    本枚举管不了也管不着那件事——那是适配器层的硬约束。
    """

    OFF = "off"  # 不启用深度思考
    ON = "on"  # 启用深度思考


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
    reasoning_policy: ReasoningPolicy = ReasoningPolicy.OFF

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
        # 咨询只有主理人一人、且不产出成文，深度思考没有收益
        reasoning_policy=ReasoningPolicy.OFF,
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
        # 用户定：Flash **不**深度思考
        reasoning_policy=ReasoningPolicy.OFF,
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
        # 用户定：Pro **深度**思考
        reasoning_policy=ReasoningPolicy.ON,
    ),
)

TIER_BY_ID: dict[TierId, TierSpec] = {t.id: t for t in TIERS}
