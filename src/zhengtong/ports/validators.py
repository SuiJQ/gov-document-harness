"""校验端口。

**这里有一处刻意的类型隔离（D-069）**，别当成冗余去掉：

两个从原专家团系统移植的校验脚本，返回类型叫 `ValidationResult`，
**不是 `GateVerdict`**——哪怕它检查的东西看起来很像门禁。

原因写在两份脚本的注释里：**脚本输出不得作为门禁通过依据**。
如果移植时顺手把类型名改成 `GateVerdict`，编排层就可能直接把
脚本结果当成门禁结论放行，于是"无证据不通过"这条铁律被绕过。
**用类型系统阻止误用**，比在文档里写一句警告可靠得多。

同一条纪律的另一半：`ValidationResult` **不能**被 `Result[T]`
接受，因为两者语义不同——`Result` 表示"操作成功否"，
`ValidationResult` 表示"内容合规否"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..domain.errors import Result


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """一条校验问题。`at` 为可定位锚点（D-019：证据须有锚点）。"""

    rule: str
    message: str
    at: str = ""

    @property
    def has_anchor(self) -> bool:
        return self.at != ""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """脚本校验结果。**刻意不叫 GateVerdict，见模块文档（D-069）。**"""

    script: str
    passed: bool
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def blocking(self) -> tuple[ValidationIssue, ...]:
        """阻断级问题（无锚点的问题按阻断处理——无法定位即无法核实）。"""
        return tuple(i for i in self.issues if not i.has_anchor)


@runtime_checkable
class ValidatorPort(Protocol):
    """移植的硬约束与结构校验脚本（D-054）。"""

    def run_hard_constraint(self, payload: dict[str, object]) -> Result[ValidationResult]:
        """D7 硬约束零沉降。结构化入参，不传自由文本。"""
        ...

    def run_structure(self, payload: dict[str, object]) -> Result[ValidationResult]: ...
