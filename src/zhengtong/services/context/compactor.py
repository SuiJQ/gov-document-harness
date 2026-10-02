"""压缩：85% 触发 + D-039 保留优先级。

## 触发与处置是两回事

- **触发**只看估算 token 与 85% 阈值（D-025）。
- **处置**按 D-039 的保留优先级，从后往前丢。

## 摘要为什么是注入的

摘要需要一次模型调用（要真正读过才可能总结）。
本模块**不自己调模型**，而是接一个 `摘要器` 函数进来：

- 传了 → 用真摘要
- 没传 → **不编造摘要**，而是把被丢弃内容的原始片段拼进去，
  并在开头**标明这是未摘要的原文**。

理由是「严禁编造」是本项目最高数据纪律（D-034）。
一个看起来像摘要、实则机器拼的字符串，会让模型以为
「这些早期讨论已经被理解过了」——那比明说「以下是原文」危险得多。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...ports.llm import Message
from .estimate import 估算文本, 汉字除数, 触发阈值
from .view import ContextView

#: 摘要器：把被丢弃的消息总结成一条。**由调用方注入**，本模块不调模型。
摘要器 = Callable[[tuple[Message, ...]], Message]

#: 压缩后 `recent_window` 保留的条数。
#: 取 6 是因为公文一轮「讨论→修改」通常在 6 轮内收敛；
#: 再少会丢掉当前正在改的句子，再多会挤掉早期结论。
默认保留条数 = 6

#: **没有摘要器时**，兜底原文的 token 上限。
#:
#: 不设上限会让压缩越压越大（实测见 `_做摘要` 说明），
#: 故兜底必须比「被丢掉的东西」小一个量级，否则压缩失去意义。
未摘要原文上限token = 600

#: 摘要的标记前缀（Codex 的 `SUMMARY_PREFIX` 同型做法）。
#:
#: `ContextView.summary_of_dropped` 已经是独立字段，
#: **下游按字段就能区分**摘要与真实历史——这是结构带来的。
#: 但 `messages()` 会把五栏拍平成一个消息序列发给模型，
#: **模型看到的是拍平后的结果**：那里摘要与真实对话长得一样，
#: 模型可能以为「历史就这么多」，从而误判当前局面。
#:
#: 故凡摘要一律带此前缀，让模型自己也能分辨。
摘要前缀 = "【早期讨论摘要】"


@dataclass(frozen=True, slots=True)
class 压缩结果:
    """一次压缩的结果。**可观测**（§7.2：压缩本身要能被看见）。"""

    视图: ContextView
    丢弃条数: int
    压缩前token: int
    压缩后token: int
    摘要token: int

    @property
    def 是否真的变小了(self) -> bool:
        """压缩必须真的减小体积。

        若摘要本身比丢掉的内容还大（长对话 + 短摘要器），
        压缩就是纯亏——白丢上下文还占更多位置。
        这个判断让调用方能发现「压缩策略失效」而不是继续压。
        """
        return self.压缩后token < self.压缩前token

    @property
    def 保留概览(self) -> dict[str, int]:
        """「保留了 X / Y / Z」（§7.2 要求压缩可观测）。"""
        return {
            "系统与角色": len(self.视图.system_and_role),
            "绝不丢弃": len(self.视图.pinned_facts),
            "产物引用": len(self.视图.artifact_refs),
            "近期窗口": len(self.视图.recent_window),
            "已摘要": 0 if self.视图.summary_of_dropped is None else 1,
        }


def 需要压缩(视图: ContextView, *, 上下文容量: int) -> bool:
    """是否已达到 85% 兜底阈值。"""
    from .view import 估算视图

    return 估算视图(视图) >= 触发阈值(上下文容量)


def _丢弃顺序(窗口: tuple[Message, ...]) -> list[int]:
    """该丢哪些消息，返回下标，**从先丢到后丢**。

    ## 为什么要分优先级（L-1）

    原实现按位置切前 N 条，**不区分消息种类**。
    材料经工具读入后是 `role="tool"` 的消息，与当前对话混在同一个窗口里，
    被同一个杠杆无差别丢弃。

    于是会出现这种情况：材料占八成体积，被丢的八成里大部分是刚读完、
    正要用的材料，而真正该丢的早期讨论只占两成却毫发无伤。
    **「压完变小」不等于「压得对」**——前者是体积问题，后者是取舍问题。

    ## 排序规则

    - `role="tool"` 优先丢（材料可重新读，对话不可重放）
    - 同类内按原序（`enumerate` 下标）——**越早越先丢**
    - `system` 不出现在 `recent_window` 里（它在 `system_and_role`，
      压缩器够不着），故不必单列

    用 `(级别, 原下标)` 做键而不是自定义比较器：
    Python 的元组比较先比级别再比下标，一行说清两条规则。
    """
    带级 = [(0 if m.role == "tool" else 1, i) for i, m in enumerate(窗口)]
    带级.sort()
    return [i for _, i in 带级]


def 压缩(
    视图: ContextView,
    *,
    上下文容量: int,
    保留条数: int = 默认保留条数,
    摘要器: 摘要器 | None = None,
) -> 压缩结果 | None:
    """压一次。**返回 None 表示不需要压**（调用方据 None 决定跳过）。

    ## 保留优先级（D-039，从后往前丢）

    | 级别 | 内容 | 本实现 |
    |---|---|---|
    | 绝不丢弃 | 方向确认单、用户明示事实与禁止事项、硬约束 D7、已发现的严重问题 | `pinned_facts`，**压缩器动不了** |
    | 尽量保留 | 文章大纲、已写好的正文段落 | 在 `recent_window` 尾部，被优先保留 |
    | 可摘要替代 | 早期讨论过程、工具调用原始返回、参考材料原文 | 超出窗口部分进摘要 |

    ## 关键性质：`pinned_facts` 物理上不可丢

    本函数只重建 `recent_window` 与 `summary_of_dropped`，
    `pinned_facts` / `system_and_role` / `artifact_refs` 原样透传。
    所以「绝不丢弃」不是靠压缩算法**记得**保留，
    而是**结构上就够不着**——这正是 §7.2 的原话。

    ## 丢弃按优先级，不按位置（L-1）

    `role="tool"`（材料）先于对话被丢，同类内越早越先丢。
    保留的因此**不一定是尾部连续一段**——这是有意的：
    要保住「正在进行的讨论」，就得接受丢掉一条更早的材料。
    """
    from .view import 估算视图

    压缩前 = 估算视图(视图)
    if 压缩前 < 触发阈值(上下文容量):
        return None

    窗口 = 视图.recent_window
    要丢 = len(窗口) - max(保留条数, 0)

    if 要丢 <= 0:
        # 没有可丢的了还不达标 → 不可丢的部分本身太大。
        # 此时返回 None 而非空转：反复压同样一份视图毫无意义，
        # 调用方应据 None 去报「窗口装不下」而非继续重试。
        return None

    丢弃下标 = set(_丢弃顺序(窗口)[:要丢])
    丢弃 = tuple(m for i, m in enumerate(窗口) if i in 丢弃下标)
    保留 = tuple(m for i, m in enumerate(窗口) if i not in 丢弃下标)

    摘要 = _做摘要(丢弃, 摘要器)
    新视图 = ContextView(
        system_and_role=视图.system_and_role,
        pinned_facts=视图.pinned_facts,
        artifact_refs=视图.artifact_refs,
        recent_window=保留,
        summary_of_dropped=摘要,
    )
    压缩后 = 估算视图(新视图)
    return 压缩结果(
        视图=新视图,
        丢弃条数=len(丢弃),
        压缩前token=压缩前,
        压缩后token=压缩后,
        摘要token=估算文本(摘要.content),
    )


def _做摘要(丢弃: tuple[Message, ...], 摘要器: 摘要器 | None) -> Message:
    """有摘要器就用；没有就**明说这是原文**，不假装总结过。

    ## 兜底为什么必须有界（实测教训）

    第一版兜底把**全部**原文塞进摘要，结果是
    **压缩后比压缩前还大**——实测 40 条对话、
    丢 38 条留 2 条时，10,072 → 10,116 token。
    原因是摘要 token（9,544）几乎等于被丢掉的总量：
    把原文整体搬进摘要，等于「把窗口填满之后再加一段原文」，
    压缩不但没干活，还让下一轮更早触顶。

    故兜底取**有界原文**：标明未经摘要、标明是否截断，然后截到上限。
    诚实与可用不冲突——标了「未摘要且已截断」就既没说谎，也没失控。
    """
    if 摘要器 is not None:
        return Message(role="system", content=f"{摘要前缀}{_构成说明(丢弃)}{摘要器(丢弃).content}")
    原文 = "\n".join(m.content for m in 丢弃)
    上限字符 = int(未摘要原文上限token * 汉字除数)
    已截断 = len(原文) > 上限字符
    if 已截断:
        原文 = 原文[:上限字符]
    标注 = "未经摘要，且以下原文已截断" if 已截断 else "未经摘要"
    return Message(
        role="system",
        content=f"{摘要前缀}【以下为已被移出上下文的早期讨论原文，{标注}】\n"
        f"{_构成说明(丢弃)}\n"
        f"原文如下，请自行阅读：\n{原文}",
    )


def _构成说明(丢弃: tuple[Message, ...]) -> str:
    """报出被丢的是什么构成。**判据第 3 条：无论谁被丢，必须标明。**

    只报「共 N 条」不够——模型无从知道丢掉的到底是材料还是对话。
    两者对它含义不同：材料可以重读，对话丢了就没了。
    「标明」的最低标准，是让模型能据此判断自己手上还剩什么。
    """
    工具数 = sum(1 for m in 丢弃 if m.role == "tool")
    对话数 = len(丢弃) - 工具数
    段 = [f"共 {len(丢弃)} 条"]
    if 工具数:
        段.append(f"其中工具返回 {工具数} 条")
    if 对话数:
        段.append(f"对话 {对话数} 条")
    return "（" + "，".join(段) + "）"
