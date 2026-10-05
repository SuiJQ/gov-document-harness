"""强类型标识。

为什么要强类型：`RunId` 与 `ArtifactId` 都是字符串，类型系统分不出
"把产物 ID 误当成运行 ID 传进函数"。本项目里这种错误一旦发生，
后果是**静默读到错误的续跑状态**——不报错，只是结果不对。
用 NewType 把它变成静态可查的错误。

用 `NewType` 而不是包装类：包装类会引入运行时开销与序列化麻烦，
而标识只是"换个标签的字符串"，不需要额外行为。
代价是运行期不做校验——但标识只由程序自己生成，不由用户输入，
不存在伪造场景。
"""

from __future__ import annotations

from typing import NewType

RunId = NewType("RunId", str)
"""一次运行的标识。格式 `run-YYYYMMDD-HHMMSS-<4位>`，可读且便于人工排查。"""

ArtifactId = NewType("ArtifactId", str)
"""产物的标识。含阶段与角色，便于从文件名反查来源（D-080：仅内部使用）。"""

# 注：`PhaseId` 与 `GateId` 曾在此以 NewType 各声明一份，但真正的定义是
# `phase.py` 的 `PhaseId` 枚举与 `gate.py` 的 `GateId`——同名两物。
# 写 `from .ids import PhaseId` 会拿到一个 NewType，赋值给枚举字段时报
# AttributeError，且报错位置离定义处很远，极难定位。故移除这两个别名，
# 全仓一律从 phase.py / gate.py import。**(删于实现第 3 步，实测踩到)**


def make_run_id(stamp: str, suffix: str) -> RunId:
    """构造运行标识。

    `stamp` 形如 `20261001-143000`，`suffix` 为四位十六进制。
    刻意**不做格式校验**——标识由程序自己生成，
    校验只会带来"到底谁负责生成"的额外约定，得不到实际收益。
    """
    return RunId(f"run-{stamp}-{suffix}")
