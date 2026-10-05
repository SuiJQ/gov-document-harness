"""阶段标识与状态。

代码的 `PhaseId` 与架构 §8.3 的「Phase -1/0/0.5/1/2/3/4」
是**两套东西**，各有分工：

- `PhaseId` 是**状态机的控制流**——决定「下一步做什么、失败了去哪」，
  是可测、可 diff 的代码。
- 架构的 Phase 号是**编排语义**——决定「这一步谁产出什么」，
  写在架构 §8.3 与内容包提示词里。

两者**不必同名**，但必须能互相翻译：`_ARCHITECTURE_PHASE` 就是那张表。
没有它，代码按自己的阶段推进、内容包按架构的阶段理解，
两边对不上「谁该在什么时候写什么」——**这类错误任何单侧检查都查不出来**。

> **别把映射挪进内容包**：`app.py` 的 `_PRODUCING` 已声明
> 角色对应关系只放代码一处（D-080）。映射表同理，改内容包不该动它。

标准模式只展示「写作方向确认单 / 文章大纲 / 正文初稿」三个阶段成果（D-046）——
那是**投影层**的事，本模块不关心界面怎么显示。
"""

from __future__ import annotations

from enum import StrEnum


class PhaseId(StrEnum):
    """阶段标识（英文，仅用于日志与状态持久化）。"""

    INTAKE = "intake"  # 需求采集
    DIRECTION = "direction"  # 方向确认（D-Card）
    OUTLINE = "outline"  # 文章大纲
    DRAFTING = "drafting"  # 正文起草
    GATING = "gating"  # 门禁检查（并行 4 组，D-049）
    REVISION = "revision"  # 退回修正
    FINAL = "final"  # 终审与交付
    DONE = "done"  # 交付完成

    @property
    def display_label(self) -> str:
        """界面中文名（D-080）。"""
        return _PHASE_LABELS[self]

    @property
    def architecture_phase(self) -> str:
        """对应的架构 §8.3 阶段号。

        内容包提示词里写的是「Phase 0」「Phase 0.5」这些编号，
        日志与状态里存的是 `PhaseId`——排查问题时两边对不上号，
        就看不出「代码这一步对应内容包的哪一步」。
        """
        return _ARCHITECTURE_PHASE[self]


#: `PhaseId` → 架构 §8.3 阶段号。
#:
#: **一对多是有意的**：`OUTLINE` 一格代码里要走完架构的
#: Phase 0（陈纲举因果推演）与 Phase 0.5（骆准绳政策取证 + 文择体文种判定）
#: 两次角色交接，D-049 的门禁另算 `GATING`。
#: 所以这里记的是**区间**「0~0.5」而不是单值——
#: 假装是一对一，等于把「代码少一次交接」这件事藏起来。
_ARCHITECTURE_PHASE: dict[PhaseId, str] = {
    PhaseId.INTAKE: "-1（+ 需求采集前置）",
    PhaseId.DIRECTION: "-1",
    PhaseId.OUTLINE: "0 ~ 0.5",
    PhaseId.DRAFTING: "1",
    PhaseId.GATING: "3",
    PhaseId.REVISION: "1（退回重做）",
    PhaseId.FINAL: "2 ~ 4",
    PhaseId.DONE: "4（完成）",
}


class PhaseState(StrEnum):
    """阶段状态。"""

    PENDING = "未开始"
    ACTIVE = "进行中"
    GATED = "门禁中"
    REJECTED = "已退回"
    APPROVED = "已通过"
    SKIPPED = "已跳过"


_PHASE_LABELS: dict[PhaseId, str] = {
    PhaseId.INTAKE: "需求采集",
    PhaseId.DIRECTION: "方向确认",
    PhaseId.OUTLINE: "文章大纲",
    PhaseId.DRAFTING: "正文起草",
    PhaseId.GATING: "门禁检查",
    PhaseId.REVISION: "退回修正",
    PhaseId.FINAL: "终审交付",
    PhaseId.DONE: "交付完成",
}
