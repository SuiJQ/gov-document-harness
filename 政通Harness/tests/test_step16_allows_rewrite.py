"""`allows_rewrite` 必须真被执行（D-254）。

## 缺陷形状

`TierSpec.allows_rewrite` 是 D-050 退回重写的判据依据，架构第 180 行
明文要求 `TierSpec` 含该字段。但它**只在自己的定义处出现过**，
编排层一次没读。

## 为什么这不是「读漏了」那么简单

当前**看起来是对的**：咨询档 `gates_apply=False` → 门禁数为 0
→ 退回循环根本不进 → `allows_rewrite=False` 「碰巧」被满足。

> **被巧合遮住 ≠ 被实现。** 一旦咨询档将来要门禁（架构 §8.3
> 的口径是「不触发方向确认、不做政策检索、不走门禁」，没说永远不会有），
> 这个字段立刻从「死字段」变成「真漏洞」。

故判据不能只测现状（现状是绿的），必须测**机制**：
把门禁能力打开而 `allows_rewrite` 仍为假，看编排是否仍退回。

## 测什么、不断什么

**测**：不可退回的档位被强行喂门禁时，编排**不得**派发 `REVISION`，
且必须走 `升级裁决` 路径。

**不断**：退回数、轮次增减——那属于 `test_step14_*` 的地盘。
一个判据只锁一件事，才知道红的是哪条。

## 为什么不能只断言「不派发 REVISION」

不派发 REVISION 有两种原因：**正确地拒绝** vs **根本没进循环**。
前者发 `升级裁决` 事件并返回 `GATE_REWRITE_EXHAUSTED`，后者可能什么都不发。
故必须同时断言**事件与错误码**，两者缺一则判据会被绕过。
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
    from test_step14_orchestrate_gate import _编排
    from zhengtong.domain.tier import TierId

    编 = _编排(驳回=驳回)
    return 编, 编.跑到终(档位=TierId.PRO, 需求="通知")


class Testallows_rewrite字段本身:
    def test_咨询档不允许退回(self) -> None:
        """**前置事实**：确认字段值对。

        不是重复测 `tier.py`，而是这条判据的后续用例依赖它——
        若哪天有人把咨询档改成 `True`，下面的机制判据会失去意义。
        """
        from zhengtong.domain.tier import TIER_BY_ID, TierId

        assert TIER_BY_ID[TierId.CONSULT].allows_rewrite is False
        assert TIER_BY_ID[TierId.PRO].allows_rewrite is True


class Test不可退回档位的机制:
    """**本组是本缺陷的真正判据**：机制必须能验，故绕过门禁能力
    这道「巧合遮罩」，直接构造「有门禁但不许退」的组合。"""

    def test_不许退回时不得派发REVISION(self, monkeypatch) -> None:
        """**本条是核心**：`allows_rewrite=False` → 一次都不许自动重写。

        用 `replace` 造出「门禁适用但不许退回」的档位——
        即咨询档**将来**要门禁时的情形。
        """
        编, 结果 = _跑不可退回档({"G2": 99}, monkeypatch)

        assert 结果.is_error, "不许退回却放行了"
        重写 = [e for e in 编.事件 if e.kind.name == "GATE_REWORK"]
        assert not 重写, f"不许退回却重写了 {len(重写)} 轮——allows_rewrite 未被执行"

    def test_不许退回时走升级裁决(self, monkeypatch) -> None:
        """**必须断言事件与错误码，不能只断言「没重写」**。

        「没重写」有两种原因：正确拒绝 vs 根本没进循环。
        只断言前者的话，判据能被「让循环空转」绕过。
        """
        from zhengtong.domain.errors import ErrorCode

        编, 结果 = _跑不可退回档({"G2": 99}, monkeypatch)
        assert 结果.is_error
        错 = 结果.error
        assert 错 is not None
        assert 错.code is ErrorCode.GATE_REWRITE_EXHAUSTED, (
            f"错误码是 {错.code}，应是 GATE_REWRITE_EXHAUSTED"
        )
        升级 = [e for e in 编.事件 if e.kind.name == "GATE_ESCALATED"]
        assert 升级, "未发 GATE_ESCALATED——不是「拒绝退回」而是「没进循环」"

    def test_不许退回时错误信息要说清原因(self, monkeypatch) -> None:
        """**用户要能分清「不能改」与「改不动」**。

        只说「门禁未通过」会让用户以为再试一次有用。
        """
        _, 结果 = _跑不可退回档({"G2": 99}, monkeypatch)
        错 = 结果.error
        assert 错 is not None
        assert "不允许" in 错.detail or "不可退回" in 错.detail, (
            f"错误信息没说清是不可退档位：{错.detail}"
        )

    def test_可退回档位不受影响(self) -> None:
        """**反向对照**：接了 `allows_rewrite` 不能把 Pro 也堵死。

        这条防的是「无脑改成永不退回」——那会让 D-050 的重写功能失效，
        而现有判据仍全绿（它们只测 Pro 会重写，不测怎么关）。
        """
        编, 结果 = _跑(驳回={"G2": 99})
        assert 结果.is_error
        重写 = [e for e in 编.事件 if e.kind.name == "GATE_REWORK"]
        assert len(重写) == 2, f"Pro 档应重写 2 轮，实际 {len(重写)} 轮"


def _跑不可退回档(驳回: dict[str, int], monkeypatch):
    """跑一个「门禁适用但不许退回」的档位。

    ## 为什么要 monkeypatch 查表

    `跑到终(档位=TierId)` 收的是**枚举**，不是 `TierSpec`——
    造一个假 `TierSpec` 传进去会在 `阶段序列(档位)` 处 KeyError。

    故改 `TIER_BY_ID` 这张**真实查表**：编排层读的正是它
    （`_该跑门禁`、`TIER_BY_ID[档].roster`、`阶段序列` 都是查这张表）。
    改表不改调用路径，故判据仍验的是真机制。

    ## **为什么只改 `allows_rewrite`、不改 `id`**（栽过一次）

    首次构造时把 `id` 改成 `CONSULT`，结果**门禁段根本没进**：
    `阶段序列` 查的是 `TIER_BY_ID[档].is_consult`，而 `is_consult`
    由 `id` 决定 → 走了咨询序列（无 gating）→ 门禁数 0。

    于是判据红在「不许退回却放行了」，但**红的原因不是缺陷未修**，
    是我的构造让它压根没走到被测代码。

    > **判据红时先确认「红的是不是它声称的那件事」。**
    > 这次是我自己的判据在骗我——和 D-214 同族：
    > 同一个结果（通过）可以由不同原因产生，判据须能区分。

    故只改 `allows_rewrite` 一个字段：门禁照跑（走成文序列）、
    但不许退回——这才是字段真被读到的可观察后果。
    """
    from test_step14_orchestrate_gate import _编排
    from zhengtong.domain import tier as tier_mod
    from zhengtong.domain.tier import TierId

    假 = dataclasses.replace(
        tier_mod.TIER_BY_ID[TierId.PRO],
        allows_rewrite=False,
    )
    monkeypatch.setitem(tier_mod.TIER_BY_ID, TierId.PRO, 假)
    编 = _编排(驳回=驳回)
    结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
    return 编, 结果


import dataclasses  # noqa: E402

_ = pytest
