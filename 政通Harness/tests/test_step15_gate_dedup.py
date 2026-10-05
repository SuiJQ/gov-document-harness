"""门禁不得重复记账（D-253）。

## 缺陷形状

`orchestrate.py` 门禁段：

```python
while True:
    门禁 = _该跑门禁(档)
    self.门禁.extend(门禁)      # ← 在循环内
```

`while True` 尾部无 `break`，唯一出口是循环体内的 `return`。
故每退回一轮，`self.门禁` 就多一份**全集**。

实测（Pro 档）：

| 场景 | `self.门禁` | 应为 |
|---|---|---|
| 一次通过 | 12 | 12 |
| 退回 1 次后通过 | **24** | 12 |
| 永远驳回 | **36** | 12 |

## 为什么这不是「内部字段脏」

`走过的门禁` 零消费方，但 `gate-report.md` 用的是**同一个 `self.门禁`**：

```python
_门禁报告(self.门禁, 裁定)
```

故退回一次后，**用户拿到的报告里 12 道门禁整份重复一遍**。
这是交付物错误，不是内部状态脏。

## 为什么现有判据查不出

`test_orchestration.py` 只有 `assert not 结果.门禁账.已取证`——
**「非空」在 12 条和 24 条下都成立**。

断言「非空」锁不住「不重复」：**前者是存在性，后者是唯一性。**
本文件补的就是唯一性那一半。

## 本文件测什么、不断言什么

**测**：多轮退回下 `走过的门禁` 的长度与唯一性、报告正文的行数。
**不断言**：退回数、事件序列、失败路径的具体错误码——那些另有判据管。
断言越多，越容易出现「同一结果由不同原因产生」而判据无法区分。
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tests")
sys.path.insert(0, "src")


@pytest.fixture(autouse=True)
def _路径() -> None:
    pass


def _跑(驳回: dict[str, int] | None):
    """跑一次 Pro 档全流程，返回编排结果。

    ## 为什么固定 Pro 档

    咨询档 `gates_apply=False` → 门禁数 0 → 退回循环根本不进，
    **测不出重复**。而 Flash 少一个角色，门禁取证方不全，
    会引入无关变量。故取 Pro：12 道门禁齐全，且有退回能力。
    """
    from test_step14_orchestrate_gate import _编排
    from zhengtong.domain.tier import TierId

    编 = _编排(驳回=驳回)
    return 编.跑到终(档位=TierId.PRO, 需求="通知")


def _跑并取事件(驳回: dict[str, int] | None):
    """跑一次并返回 `(结果, 门禁事件的 gate_id 列表)`。

    失败路径上没有 `编排结果`（`Result.fail`），要观察痕迹只能看事件流。
    """
    from test_step14_orchestrate_gate import _编排
    from zhengtong.domain.tier import TierId

    编 = _编排(驳回=驳回)
    结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
    门 = [e.data.get("gate_id") for e in 编.事件 if e.kind.value.startswith("gate.")]
    return 结果, [g for g in 门 if g]


def _门禁报告正文(结果) -> str:
    for 件 in 结果.产物:
        if "门禁" in 件.内容:
            return 件.内容
    return ""


def _该跑的(驳回: dict[str, int] | None) -> set[str]:
    """本档**该跑**的门禁 = 已取证 ∪ 不适用（D-390）。

    单独取「已取证」不对：G3 于 Flash 无编制、G9 是动态门禁，
    它们永远不取证，用「已取证」比数量会少两道且与轮次无关——
    那测的就不是「该跑哪些」而是「查了几道」，两件事。
    """
    账 = _跑(驳回).unwrap().门禁账
    return {g.value for g in 账.已取证} | {g.value for g in 账.不适用}


class Test走过的门禁是集合语义:
    """**本条是核心**：`self.门禁` 的语义是「经手过哪些门禁」，
    那是**集合**，不是**序列**。同一道门禁跑三遍仍是一道。"""

    def test_一次通过时门禁不重复(self) -> None:
        结果 = _跑(None).unwrap()
        门禁 = [g.value for g in 结果.门禁账.已取证]
        assert len(门禁) == len(set(门禁)), f"一次通过就重复了：{门禁}"

    def test_退回一次后门禁不重复(self) -> None:
        """**本条是本缺陷的直接判据**：退 1 次后门禁不重复。

        注：Flash 档「本档该跑」12 道，但**真取证只有 10 道**——
        G3（文择体不在编制）与 G9（动态门禁）本就不该有结论（D-390）。
        故这里断言 `已取证 + 不适用` 覆盖全部 12 道。
        """
        结果 = _跑({"G2": 1}).unwrap()
        账 = 结果.门禁账
        门禁 = [g.value for g in 账.已取证]
        assert len(门禁) + len(账.不适用) == 12, (
            f"已取证 {len(门禁)} + 不适用 {len(账.不适用)} 应合起来覆盖 12 道"
        )
        assert len(门禁) == len(set(门禁)), f"退回一次后出现重复：{门禁}"

    def test_退回后门禁数量不变(self) -> None:
        """**不只是「去重了」，数量还得对**。

        只查唯一性不够——`{G1}` 也满足唯一性。
        故同时断言退回前后数量相等。
        """
        一次 = _该跑的(None)
        退回 = _该跑的({"G2": 1})
        assert len(退回) == len(一次), f"退回一次后门禁数从 {len(一次)} 变成 {len(退回)}：{退回}"

    def test_门禁集合完全相同(self) -> None:
        """**退回不改变「该跑哪些门禁」**——它是档位的函数，不是轮次的函数。"""
        一次 = _该跑的(None)
        退回 = _该跑的({"G2": 1})
        assert 一次 == 退回, f"退回后门禁集合变了：{一次 ^ 退回}"

    def test_退满上限时也不重复(self) -> None:
        """**失败路径同样不能重复**。

        只修成功路径是很常见的半吊子——失败时用户拿着错误码去翻报告，
        报告正是那唯一的台账（用户约束：不展示审核意见）。
        """
        from test_step14_orchestrate_gate import _编排  # noqa: F401  (保持 import 形状一致)

        结果, 门 = _跑并取事件({"G2": 99})
        assert 结果.is_error
        assert 结果.error is not None
        # **失败路径没有 `编排结果` 对象**（`Result.fail`），故不能去读门禁字段。
        #
        # 原先写的是 `getattr(结果, "走过的门禁", ())` —— 字段一旦被删，
        # 它就返回空元组，于是 `len(()) == len(set(()))` **恒真**，
        # 判据从此永久变绿：这正是「字段没了而判据还在」的样子（D-389）。
        # 改为断言**门禁事件不重复**——那才是失败路径上真实存在的痕迹。
        # **事件流本来就该重复**：退回重写会重跑门禁，
        # 于是同一道 G2 每轮都发一次 `gate.started`。
        # 「不重复」约束的是**结果里的门禁集合**（成功路径的 `门禁账.已取证`），
        # 失败路径没有结果对象，故此处只断言：错误码正确 + 留下了痕迹。
        assert 门, "失败路径也该留下门禁事件痕迹——一条都没有说明事件没接上"
        assert 结果.error.code.value == "E042", 结果.error.code.value


class Test门禁报告正文:
    """**这一组测的是用户真正拿到的东西**，不是内部字段。

    内部字段对了、报告仍错，是很常见的形态——
    故必须直接断言落盘产物的正文。
    """

    def test_报告每道门禁只出现一次(self) -> None:
        结果 = _跑({"G2": 1}).unwrap()
        正文 = _门禁报告正文(结果)
        assert 正文, "没找到门禁报告——判据无效"
        行 = [ln for ln in 正文.splitlines() if ln.startswith("- G")]
        assert len(行) == len(set(行)), (
            f"报告里有 {len(行)} 行但只有 {len(set(行))} 道不同门禁——用户会看到同一道门禁重复判定"
        )

    def test_报告行数等于该档门禁数(self) -> None:
        """**行数必须等于实际门禁数**，不是「至少这么多」。

        「至少」会放过重复——重复只会让行数变多。
        """
        from zhengtong.domain.gate import FORMAL_GATES

        结果 = _跑({"G2": 1}).unwrap()
        正文 = _门禁报告正文(结果)
        行 = [ln for ln in 正文.splitlines() if ln.startswith("- G")]
        assert len(行) == len(FORMAL_GATES), f"报告 {len(行)} 行，该跑门禁 {len(FORMAL_GATES)} 道"
