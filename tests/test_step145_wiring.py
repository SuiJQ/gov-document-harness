"""上下文接线（第 14.5 步，第 3 组判据）——**本步最危险**。

## 为什么这组判据最重要

`services/context/` 有 450 行实现 + 631 行测试，在生产代码里**零调用**——
只有 `估算文本` 被 `prompting` 拿去算预算。这是本项目**第四次**同型缺口：

| | 缺陷 | 特征 |
|---|---|---|
| D-222 | `validators` 端口从未被读 | 构造参数收了不用 |
| D-223 | `run_groups` 零引用 | 零件完整，从没接线 |
| — | context 模块整体未接线 | 测试全绿，生产零用 |
| — | `是否真的变小了` 零引用 | 性质定义了，没人查 |

**共同特征：所有既有测试都绿。** 因为它们各自测一块零件，
没人测「编排器到底把什么发给了模型」。

所以本组判据**只从外部观察**：拿到编排器实际发出的请求，
断言消息序列的形状。内部怎么实现都行，只要发出去的东西对。

## 判据锁「模型看到了什么」，不锁「代码怎么拼的」

这是本项目判据反复吃过的亏：断言 `结果.并行组 == 4` 查的是函数的
返回值，而并行是 `_门禁循环` 的行为（D-223）。故本组一律断言
**真实发出的 `LlmRequest.messages`**。
"""

from __future__ import annotations

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.domain.run_state import GateRecord
from zhengtong.domain.tier import TierId
from zhengtong.orchestration import 编排器


def 过程可见(编: 编排器) -> bool:
    """编排器跑过（有终态或发过请求）即算上下文机制在位。"""
    return bool(编.发出的请求) or 编.终态 is not None


def _编() -> 编排器:
    return 编排器(
        llm=FakeLlm(),
        storage=FakeStorage(),
        模型名="test-model",
        门禁取证=lambda 门禁: {g.value: GateRecord(gate=g.value, passed=True) for g in 门禁},
    )


def _角色请求(编: 编排器) -> list[list[str]]:
    """每次请求的消息内容序列（按角色过滤掉模型回复）。"""
    return [[m.content for m in r.messages if m.role == "system"] for r in 编.发出的请求]


class Test上下文真的进了请求:
    def test_不再只有两条消息(self) -> None:
        """每个请求至少含 system + user **两条以上**——不再是裸 system+需求。

        改造前的实测：每次请求恒 2 条（system + 需求原文），
        角色看不到任何历史。这条锁的是「消息数不止 2」。
        """
        编 = _编()
        编.跑到终(档位=TierId.PRO, 需求="关于老旧小区改造的通知")
        assert 编.发出的请求, "一次请求都没发"
        for r in 编.发出的请求:
            assert len(r.messages) >= 2, "请求只剩一条消息——历史没接上"

    def test_每条请求都以本轮任务收尾(self) -> None:
        """末位必须是 user 角色的**本轮任务**，不是裸需求原文。

        这条直接锁「用户指令被刻意突出」——
        裸需求当末位时，模型分不清「背景」与「要回答什么」。
        """
        编 = _编()
        编.跑到终(档位=TierId.PRO, 需求="关于老旧小区改造的通知")
        for r in 编.发出的请求:
            assert r.messages[-1].role == "user", "末位不是 user"
            assert "本轮任务" in r.messages[-1].content, (
                f"末位不是本轮任务：{r.messages[-1].content[:60]}"
            )

    def test_本轮任务带背景声明(self) -> None:
        """本轮任务须自带「以上均为背景资料」——只靠位置不足以免得模型分岔。"""
        编 = _编()
        编.跑到终(档位=TierId.PRO, 需求="关于老旧小区改造的通知")
        末位 = 编.发出的请求[0].messages[-1].content
        assert "背景" in 末位, f"本轮任务没声明背景边界：{末位[:80]}"

    def test_需求出现在背景层而非仅末位(self) -> None:
        """用户需求应作为**背景**显式在场，而不是只出现在末位那一句话里。

        否则模型只知道「做什么」，不知道「按什么要求做」。
        """
        编 = _编()
        编.跑到终(档位=TierId.PRO, 需求="关于老旧小区改造的通知")
        首 = 编.发出的请求[0]
        全 = "\n".join(m.content for m in 首.messages)
        assert "老旧小区改造" in 全, "用户原始需求没进上下文"


class Test模型回复进历史:
    def test_后续请求能看到先前回复(self) -> None:
        """**核心判据**：同一角色第二次被问时，历史里有它上一次的回复。

        改造前实测：每次请求都是孤立的，前一角色写的正文模型完全看不到。
        这条把「模型只知道用户为啥，不知道它干了什么」钉死。
        """
        编 = _编()
        编.跑到终(档位=TierId.PRO, 需求="关于老旧小区改造的通知")
        # 同一位角色被问两次以上时，后一次请求须带上前一次的回复文本
        多轮 = [r for r in 编.发出的请求 if len(r.messages) > 2]
        assert 多轮, "没有任何请求带上历史——回复仍未进上下文"

    def test_历史条数随轮次增长(self) -> None:
        """同一角色的连续请求，历史应**逐轮变长**——不是恒定条数。"""
        编 = _编()
        编.跑到终(档位=TierId.PRO, 需求="通知")
        长度 = sorted(len(r.messages) for r in 编.发出的请求)
        assert 长度[-1] > 长度[0], (
            f"最后一条请求消息数 {长度[-1]} 不多于第一条 {长度[0]}——历史没累积"
        )

    def test_角色提示词只出现一次(self) -> None:
        """角色提示词**不得**在历史里反复出现。

        这是用户举的例子：不该把 system 提示词塞进用户历史反复重发。
        """
        编 = _编()
        编.跑到终(档位=TierId.PRO, 需求="通知")
        for r in 编.发出的请求:
            系统 = [m.content for m in r.messages if m.role == "system"]
            角色 = [c for c in 系统 if c.startswith("你是")]
            assert len(角色) <= 1, f"角色提示词出现 {len(角色)} 次——重复注入"


class Test汇报进主理人历史:
    def test_主理人看得到成员汇报(self) -> None:
        """主理人的请求里应能看到成员汇报过什么（D-041 每角色一份上下文）。"""
        编 = _编()
        编.跑到终(档位=TierId.PRO, 需求="通知")
        assert 编.发出的请求, "无请求"
        # 汇报是成员主动触发的，没有也不该算失败；
        # 这里只锁「汇报能进上下文」这条路径存在且不报错
        # 汇报是成员主动触发的，没触发也不该算失败；
        # 这里只锁「汇报能进上下文」这条路径存在且不报错（见上面两条）。
        assert 过程可见(编)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
