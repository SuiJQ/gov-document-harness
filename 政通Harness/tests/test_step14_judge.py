"""门禁判定与退回：第 14 步核心判据（判据先于实现）。

覆盖 §8.3 门禁失败的三条处置分支，以及 D-050 上限与环境性缺失反死循环铁律。

## 判据锁的是「决策函数」而非「跑没跑」

若只断言「门禁跑了」，任何 `return ok` 都能骗过去。故本组判据全部锁在
**纯函数 `门禁判定(记录, 环境性缺失) -> 门禁处置`** 上：
换一组输入必须换一种处置，同一组输入必须稳定。
"""

from __future__ import annotations

import pytest

from zhengtong.domain.gate import 处置, 门禁判定
from zhengtong.domain.run_state import GateRecord


def _记录(**kw: object) -> GateRecord:
    return GateRecord(gate="G2", **kw)  # pyright: ignore[reportArgumentType]


class Test门禁判定三分支:
    def test_通过则放行(self) -> None:
        assert 门禁判定(_记录(passed=True), False) is 处置.放行

    def test_红线退回不升级(self) -> None:
        """红线问题退回重写，且首次退回不得升级（D-028 无绕过）。"""
        判 = 门禁判定(_记录(passed=False, rejection_reason="E1 事实编造"), False)
        assert 判 is 处置.退回

    def test_建议不阻断但仍归档(self) -> None:
        """建议类不阻断——措辞可优化不能卡死流程（D-016 三档处置）。"""
        assert 门禁判定(_记录(passed=True, rejection_reason="措辞可优化"), False) is 处置.放行


class TestD050退回上限:
    def test_退回第一次仍退回(self) -> None:
        assert 门禁判定(_记录(passed=False, attempt=1), False) is 处置.退回

    def test_退回第二次升级裁决(self) -> None:
        """同一门禁退回上限 2 次：第 3 次不再自动重写，转用户裁决（D-050）。"""
        assert 门禁判定(_记录(passed=False, attempt=2), False) is 处置.升级裁决

    def test_退回上限不因通过而绕过(self) -> None:
        """通过就是通过——上限只拦不通过的，不会把通过的判成升级。"""
        assert 门禁判定(_记录(passed=True, attempt=2), False) is 处置.放行


class Test环境性缺失反死循环:
    def test_环境性缺失直接升级(self) -> None:
        """无库/无网导致的取证失败必须一次性升级，不得走「退2次」空转。

        这是 gate-manual「反死循环铁律」——反复退避本身是缺陷。
        """
        判 = 门禁判定(_记录(passed=False, attempt=0, rejection_reason="无网络"), True)
        assert 判 is 处置.升级裁决

    def test_环境性缺失即使attempt为0也升级(self) -> None:
        """第 1 次就升级，不消耗退回数——环境问题不是「文稿需要改」。"""
        assert 门禁判定(_记录(passed=False, attempt=0), True) is 处置.升级裁决


class Test处置枚举语义:
    def test_放行不等于无建议(self) -> None:
        """放行允许带建议，但绝不等于「退回」——两者必须可区分。"""
        assert 处置.放行 is not 处置.退回
        assert 处置.退回 is not 处置.升级裁决


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
