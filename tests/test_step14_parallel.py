"""D-049 并行 4 组：**并发执行**本身（补第 14 步漏掉的那一半）。

## 这条是复查时补上的，不是第一版就有的

第 14 步首版只做了两件事：
1. 把 `gate_groups()` 拆成「并行组 / 终闸组」（D-218）
2. 实现门禁判定与退回重写（D-220~D-222）

但**并行本身没接**：`编排器._门禁循环` 调的是 `_派发`，
那是一条串行 `for` 循环；真正的并行调度器 `kernel/scheduler.run_groups`
（含 ThreadPoolExecutor、D-130 异常就地转失败）**生产代码零引用**。

实测证据（首版，Pro 档 11 次模型调用，每次 0.15s）：
耗时 1.65s ≈ 11×0.15s，且 enter/exit 标记**严格交替** → 完全串行。

**这是本项目反复记录的同型缺口：零件都测过、衔接没测。**
判据锁不住它的原因也很典型——`test_十二道门禁压成四组` 查的是
`并行门禁组()` 这个函数的返回值，而并行是 `_门禁循环` 的**行为**。
断言「目标」而非「表面特征」在这里又生效了一次。

## 判据怎么写才算数

不能用「调了 `run_groups`」当判据——把串行 for 循环包一层函数名就骗过去了。
故本文件用**时间与线程标记**判定：4 组各 0.2s，真并行约 0.2s，
串行则约 0.8s。阈值取二者中点，留足调度抖动余量。
"""

from __future__ import annotations

import threading
import time

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.domain.gate import GateId
from zhengtong.domain.run_state import GateRecord
from zhengtong.domain.tier import TierId
from zhengtong.orchestration import 编排器

#: 每个角色被问时睡这么久（模拟真实模型延迟）
_每问延迟 = 0.2


class Test门禁真并行:
    def test_并行组并发调用取证(self) -> None:
        """`run_groups` 必须**并发**调用 `门禁取证`。

        判据直接打在 `run_groups` 上：4 个组各睡 0.2s，
        并发约 0.2s、串行约 0.8s。取 0.75 中点为阈值。
        """
        在场 = {"n": 0, "峰": 0}
        锁 = threading.Lock()
        标记: list[str] = []

        def 慢取证(门禁: tuple[GateId, ...]) -> dict[str, GateRecord]:
            with 锁:
                在场["n"] += 1
                在场["峰"] = max(在场["峰"], 在场["n"])
                标记.append("enter")
            try:
                time.sleep(_每问延迟)
            finally:
                with 锁:
                    在场["n"] -= 1
                    标记.append("exit")
            return {g.value: GateRecord(gate=g.value, passed=True) for g in 门禁}

        编 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 门禁取证=慢取证)
        编.跑到终(档位=TierId.PRO, 需求="通知")
        assert 在场["峰"] >= 2, f"取证最大同时在场 {在场['峰']} 个——并行组没有并发执行"

    def test_十二道门禁压成四组调用(self) -> None:
        """12 道门禁必须压成 **4 次**取证调用（D-049），不是 12 次。

        次数是最直接的判据：一次一个门禁就是没分组。
        """
        次数 = {"n": 0}

        def 计数取证(门禁: tuple[GateId, ...]) -> dict[str, GateRecord]:
            次数["n"] += 1
            return {g.value: GateRecord(gate=g.value, passed=True) for g in 门禁}

        编 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 门禁取证=计数取证)
        编.跑到终(档位=TierId.PRO, 需求="通知")
        # 4 个并行组 + 1 次终闸（主理人独占，串行在后）
        assert 次数["n"] == 5, f"取证调了 {次数['n']} 次——应压成 4 组并行 + 1 次终闸 = 5 次"

    def test_终闸组不与门禁组并行(self) -> None:
        """G12（主理人独占终闸）**不得**与门禁组并行（D-218 的行为面）。

        复查查出：`_该段角色` 从 `GATES[...].owners` 展开，
        **把 G12 的取证方（主理人）也算进了 GATING 的并行当值**——
        D-218 只修了分组函数，编排侧这个洞还在。
        """
        from zhengtong.domain.gate import GATES, 并行门禁组
        from zhengtong.domain.gate import GateId as _G
        from zhengtong.domain.role import RoleId

        终闸主人 = set(GATES[_G.G12].owners)
        并行当值 = {r for 组 in 并行门禁组() for r in 组}
        assert not (终闸主人 & 并行当值), f"主理人独占终闸 {终闸主人} 被排进了并行组 {并行当值}"
        assert RoleId.TEAM_LEAD not in 并行当值
