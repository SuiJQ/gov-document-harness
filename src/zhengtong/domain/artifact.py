"""产物。

产物分两类，**不要混**：

- `DRAFT`：过程中的稿件（大纲、初稿、修正稿），可被覆盖、可重生成
- `DELIVERABLE`：最终交付物（D-038 主件 + 配套件），导出前必过占位符扫描

分开的理由是导出层的拦截逻辑（架构 §15 第 15 步）：
**只扫描交付物**，否则过程稿里的占位符会把导出永久堵死。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .ids import ArtifactId


class ArtifactKind(StrEnum):
    """产物种类。"""

    OUTLINE = "outline"  # 文章大纲
    DRAFT = "draft"  # 正文初稿 / 修正稿
    GATE_REPORT = "gate-report"  # 门禁报告
    MAIN_DOCUMENT = "main-document"  # 主件（交付物）
    ATTACHMENT = "attachment"  # 配套件（交付物）
    NOTE = "note"  # 交付说明

    @property
    def is_deliverable(self) -> bool:
        """是否属最终交付物。只有交付物才走导出扫描与门禁。"""
        return self in (
            ArtifactKind.MAIN_DOCUMENT,
            ArtifactKind.ATTACHMENT,
            ArtifactKind.NOTE,
        )


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """产物引用。**只存引用，不存全文**。

    全文由存储层负责，领域层只记"哪个产物、在哪、什么版本"。
    把全文塞进状态对象会让状态无限膨胀，
    断点续跑时写一个几百 KB 的 JSON 是迟早的事。
    """

    id: ArtifactId
    kind: ArtifactKind
    path: str  # 相对 data/<run-id>/ 的路径
    version: int = 1

    @property
    def is_deliverable(self) -> bool:
        return self.kind.is_deliverable
