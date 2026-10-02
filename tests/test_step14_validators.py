"""第 14 步：提问前必须跑真实校验器（D-040）。

## 这条欠账的准确形态

`编排器.__init__` 收了 `validators` 端口并存进 `self.validators`，
**但全类从头到尾没有一处读它**——真校验器接上了，却没人调用。

而 D-040 要求的是「提问前先跑硬约束」：方向确认（D-Card 必填字段零沉降）
必须在**向用户提问之前**校验，否则拿着缺字段的 D-Card 去问用户，
问出来的答案填不进去，等于白问一轮。

## 判据锁的是「真被调用过」，不是「校验器接上了」

第 11 步 D-196 已经让 `build_app_from_workspace` 装真校验器，
那类判据查的是**装配**。本文件查的是**调用**：
即使接上了，没人调它一样是零实现——这正是本条欠账的形状。
"""

from __future__ import annotations

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.domain.run_state import GateRecord
from zhengtong.domain.tier import TierId
from zhengtong.orchestration import 编排器
from zhengtong.ports.validators import ValidationIssue, ValidationResult


class _记录校验器:
    """记录被调过的校验器，并可指定硬约束是否通过。"""

    def __init__(self, *, 通过: bool = True) -> None:
        self.通过 = 通过
        self.硬约束入参: list[dict[str, object]] = []
        self.结构入参: list[dict[str, object]] = []

    def run_hard_constraint(self, payload: dict[str, object]) -> object:
        self.硬约束入参.append(payload)
        issues = () if self.通过 else (ValidationIssue(rule="D7", message="必填字段沉降"),)
        from zhengtong.domain.errors import Result

        return Result.ok(ValidationResult(script="hard", passed=self.通过, issues=issues))

    def run_structure(self, payload: dict[str, object]) -> object:
        self.结构入参.append(payload)
        from zhengtong.domain.errors import Result

        return Result.ok(ValidationResult(script="struct", passed=True))


def _编排(校验器: object) -> 编排器:
    return 编排器(
        llm=FakeLlm(),
        storage=FakeStorage(),
        validators=校验器,  # pyright: ignore[reportArgumentType]
        门禁取证=lambda 门禁: {g.value: GateRecord(gate=g.value, passed=True) for g in 门禁},
    )


class Test提问前跑硬约束:
    def test_方向确认前跑过硬约束(self) -> None:
        """DIRECTION（方向确认）之前必须调用 `run_hard_constraint`（D-040）。"""
        校 = _记录校验器()
        编 = _编排(校)
        编.跑到终(档位=TierId.PRO, 需求="通知")
        assert 校.硬约束入参, "提问前没跑 run_hard_constraint——D-040 零实现"

    def test_硬约束收到DCard而非空壳(self) -> None:
        """校验入参必须带 D-Card（D-196 同型缺口：真校验器要看的东西不能不传）。"""
        校 = _记录校验器()
        编 = _编排(校)
        编.跑到终(档位=TierId.PRO, 需求="通知")
        首次 = 校.硬约束入参[0]
        assert "dcard" in 首次, f"硬约束入参没有 dcard：{首次}"

    def test_硬约束不通过则不放行(self) -> None:
        """硬约束不通过时，**不得**照常提问跑完——须被拦下。

        这是 D-040 的实质：不通过还往下走，等于校验器白跑。
        """
        校 = _记录校验器(通过=False)
        编 = _编排(校)
        结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
        assert 结果.is_error, "硬约束不通过却照常跑完——校验形同虚设"

    def test_咨询档也跑硬约束(self) -> None:
        """咨询档门禁不适用，但 D-Card 零沉降对咨询同样成立。"""
        校 = _记录校验器()
        编 = _编排(校)
        编.跑到终(档位=TierId.CONSULT, 需求="咨询一下")
        assert 校.硬约束入参, "咨询档未跑硬约束"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
