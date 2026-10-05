"""方向确认单（D-Card）。

D-069 是本模块的全部意义所在：
**把"严禁编造"（D-034）从提示词层的软约束，变成结构上无法违反的硬约束。**

做法是给每个字段标注 `Provenance`（出处）：

- `USER` —— 用户明确说的，或从 `user/` 文件提取的。**只有这两种来源。**
- `LEAD_INFERRED` —— 范正章研判的，**仅 D8 允许**（D-040）。

于是导出前的编造检查退化���一次机械判断：

    任何必填字段，若值为空而 provenance 声明为 USER → 拦截

**不需要判断文本像不像编的**，因为编造在写入时就已经结构上不可能发生。

D-040 的字段清单：

| 编号 | 字段 | 缺失时 | 允许研判 |
|---|---|---|---|
| D1 | 主件：文种 + 标题 + 份数 | 弹表单问，不可推断 | 否 |
| D2 | 配套件：逐项清单 | 问"有无配套件" | 否 |
| D3 | 关键时点：定稿 / 报送截止 | 问；拒答标待填 | 否 |
| D4 | 事实边界：禁止假设项 | 问 | 否 |
| D5 | 数据状态：逐项标注可靠度与时效 | 未标注不得入正文 | 否 |
| D6 | 主体排序：顺序 + 排序依据 | ≥3 主体时必问 | 否 |
| D7 | 硬约束清单 + 落点章节 | **必问，缺则不得动笔** | 否 |
| D8 | 语域特征 | 范正章研判并标 | **是** |
| D9 | 例外接口：责任主体 + 触发条件 + 后续动作 | 涉及折中方案时必问 | 否 |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class DCardFieldId(StrEnum):
    """D1–D9 字段编号。中文名只在界面用（D-080）。"""

    D1 = "D1"
    D2 = "D2"
    D3 = "D3"
    D4 = "D4"
    D5 = "D5"
    D6 = "D6"
    D7 = "D7"
    D8 = "D8"
    D9 = "D9"


class Provenance(StrEnum):
    """字段出处（D-069）。"""

    USER = "USER"  # 用户明确说的，或从 user/ 文件提取的
    LEAD_INFERRED = "LEAD_INFERRED"  # 范正章研判，仅 D8 允许

    @property
    def is_inferred(self) -> bool:
        return self is Provenance.LEAD_INFERRED


@dataclass(frozen=True, slots=True)
class DCardField:
    """方向确认单的一个字段。

    用 `frozen=True` 是有意的：一旦填了就不可就地改，
    任何"改一下来源标注"的动作都必须显式重建字段——
    那正是我们想让人多想一步的地方。
    """

    id: DCardFieldId
    label: str
    value: str | None = None
    provenance: Provenance = Provenance.USER

    @property
    def is_filled(self) -> bool:
        return self.value is not None and self.value.strip() != ""

    @property
    def is_missing(self) -> bool:
        return not self.is_filled

    def filled_with(self, value: str, provenance: Provenance) -> DCardField:
        """返回填好值的**新**字段（原字段不变）。"""
        return DCardField(self.id, self.label, value, provenance)


@dataclass(frozen=True, slots=True)
class DCard:
    """完整的方向确认单。

    `by_id` 存全部 9 个字段（含未填的），因为"某字段存在但为空"
    与"某字段不存在"是两件事：前者是待补信息，后者是漏了字段本身。
    """

    by_id: dict[DCardFieldId, DCardField] = field(default_factory=dict)

    def get(self, fid: DCardFieldId) -> DCardField:
        return self.by_id[fid]

    def value(self, fid: DCardFieldId) -> str | None:
        return self.by_id[fid].value

    @property
    def missing_fields(self) -> tuple[DCardFieldId, ...]:
        """缺失且必须由用户提供的字段。

        **D8 不算缺失**——它允许主理人研判，
        且研判结果会标 `LEAD_INFERRED`，不会构成编造。
        """
        return tuple(
            fid
            for fid, f in self.by_id.items()
            if f.is_missing and fid not in ALLOWED_INFERRED_FIELDS
        )

    def with_field(self, f: DCardField) -> DCard:
        """返回替换了某字段的**新**确认单。"""
        new = dict(self.by_id)
        new[f.id] = f
        return DCard(new)


#: 允许主理人研判的字段（D-040：仅 D8）。
ALLOWED_INFERRED_FIELDS: Final[frozenset[DCardFieldId]] = frozenset({DCardFieldId.D8})

#: 字段中文标签（D-080：只在此处一处）。
FIELD_LABELS: Final[dict[DCardFieldId, str]] = {
    DCardFieldId.D1: "主件：文种、标题、份数",
    DCardFieldId.D2: "配套件清单",
    DCardFieldId.D3: "关键时点（定稿 / 报送截止）",
    DCardFieldId.D4: "事实边界：禁止假设项",
    DCardFieldId.D5: "数据状态（可靠度与时效）",
    DCardFieldId.D6: "主体排序（顺序与依据）",
    DCardFieldId.D7: "硬约束清单与落点章节",
    DCardFieldId.D8: "语域特征",
    DCardFieldId.D9: "例外接口（责任主体、触发条件、后续动作）",
}


def empty_dcard() -> DCard:
    """一张全空的确认单，九个字段都在、值都空。"""
    return DCard({fid: DCardField(fid, label) for fid, label in FIELD_LABELS.items()})
