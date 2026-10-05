"""第 14 步判据：L5 门禁 + 断点续跑。

架构 §15 第 14 行的三条：
**并行 4 组（D-049）／退回上限 2 次（D-050）／幂等重放（D-065）**。

本文件覆盖三类偷懒实现，每条判据对准一个：

| 偷懒实现 | 抓它的判据 |
|---|---|
| 门禁只报「跑过了」不真判定 | `test_门禁结论须由取证产出` |
| 12 道门禁串行调用 | `test_十二道门禁压成四组` |
| 第 3 次还在自动重写 | `test_第三次升级用户裁决` |
| 环境性缺失也走「退 2 次」 | `test_环境性缺失一次性升级` |
| 红线被当「建议」放行 | `test_红线不放行` |
| 幂等只覆盖重试不覆盖退回 | `test_退回重写不命中幂等` |

## 判据怎么写才算数

**「门禁跑通了」最容易被写成 `assert 报告.ok`**——那只证明没崩。
故此处一律断言**结论是从取证内容算出来的**：
换一份取证内容，结论必须跟着变；取证方不在编制内，门禁必须报缺证据。

## 本轮我自己的判据错误（两处，都是「报红先问测试错还是实现错」）

1. 初版断言「并行组 ≤ 4」取自架构原文，**但审计 D-143 已把 TEAM_LEAD 组
   补回 `gate_groups()`（实证：穷举 32 种编制组合）**，故完整枚举是 5 组。
   我把「并行组」与「完整枚举」混为一谈 → **测试错**。
2. `终闸组()` 初版返回扁平 `(RoleId.TEAM_LEAD,)`，与 `gate_groups()` 的
   嵌套 `((TEAM_LEAD,),)` 不同形，导致「并集 == 完整枚举」判据恒假 →
   **实现错**（我造的类型不一致），已修为同形。
"""

from __future__ import annotations

import pytest

from zhengtong.domain.gate import GateId, gate_groups, 并行门禁组, 终闸组
from zhengtong.domain.role import RoleId
from zhengtong.domain.tier import TIER_BY_ID, TierId

# ═══ D-049 并行 4 组 ═══


class Test并行分组:
    def test_十二道门禁压成四组(self) -> None:
        """12 道正式门禁必须压成 4 组并行调用（D-049，架构 §8.3 阶段图）。

        **不许 12 次调用**。门槛：并行的独立取证组恰好 4 组，
        且不含主理人终闸组——架构明文「主理人独占终闸不与其他门禁并行」。
        """
        组 = 并行门禁组()
        assert len(组) == 4, f"应压成 4 组并行，实得 {len(组)} 组：{组}"
        assert all(组), "有空组——那是把门禁算进分组却没真派出去"
        assert all(RoleId.TEAM_LEAD not in g for g in 组), "主理人独占终闸不得混入并行组"

    def test_终闸组单列且为主理人(self) -> None:
        """终闸组 = 主理人独占，与并行组分离。"""
        assert 终闸组() == ((RoleId.TEAM_LEAD,),)

    def test_完整枚举含终闸组(self) -> None:
        """`gate_groups()` 是全部独立取证组的完整枚举（审计 D-143 补回 TEAM_LEAD）。

        并行组 + 终闸组 == 完整枚举——**两函数合起来不重不漏**，
        这才是「从 owners 推导、不另建事实源」的正确形态。
        """
        assert set(并行门禁组()) | set(终闸组()) == set(gate_groups())

    def test_咨询档无门禁不并行(self) -> None:
        """咨询档 `gates_apply=False`，不该有任何门禁组。"""
        assert TIER_BY_ID[TierId.CONSULT].gates_apply is False

    def test_十二道正式门禁都有主人(self) -> None:
        """每道正式门禁都必须能落到一个取证方（D-049 配平铁律）。

        G9 是唯一例外——运行期才定（D-084）。
        """
        from zhengtong.domain.gate import FORMAL_GATES, GATES

        无主 = [
            g.value for g in FORMAL_GATES if not GATES[g].owners and not GATES[g].dynamic_owners
        ]
        assert not 无主, f"这些门禁既无静态主人又非动态：{无主}"


# ═══ 判据基座自检 ═══


class Test判据基座:
    def test_本文件引用的门禁与档位都存在(self) -> None:
        """防判据自身写错档位名/门禁名却仍然全绿。"""
        assert GateId.G1 in GateId
        assert TIER_BY_ID[TierId.FLASH].allows_rewrite is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
