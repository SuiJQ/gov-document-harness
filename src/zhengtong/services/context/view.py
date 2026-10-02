"""上下文视图。**上下文是一个「视图」，不是「累积的消息列表」**（架构 §7.2）。

## 五个手段 = 五个字段

D-025 承诺「五个主动手段」但 §7.2 未逐条枚举。
本实现**以 §7.2 的五个字段为准**，每个字段即一个手段：

| 字段 | 手段 | 作用 |
|---|---|---|
| `system_and_role` | 系统与角色提示恒在 | 每回合都在，不受压缩影响 |
| `pinned_facts` | 绝不丢弃（D-039） | 结构上就不会被丢，不靠算法记得 |
| `artifact_refs` | 产物只传引用 | 全文不进上下文（D-030/D-072） |
| `recent_window` | 有限长窗口 | 只带最近 N 条，早期靠摘要 |
| `summary_of_dropped` | 早期讨论摘要替代 | 丢过程不丢结论 |

## 为什么全部用 `tuple`

视图必须**真正不可变**：若装 `deque`/`list`，可变状态会穿透 `frozen` 壳，
hash 也不稳定，而下面三个卖点全都建立在「视图是值」上：

1. **压缩 = 改视图，不是破坏性删除** —— 视图切换可逆、可审计、可回放。
2. **压缩这件事本身可观测** —— 切换时能报「保留了 X/Y/Z」。
3. **不会累积出病态历史** —— 根本没有「追加到列表」这个动作。

窗口滑动这种可变动作放在**生成视图的 composer/compactor** 里，
不放在视图本身：读旧视图、算出新 `tuple` 视图返回，视图自身始终是常量。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...ports.llm import Message


@dataclass(frozen=True, slots=True)
class ContextView:
    """一次模型调用要看到的全部上下文。

    五字段全 `tuple`，唯一可空的是 `summary_of_dropped`（还没丢过东西时为 None）。
    """

    #: 系统提示 + 角色提示。**恒在**，压缩不碰它。
    system_and_role: tuple[Message, ...] = ()
    #: D-039「绝不丢弃」四类。**结构上就丢不掉**——
    #: 压缩器只能动 `recent_window`，动不了这一栏。
    pinned_facts: tuple[Message, ...] = ()
    #: 产物只传引用（D-030/D-072）：正文全文不进上下文。
    artifact_refs: tuple[Message, ...] = ()
    #: 最近若干条。**唯一会被压缩动的一栏。**
    recent_window: tuple[Message, ...] = ()
    #: 被丢弃的早期讨论之摘要。为 None 表示还没发生过压缩。
    summary_of_dropped: Message | None = None

    def messages(self) -> tuple[Message, ...]:
        """按**时间与重要性**顺序组装成模型消息序列。

        顺序为：`system_and_role → pinned_facts → summary_of_dropped →
        artifact_refs → recent_window`。

        **为什么摘要排在 `recent_window` 之前**（而非 §7.2 字段声明的顺序）：
        摘要是被丢掉的那段**更早**的内容，摘要在前、保留的近期窗口在后，
        才符合时间线。若把摘要放最后，模型会先读到近期细节再读到
        「这些之前的都被压缩了」，容易误以为近期内容就是全部。

        **`artifact_refs` 夹在摘要与近期窗口之间**：它不是时间线上的内容，
        而是横切各阶段的产物索引，放在两者之间让模型在读近期对话时
        仍能看见「有哪些产物可查」。
        """
        摘要 = () if self.summary_of_dropped is None else (self.summary_of_dropped,)
        return (
            *self.system_and_role,
            *self.pinned_facts,
            *摘要,
            *self.artifact_refs,
            *self.recent_window,
        )


def 估算视图(view: ContextView) -> int:
    """估算一个视图的 token 合计。

    **只算真正会发给模型的消息**（走 `messages()`），
    不另算一遍字段——两处算法必然分叉，而分叉出来的数字没人会发现。
    """
    from .estimate import 估算文本

    return sum(估算文本(m.content) for m in view.messages())
