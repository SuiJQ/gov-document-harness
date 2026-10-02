"""工具声明。**数据化**（架构 §7.4）。

## 为什么是数据不是函数

`ToolSpec` 是 frozen dataclass，不是 handler 类。
理由是「这份工具清单」必须能**打印给人看**：
模型能看到什么、允许什么角色调、有什么副作用，
都应当是一份能被直接读出来的东西，而不是要读代码才知道。

## 双重白名单落在两个字段上

架构 §7.4「每次调用三重检查」：

1. **角色白名单** → `allowed_roles`
2. **路径白名单** → 由 `WorkspaceGuard` 执行，见 §7.4「路径解析的唯一实现」（D-076）
3. **草稿纸上限** → D-042，见 `草稿纸.py`

**`allowed_roles` 是空集时表示「无人可调」**，不表示「所有人可调」。
把空集解释成通配，会让「删掉一条白名单」变成「放开给所有人」——
这是最容易出事的默认方向，故反着来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from ...domain.role import RoleId


class Effect(StrEnum):
    """副作用类别。**决定审计强度与是否需要审批**（架构 §7.4）。

    分开而非笼统分「读/写」，是因为处置完全不同：
    `PUBLISH` 会在公共区留下他人可见的东西，
    `SUSPEND` 会停住这个角色、`READ` 什么都不会发生。
    """

    PUBLISH = "publish"  # 产出他人可见的产物
    READ = "read"  # 只读，无副作用
    SIGNAL = "signal"  # 向主理人或用户发信号
    SUSPEND = "suspend"  # 挂起/唤醒，改变角色运行状态
    ASK_USER = "ask_user"  # 向用户提问
    REWORK = "rework"  # 退回重做
    PURE = "pure"  # 纯校验，无外部可见影响


#: 全体角色（五个专家都包含）。
_全体: Final[frozenset[RoleId]] = frozenset(
    {RoleId.TEAM_LEAD, RoleId.STRUCTURE, RoleId.LANGUAGE, RoleId.COMPLIANCE, RoleId.DOC_ADVISOR}
)

#: 仅主理人范正章（D-041：成员唯一的对外通道是向主理人汇报）。
_仅主理人: Final[frozenset[RoleId]] = frozenset({RoleId.TEAM_LEAD})


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个工具的声明。

    `parameters` 用 JSON Schema 的 `dict`——它要原样发给模型，
    转成自定义对象就等于我们有权改写发给模型的协议（D-030：
    「程序原样请求，不拼接、不改写」）。
    """

    name: str
    description: str
    parameters: dict[str, object] = field(default_factory=dict)
    allowed_roles: frozenset[RoleId] = _全体
    effect: Effect = Effect.READ
    requires_publish: bool = False  # 产物必须经公共区（D-042）

    @property
    def 可被全员调用(self) -> bool:
        return self.allowed_roles == _全体

    @property
    def 仅主理人可调(self) -> bool:
        """供审计与断言使用：这类工具漏到成员手里是权限事故。"""
        return self.allowed_roles == _仅主理人
