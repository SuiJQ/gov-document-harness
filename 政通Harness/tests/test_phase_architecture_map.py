"""代码阶段 ↔ 架构阶段的映射契约（D-152）。

第四轮检查查出的最大问题：架构 §8.3、内容包、代码 `PhaseId`
是**三方三套编号**。D-152 定的修法是加映射表而非改名。

**本文件的作用是防止映射表被改成一对一**——
那会把「代码少一次角色交接」这个实现缺口藏起来。
"""

from __future__ import annotations

import pytest

from zhengtong.domain.phase import PhaseId


def test_映射须覆盖全部阶段() -> None:
    """漏一个成员 = 有一阶段在日志里查不到对应 Phase 号。"""
    for phase in PhaseId:
        assert phase.architecture_phase, f"{phase.value} 没有架构阶段映射"


def test_outline必须是区间不是单值() -> None:
    """`OUTLINE` 要走完 Phase 0 与 Phase 0.5 **两次角色交接**。

    写成单值（如 "0"）等于假装只有一次交接，
    于是「代码少一次交接」这件事在文档里就看不见了。
    """
    v = PhaseId.OUTLINE.architecture_phase
    assert "0.5" in v, f"OUTLINE 的映射应含 0.5（政策取证 + 文种判定），实为 {v!r}"
    assert "~" in v, f"OUTLINE 应记为区间，实为 {v!r}"


def test_映射不得为空串或占位() -> None:
    """防止用 '-'、「待定」之类糊弄过去。

    这里刻意**不**断言长度——`DRAFTING` 的合法映射就是单字 `"1"`，
    证伪时实测到：加了 `len >= 2` 会把正确的映射判失败。
    判据只能是「非空且不是占位词」。
    """
    占位 = {"-", "—", "待定", "?", "TBD", "N/A"}
    for phase in PhaseId:
        v = phase.architecture_phase
        assert v.strip(), f"{phase.value} 的映射为空"
        assert v not in 占位, f"{phase.value} 的映射是占位词：{v!r}"


def test_各阶段映射与架构一致() -> None:
    """逐条钉住——架构 §8.3 改了这里必须跟着改。"""
    期望 = {
        PhaseId.INTAKE: "-1",
        PhaseId.DIRECTION: "-1",
        PhaseId.OUTLINE: "0",
        PhaseId.DRAFTING: "1",
        PhaseId.GATING: "3",
        PhaseId.REVISION: "1",
        PhaseId.FINAL: "2",
        PhaseId.DONE: "4",
    }
    for phase, 前缀 in 期望.items():
        实际 = phase.architecture_phase
        assert 实际.startswith(前缀), f"{phase.value} 应以 {前缀} 开头，实为 {实际!r}"


def test_门禁与定稿不得映射到同一阶段() -> None:
    """`GATING` 与 `FINAL` 是两件事，映射撞一起说明表填错了。"""
    assert PhaseId.GATING.architecture_phase != PhaseId.FINAL.architecture_phase


@pytest.mark.parametrize("phase", list(PhaseId))
def test_映射对所有阶段稳定(phase: PhaseId) -> None:
    """属性应是纯读取，多次调用结果一致（避免有人塞了有副作用的实现）。"""
    assert phase.architecture_phase == phase.architecture_phase
