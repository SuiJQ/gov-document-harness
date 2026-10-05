"""真实校验器（D-196）。

## 为什么这曾是欠账

`build_app_from_workspace` 的 `validators` 端口**故意接假实现**，
注释写明「第 11 步须改掉」。理由写在 D-196：

> 假实现接在真实运行路径上，是本项目反复吃亏的地方。

D-165 的五次（llm / retriever / user_files / storage / 路径守卫），
症状完全一样：**真配置、测试全绿、什么也没真跑**。
故这条不留含糊注释，直接立账。

## 刻意的类型隔离（D-069）

返回的是 `ValidationResult`，**不是 `GateVerdict`**。
原因见 `ports/validators.py` 的模块文档：
脚本输出不得作为门禁通过依据。

本模块因此**不 import `GateVerdict`、不构造门禁结论**——
若哪天有人在这里返回一个门禁对象，`test_结果类型不是门禁结论`
会立刻变红。

## 本模块查什么

| 校验 | 查什么 | 依据 |
|---|---|---|
| `run_hard_constraint` | D-Card 必填字段零沉降 | D-040 / D-069 |
| `run_structure` | 门禁须有证据 | D-034「无证据不通过」 |

**只查结构化事实，不查文风**。文风与合规细则在内容包里由模型执行，
代码硬编码文风判据就等于把公文内容写进代码（D-055），明令禁止。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.errors import Result
from ..ports.validators import ValidationIssue, ValidationResult

#: D-Card 必填字段（D-040）。**只有 D8 允许主理人研判**，
#: 其余字段用户没给就是没给——不在此处判「编造」，
#: 那由 `validate_dcard` 按 `Provenance` 拦。
_必填字段: tuple[str, ...] = ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D9")


@dataclass(frozen=True, slots=True)
class 硬约束校验器:
    """结构化硬约束校验（D-040 / D-034）。

    **入参是 `dict[str, object]` 而非自由文本**——端口签名就是这么定的。
    校验器读不懂散文，读得懂结构；让模型把结构化内容转成散文再校验，
    等于在校验前先丢掉了结构。
    """

    def run_hard_constraint(self, payload: dict[str, object]) -> Result[ValidationResult]:
        """D7 等必填字段**零沉降**：缺就是缺，不得以默认值填上。"""
        卡 = payload.get("dcard")
        问题: list[ValidationIssue] = []
        if not isinstance(卡, dict):
            问题.append(
                ValidationIssue(
                    rule="D040-结构",
                    message="入参缺 dcard 结构，无法校验必填字段",
                )
            )
        else:
            for 字段 in _必填字段:
                值 = 卡.get(字段)
                if 值 is None or (isinstance(值, str) and not 值.strip()):
                    # **带锚点**：报出是哪个字段（D-019：证据须有锚点）。
                    问题.append(
                        ValidationIssue(
                            rule="D040-必填",
                            message=f"{字段} 为空，缺则不得动笔",
                            at=f"dcard.{字段}",
                        )
                    )
        return Result.ok(
            ValidationResult(
                script="hard_constraint",
                passed=not 问题,
                issues=tuple(问题),
            )
        )

    def run_structure(self, payload: dict[str, object]) -> Result[ValidationResult]:
        """门禁须有证据。**无证据不通过**（D-034）。"""
        问题: list[ValidationIssue] = []
        门禁 = payload.get("gate")
        if not isinstance(门禁, str) or not 门禁:
            问题.append(ValidationIssue(rule="D034-结构", message="入参缺 gate 编号"))
        证据 = payload.get("evidence")
        if not isinstance(证据, (list, tuple)) or not 证据:
            # **锚点缺失按阻断处理**（`ValidationResult.blocking`）：
            # 无法定位即无法核实。
            问题.append(
                ValidationIssue(
                    rule="D034-无证据",
                    message=f"{门禁 or '门禁'} 无取证证据，不得判定通过",
                )
            )
        return Result.ok(
            ValidationResult(
                script="structure",
                passed=not 问题,
                issues=tuple(问题),
            )
        )
