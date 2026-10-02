"""组装：按 D-025 五个手段把输入拼成一个 `ContextView`。

**composer 不做判断，只做组装。** 是否超阈值、要不要压缩是 `compactor` 的事。
分开的理由：组装是纯函数（同样输入必得同样视图），
而压缩要读容量、要调摘要器；混在一起会让「组装」变得依赖运行环境。

## 五个主动手段如何落到字段上

| 手段 | 本模块对应 |
|---|---|
| 系统与角色恒在 | `system_and_role` 原样置于最前 |
| 绝不丢弃（D-039） | `pinned_facts` 原样透传，**压缩器够不着** |
| 产物只传引用 | `artifact_refs` 渲染成短索引消息，**不带正文** |
| 有限长窗口 | 只取传入的 `对话`（调用方给多少就是多少，不在此追加） |
| 早期摘要替代 | `已被压缩摘要` 原样透传，组装时不重算 |

**「组装时不做窗口裁剪」是有意的**：窗口多长是编排层的决定
（它知道哪些轮次还有效），composer 若擅自裁剪就成了隐藏的策略点。
"""

from __future__ import annotations

from ...ports.llm import Message
from .view import ContextView


def 组装(
    *,
    系统与角色: tuple[Message, ...],
    绝不丢弃: tuple[Message, ...] = (),
    产物引用: tuple[str, ...] = (),
    对话: tuple[Message, ...] = (),
    已被压缩摘要: Message | None = None,
) -> ContextView:
    """拼出一个视图。**纯函数**：同样输入必得同样视图。"""
    return ContextView(
        system_and_role=系统与角色,
        pinned_facts=绝不丢弃,
        artifact_refs=_渲染产物引用(产物引用),
        recent_window=对话,
        summary_of_dropped=已被压缩摘要,
    )


def _渲染产物引用(产物: tuple[str, ...]) -> tuple[Message, ...]:
    """把产物索引渲染成消息。**只传路径与名称，绝不带正文。**

    「只传引用」是硬要求（D-030/D-072）：
    正文全文一旦进上下文，几轮之后窗口就被产物撑满了，
    而产物可能有好几份（大纲 + 初稿 + 修正稿）。
    要看正文时模型用工具去读，那才是正路。
    """
    if not 产物:
        return ()
    清单 = "\n".join(f"- {项}" for 项 in 产物)
    return (
        Message(
            role="system",
            content=(
                f"本次可用的产物（共 {len(产物)} 份，均为**引用**，正文需用工具读取）：\n{清单}"
            ),
        ),
    )
