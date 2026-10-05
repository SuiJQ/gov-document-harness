"""成员六态机（D-042）。

## 四态可双向迁移，只有一条边不可逆

```
        ┌──────┐
        │ 空闲 │  编排层尚未派发
        └──┬───┘
           │ 派发
           ▼
    ┌──────────────┐◀──── 条件命中 / 用户已答
    │    运行中     │
    └──┬────────┬──┘
       │        │ 超时/失败
       ▼        ▼
  完成/失败    等待条件 / 等待用户 / 挂起
```

一次任务里一个成员可能被反复唤醒多次，
所以「运行中 ⇄ 挂起 / 等待用户 / 等待条件」**四态之间可双向迁移**；
但**唯一不可逆的边是进入「完成/失败」**。

## 两条不可违反的约束（D-042）

1. `挂起` 状态下**绝不发起任何模型请求**——不空转、不轮询、不烧算力。
2. 产物就绪等场景**应主动 sleep + 注册条件唤醒**，不是干等。

第 2 条不写在这里，它靠编排层「不调用 `该发请求`」来保证；
第 1 条写在这里，是因为**它是关于状态的判断**，
而状态判断一旦散落到各处，就会有一处忘了。

## 为什么用函数而不是塞进 `RoleState`

`RoleState` 是 `frozen` 枚举加两个属性，塞进状态转移表会让
枚举依赖领域规则。这里保持枚举纯净，规则单独成模块——
**枚举是数据，状态机是规则，两者分开放**。
"""

from __future__ import annotations

from ..domain.role import RoleState

#: 允许的流转。**唯一不可逆的那条用注释标出。**
_允许: frozenset[tuple[RoleState, RoleState]] = frozenset(
    {
        (RoleState.IDLE, RoleState.RUNNING),
        (RoleState.RUNNING, RoleState.DONE),  # ← 唯一不可逆
        # 运行中 ⇄ 三个等待态（可反复唤醒）
        (RoleState.RUNNING, RoleState.SUSPENDED),
        (RoleState.RUNNING, RoleState.AWAITING_USER),
        (RoleState.RUNNING, RoleState.AWAITING_CONDITION),
        (RoleState.SUSPENDED, RoleState.RUNNING),
        (RoleState.AWAITING_USER, RoleState.RUNNING),
        (RoleState.AWAITING_CONDITION, RoleState.RUNNING),
    }
)


def 允许流转(起: RoleState, 止: RoleState) -> bool:
    """是否允许这次流转。**非法流转返回 `False`，不抛异常**——
    状态机是判定器不是校验器，抛异常会把「还没到那一步」变成崩溃。"""
    return (起, 止) in _允许


def 可逆(起: RoleState, 止: RoleState) -> bool:
    """这次流转是否**双向可达**（即不是那条不可逆边）。

    判据直接查「反向是否也允许」——
    比维护第二张表可靠：两张表迟早会分叉。
    """
    return (止, 起) in _允许


def 该发请求(态: RoleState) -> bool:
    """该状态下**是否可以发起模型请求**。

    D-042 铁律一：只有「运行中」可以。
    挂起、等待用户、等待条件、空闲一律不发——
    **不空转、不轮询、不烧算力**。
    """
    return 态 is RoleState.RUNNING
