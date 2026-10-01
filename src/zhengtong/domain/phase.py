"""阶段标识与状态。

阶段划分取自架构 §8.2 的流程骨架。标准模式只展示
"写作方向确认单 / 文章大纲 / 正文初稿"三个阶段成果（D-046）——
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
