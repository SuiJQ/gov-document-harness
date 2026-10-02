"""压缩按 token 预算保留，不按条数（第 14.5 步，第 2 组判据）。

## 为什么必须改

现有实现是 `要丢 = len(窗口) - 保留条数`，条数与体积**完全脱钩**。
实测：8 条 558 token 的长消息，容量 2000 / 阈值 1700，
压缩后仍 **3984 token**——超限 2.3 倍，而压缩器已经「成功」返回了。

调用方拿到的是「压完了」，于是照发一个**必然被服务端拒绝**的请求。

反过来 40 条 10 token 的短消息会丢 34 条，压到远低于阈值——
**白丢上下文**。同样「保留 6 条」，体积能差 50 倍。

条数在任何语言里都不是体积的可靠代理。Codex 用
`TruncationPolicy::Tokens(remaining)` 逐条扣减（见 docs/CONTEXT_RESEARCH.md）。

## 判据怎么写才算数

不能只断言「压完变小了」——那是现有 `是否真的变小了` 在做的事，
它**只查变小、不查压到阈值内**，所以超限场景一路绿灯（生产还零引用）。

故本组断言两件更强的事：
1. 压完**装得下**（不是只变小）
2. 装不下时**如实报装不下**，而不是谎称压完
"""

from __future__ import annotations

import pytest

from zhengtong.ports.llm import Message
from zhengtong.services.context import ContextView, 压缩, 触发阈值
from zhengtong.services.context.view import 估算视图

_容量 = 2000


def _长消息(n: int, *, 长度: int = 30) -> tuple[Message, ...]:
    词 = "这是一段比较长的公文正文内容用来测试按条数保留会不会爆预算。" * 长度
    return tuple(
        Message(role="user" if i % 2 else "assistant", content=f"{i}：{词}") for i in range(n)
    )


def _摘要器(内容: tuple[Message, ...]) -> Message:
    return Message(role="assistant", content="【早期讨论摘要】略。")


class Test按token预算保留:
    def test_压完必须装得下(self) -> None:
        """压完的视图必须**装得下**——不是只变小。"""
        视图 = ContextView(recent_window=_长消息(8))
        结果 = 压缩(视图, 上下文容量=_容量, 摘要器=_摘要器)
        assert 结果 is not None, "超阈值却没触发压缩"
        assert 结果.压缩后token <= 触发阈值(_容量), (
            f"压完 {结果.压缩后token} token，仍超阈值 {触发阈值(_容量)}"
            f"（原 {结果.压缩前token}）——压缩谎称成功"
        )

    def test_装得下就停手不白丢(self) -> None:
        """总token 本来就装得下时，一条都不该丢。"""
        视图 = ContextView(recent_window=_长消息(3, 长度=2))
        assert 估算视图(视图) < 触发阈值(_容量)
        结果 = 压缩(视图, 上下文容量=_容量, 摘要器=_摘要器)
        assert 结果 is None, "没超阈值却压了"

    def test_短消息不被过度丢弃(self) -> None:
        """体积刚超一点时，只该丢**够用**的那些，不该按条数丢 34 条。

        这是「按条数」的另一半病症：**白丢**。
        """
        短 = tuple(Message(role="user", content="改一下第三段。") for _ in range(40))
        视图 = ContextView(recent_window=短)
        总 = 估算视图(视图)
        # 每条 3 token、40 条共 120；容量 130 → 阈值 110。
        # **期望按新语义算**（含摘要自身的开销，不是我最初以为的「只丢三四条」）：
        # 110 − 120 − 摘要预留 ≈ 差 37 token，每条 3 → 13 条。
        # 按条数则要丢 34 条——那才是病症所在。
        结果 = 压缩(视图, 上下文容量=130, 摘要器=_摘要器)
        assert 结果 is not None, f"总 {总} token / 阈值 110，本该触发压缩却没压"
        assert 结果.丢弃条数 <= 15, f"丢了 {结果.丢弃条数}/40 条——比按预算需要的多丢"
        assert 结果.压缩后token <= 触发阈值(130), "压完仍超限"


class Test装不下要如实报:
    def test_不可丢部分本身超限时报错(self) -> None:
        """恒定段本身就超限时，**不能谎称压完**——须区分「不需要压」与「压不动」。

        两种情况现在都返回 `None`，调用方无从分辨，
        于是会把一个注定被拒的请求发了出去。
        """
        巨 = Message(role="system", content="角色提示词。" * 2000)
        视图 = ContextView(system_and_role=(巨,), recent_window=_长消息(2, 长度=5))
        from zhengtong.services.context import 需要压缩

        结果 = 压缩(视图, 上下文容量=_容量, 摘要器=_摘要器)
        assert 结果 is None, "不可丢的部分已超限却谎称压完"
        # 压缩不动了，但**确实超限**——这两个事实必须能被调用方区分开
        assert 需要压缩(视图, 上下文容量=_容量), "调用方无从知道装不下"


class Test预算扣减口径:
    def test_预算只留给历史(self) -> None:
        """历史可用预算 = 阈值 − 恒定段占用。

        恒定段（角色提示词/规则/工具清单）占掉的额度**不能**再分给历史，
        否则历史会被挤到真的装不下。
        """
        视图 = ContextView(
            system_and_role=(Message(role="system", content="角色。" * 20),),
            recent_window=_长消息(10),
        )
        结果 = 压缩(视图, 上下文容量=_容量, 摘要器=_摘要器)
        assert 结果 is not None
        assert 结果.压缩后token <= 触发阈值(_容量)

    def test_保留的是最近的(self) -> None:
        """超预算时被丢的应是**最旧**的，保留最近讨论。"""
        视图 = ContextView(recent_window=_长消息(20, 长度=6))
        结果 = 压缩(视图, 上下文容量=_容量, 摘要器=_摘要器)
        assert 结果 is not None
        保留内容 = [m.content for m in 结果.视图.recent_window]
        assert 保留内容, "全被丢光了"
        # 最后一条一定保留（编号最大）
        assert 保留内容[-1].startswith("19："), f"保留的不是最近的：{保留内容[-1][:20]}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
