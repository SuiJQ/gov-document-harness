"""档位传播一致性（D-413）。

## 为什么单独开一个文件

`8f742ae` 修掉了 `_建过程()` 里硬编码的 `TierId.PRO`，改成「档位由调用方
显式传入」。方向对，但那次改动**没有留下任何判据**——`FIX_TIER_MISMATCH_TODO.md`
里「补充判据：档位传播一致性测试」一条至今没勾，于是同一个缺陷可以原地复发
而全仓 2100 多条测试一声不响。

这正是本项目栽过八次的那一族（D-222 / D-231 / D-233 / D-234 / D-235 / D-406）：
**值在那里，但没人往下传；所有既有测试全绿。**

## 本文件钉四件事

1. **传进去的档就是过程用的档**——三档逐个验，不再只验 PRO。
   硬编码 PRO 的时候，PRO 那条永远是绿的，**绿得最可疑**。
2. **档位要真的决定阶段线**，不是只往 `TaskSpec` 里塞个字段好看。
   这条本轮查出真缺陷：`_建过程()` 把 `序列` 写死成 `成文序列`，
   于是咨询档经这条路建出来的过程走的是成文的七个阶段——
   D-Card、大纲、门禁全会跑。生产暂不触发（`跑到终` 自己建过程），
   但它是颗地雷：哪天有人让 `取一步` 抢在 `跑到终` 之前建过程就炸。
3. **幂等复用不许静默吞档位**——`_建过程` 是幂等的，过程已存在时
   传进来的档位会被丢弃。丢弃 = 按先建的档跑完全程且不报错。
4. **`跑到终` 定的档，字段与状态必须一致**——编排器自己记的
   `self.档位` 和过程的 `状态.task.tier` 得是同一个档，
   否则请求装配按一个档、门禁编制按另一个档（D-380 的成因）。
"""

from __future__ import annotations

import pytest

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.domain.run_state import GateRecord
from zhengtong.domain.tier import TierId
from zhengtong.orchestration import 编排器
from zhengtong.orchestration.orchestrate import 咨询序列, 成文序列, 阶段序列


def _编() -> 编排器:
    return 编排器(
        llm=FakeLlm(),
        storage=FakeStorage(),
        门禁取证=lambda 请求: {g.value: GateRecord(gate=g.value, passed=True) for g in 请求.门禁},
    )


class Test传进去的档就是过程用的档:
    @pytest.mark.parametrize("档", list(TierId))
    def test_建过程按传入档位建(self, 档: TierId) -> None:
        """三档逐个验。**只验 PRO 等于没验**——硬编码 PRO 时那条恒绿。"""
        编 = _编()
        过程 = 编._建过程(档位=档)
        assert 过程.状态.task.tier is 档, (
            f"传了 {档.value}，过程却是 {过程.状态.task.tier.value}——档位被中途丢弃"
        )

    def test_咨询档走咨询序列(self) -> None:
        """档位要**决定阶段线**，不是只写进 TaskSpec 好看。

        咨询档只该走 INTAKE → FINAL → DONE。若这里拿到成文序列，
        说明 `_建过程` 只把档位塞进状态、没喂给 `序列`——
        咨询档会照跑方向确认、大纲与门禁。
        """
        编 = _编()
        过程 = 编._建过程(档位=TierId.CONSULT)
        assert 过程.序列 == 咨询序列, f"咨询档走了 {过程.序列}——阶段线没按档位取"

    @pytest.mark.parametrize("档", [TierId.FLASH, TierId.PRO])
    def test_成文档走成文序列(self, 档: TierId) -> None:
        编 = _编()
        过程 = 编._建过程(档位=档)
        assert 过程.序列 == 成文序列


class Test幂等复用不许静默吞档位:
    def test_同档复用不报错(self) -> None:
        """幂等是既有契约：同档复用返回同一个对象。"""
        编 = _编()
        首 = 编._建过程(档位=TierId.PRO)
        assert 编._建过程(档位=TierId.PRO) is 首

    def test_换档复用要炸而不是静默(self) -> None:
        """过程已按 PRO 建好，又传 CONSULT —— **必须响**。

        静默返回旧过程的话，调用方以为自己交的是 CONSULT，实际按 PRO 跑：
        门禁、编制、推理开关全按 PRO 执行，且没有任何输出提示这一点。
        """
        编 = _编()
        编._建过程(档位=TierId.PRO)
        with pytest.raises(ValueError, match="档位不一致"):
            编._建过程(档位=TierId.CONSULT)


class Test跑到终定的档两处一致:
    @pytest.mark.parametrize("档", list(TierId))
    def test_字段与状态同档(self, 档: TierId) -> None:
        """编排器记的 `self.档位` 与过程的 `状态.task.tier` 必须是同一个。

        前者管请求装配（推理开关、工具域），后者管门禁与编制。
        两者分叉 = 一套按 A 档发请求、另一套按 B 档判门禁（D-380）。
        """
        编 = _编()
        编.跑到终(档位=档, 需求="通知")
        assert 编.档位 is 档
        assert 编._过程 is not None
        assert 编._过程.状态.task.tier is 档, (
            f"`跑到终` 收了 {档.value} 但过程是 {编._过程.状态.task.tier.value}"
        )
        assert 编._过程.序列 == 阶段序列(档)
