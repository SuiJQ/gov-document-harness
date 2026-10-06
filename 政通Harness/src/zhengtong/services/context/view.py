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
from .estimate import 估算文本


@dataclass(frozen=True, slots=True)
class ContextView:
    """一次模型调用要看到的全部上下文。

    五字段全 `tuple`，唯一可空的是 `summary_of_dropped`（还没丢过东西时为 None）。
    """

    #: ① 角色提示词（内容包，只读）。**全角色全阶段不变**，压缩不碰它。
    system_and_role: tuple[Message, ...] = ()
    #: ② 动态规则层：按需装载的规则条目 + D-Card + 产物契约。
    #: **第 14.5 步从 `system_and_role` 拆出**——原先它与角色提示词
    #: 拼在同一条 message 里，模型无从分辨「恒定规矩」与「本轮情况」。
    dynamic_rules: tuple[Message, ...] = ()
    #: ⑤ 工具清单（本次实际可调子集）。
    #: **刻意放在 `messages()` 的靠后位置**：它随角色变（5 个角色 5 份），
    #: 若前置则每换角色就击穿后面全部缓存前缀。
    #: 前缀缓存只匹配**从头开始的连续前缀**，故位置直接决定命中率。
    tool_manifest: tuple[Message, ...] = ()
    #: D-039「绝不丢弃」四类。**结构上就丢不掉**——
    #: 压缩器只能动 `recent_window`，动不了这一栏。
    pinned_facts: tuple[Message, ...] = ()
    #: 产物只传引用（D-030/D-072）：正文全文不进上下文。
    artifact_refs: tuple[Message, ...] = ()
    #: ③ 历史：工具动作 + 模型回复 + 汇报。**唯一会被压缩动的一栏。**
    recent_window: tuple[Message, ...] = ()
    #: 被丢弃的早期讨论之摘要。为 None 表示还没发生过压缩。
    summary_of_dropped: Message | None = None
    #: ⑦ 本轮任务（用户指令），**恒为最后一条**。
    #: 刻意与历史里的用户消息分开：历史是「背景」，本轮任务是「要回答的」。
    #: 单独成字段而非混进 `recent_window`，是为了让这个位置**结构上**
    #: 就固定——一旦混进历史，滑动窗口就可能把它挤到中间去。
    本轮任务: Message | None = None
    #: ⑧ 门控记忆门（D-254）：**不是压缩，是导航**。
    #: 零阈值，始终在——压缩器只动 `recent_window`，够不着这一栏。
    #: 存 `(优先级标签, 内容)`，渲染时放在 `本轮任务` **之前**
    #: （U 型曲线：导航在倒数第二，指令在末尾）。
    #: 默认 `()`——不传就是空门，旧行为逐字不变。
    记忆门: tuple[tuple[str, str], ...] = ()

    def messages(self) -> tuple[Message, ...]:
        """按**缓存分层**组装成模型消息序列（第 14.5 步重排）。

        ```
        ① 角色提示词        ┐ 不随轮次变 → 缓存段 A
        ② 动态规则层        ┘
        ③ pinned_facts      ┐
        ④ 产物引用          ┘ 缓存段 B
        ⑤ 历史              （工具动作 + 回复 + 汇报）
        ⑥ 工具清单          ┐ 每轮变 → 缓存段 C
        ⑦ 本轮任务（末位）  ┘
        ```

        ## 为什么这么排

        **前缀缓存只匹配从头开始的连续前缀**，所以排序直接决定命中率：
        不动的靠前，动的靠后。原先 `tool_manifest` 若与规则同段，
        每换角色就击穿其后的全部缓存。

        **为什么本轮任务压到最末**：模型的指令跟随对**近端**更敏感，
        末位能让「这轮要回答什么」压过「背景资料是什么」。
        本轮任务写成祈使句并自带一句「以上均为背景资料」，
        是为了让这个近端优势真的兑现，而不是仅仅位置靠后。

        **为什么摘要排在历史之前**：摘要是被丢掉的那段**更早**的内容，
        摘要在前、保留的近期窗口在后，才符合时间线。若摘要放最后，
        模型会先读到近期细节再读到「这些之前的都被压缩了」，
        容易误以为近期内容就是全部。

        **产物引用夹在摘要与历史之间**：它不是时间线上的内容，
        而是横切各阶段的产物索引，放在两者之间让模型在读历史时
        仍能看见「有哪些产物可查」。

        **记忆门放在本轮任务之前**（U 型曲线）：它与本轮任务同属
        「可变 + 最重要」，但本轮任务是**指令**（要回答什么），
        记忆门是**导航**（读什么）。指令压过导航——
        末尾最后一段必须是本轮任务，否则 recency bias 会浪费在
        「读什么」上而不是「回答什么」。
        """
        摘要 = () if self.summary_of_dropped is None else (self.summary_of_dropped,)
        # **记忆门不参与压缩**：压缩器只动 `recent_window`，
        # 这一栏物理上够不着，所以永远完整。
        记忆门 = ()
        if self.记忆门:
            清单 = "\n".join(f"- [{标签}] {内容}" for 标签, 内容 in self.记忆门)
            记忆门 = (Message(role="system", content=f"【重点导航】\n{清单}"),)
        return (
            *self.system_and_role,
            *self.dynamic_rules,
            *self.pinned_facts,
            *摘要,
            *self.artifact_refs,
            *self.recent_window,
            *self.tool_manifest,
            *记忆门,
            # 本轮任务至多一条，故用条件元组而非裸 * ——
            # `*Message` 会抛「Value after * must be an iterable」。
            *(() if self.本轮任务 is None else (self.本轮任务,)),
        )


def 估算上下文层(视图: ContextView) -> int:
    """只算**压缩器管得到的那一层**：。

    ## 为什么单独拆一个函数（而不是复用 ）

     算的是**总 token**（含提示词层与本轮任务），
    而那两层**永不压缩**。用总量做触发判定，会出现两种反噬：

    - 提示词层很大时，历史才用了 10% 就触发压缩——压完总 token 没降
      （提示词层没动）， 恒返回 True，反复压同一份视图；
    - 反过来，固定开销吃掉大半容量时，历史已经没空间了，
      但总量还没到 85%，触发线迟迟不来。

    故触发判定必须看**上下文层自己的配额**，而不是总量。

    ## 配额怎么算



    固定层越大，配额越小，触发越早——产物越积越多时触发线自然前移，
    不用另写逻辑。 虽然物理上够不着，但它是**固定占用**，
    应从配额里扣（它挤占了历史的空间）。
    """
    摘要 = () if 视图.summary_of_dropped is None else (视图.summary_of_dropped,)
    return sum(
        估算文本(m.content)
        for m in (*视图.pinned_facts, *视图.recent_window, *摘要)
    )


def 估算视图(view: ContextView) -> int:
    """估算一个视图的 token 合计。

    **只算真正会发给模型的消息**（走 `messages()`），
    不另算一遍字段——两处算法必然分叉，而分叉出来的数字没人会发现。
    """
    from .estimate import 估算文本

    return sum(估算文本(m.content) for m in view.messages())
