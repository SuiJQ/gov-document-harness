"""接线判据：真实运行入口装的是不是真件。

## 为什么这类判据要单独立文件

D-165 一共发生了五次，llm / retriever / user_files / storage / 路径守卫，
**症状完全一样**：真配置配好、测试全绿、实际什么也没真跑。

共同的 fooled 方式是「读一眼代码觉得接上了」。
故这类判据只有一条规矩：**不许读实现，直接跑真实入口**，
断言装出来的对象是什么类型。

## 每条都对应一笔决策

| 判据 | 决策 |
|---|---|
| validators 装真校验器 | D-196（第 11 步还清） |
| 其余端口仍接真实现 | D-165（防第 6 次） |
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhengtong.adapters.config import Config, save
from zhengtong.adapters.fakes import (
    FakeLlm,
    FakeStorage,
    FakeUserFiles,
    FakeValidators,
)
from zhengtong.adapters.paths import Workspace, ensure_dirs
from zhengtong.adapters.validators import 硬约束校验器
from zhengtong.app import build_app_from_workspace, run_once
from zhengtong.domain.errors import ErrorCode
from zhengtong.services.retrieval import 中文检索


@pytest.fixture
def 工作区(tmp_path: Path) -> Workspace:
    """造一个配置齐全的工作区。

    **复用既有写法**（`test_app_from_workspace.py` 的 `_写配置`），
    不自造夹具——两处造法分叉就会有一处测不到真实路径。
    """

    ws = ensure_dirs(tmp_path / "ws").unwrap()
    got = save(
        ws,
        Config(
            endpoint="https://example.invalid/v1/chat/completions",
            model="some-model",
            api_key="sk-test0123456789abcdefghijklmnopqrstuvwxyz",
            context_k=128,
        ),
    )
    assert got.is_ok, got.error
    return ws


def test_不传validators时装真校验器(工作区: Workspace) -> None:
    """**D-196 的本体判据。**

    第一版这里写成 `assert isinstance(..., 硬约束校验器)` 之外还留了个
    假占位（在别的类里 `assert ws is None` 加注释说「见别的文件」）——
    那是永远绿的空壳，看着像有测试，实际什么都没验，已删。
    """

    got = build_app_from_workspace(工作区)
    assert got.is_ok, got.error
    装上的 = got.unwrap().ports.validators
    assert isinstance(装上的, 硬约束校验器), (
        f"真实入口装的是 {type(装上的).__name__}，硬约束零沉降没真跑"
    )


def test_其余真件没被这次改动带坏(工作区: Workspace) -> None:
    """**一条判据只该验一件事**——但 D-165 已被犯过五次，
    故这里同时钉住当前应当接真实现的五个端口。

    这是「防第 6 次」的闸门：谁再漏一个，这里立刻红。
    """

    装上的 = build_app_from_workspace(工作区).unwrap().ports
    assert not isinstance(装上的.validators, FakeValidators)
    assert not isinstance(装上的.llm, FakeLlm)
    assert not isinstance(装上的.storage, FakeStorage)
    assert not isinstance(装上的.user_files, FakeUserFiles)
    assert isinstance(装上的.retriever, 中文检索)


def test_显式传入时尊重调用方(工作区: Workspace) -> None:
    """「不传接真件」不等于「强制真件」。

    测试要能塞假件进来，故显式传入必须被尊重——
    否则整条测试链只能跑真实现，慢且脆。
    """

    假件 = FakeValidators()
    got = build_app_from_workspace(工作区, validators=假件)
    assert got.is_ok
    assert got.unwrap().ports.validators is 假件


def test_空卡会被真校验器拦下(工作区: Workspace) -> None:
    """**本轮新行为的本体判据**，也是 `test_app_from_workspace` 那 3 条变红的原因。

    换成真校验器后，三条冒烟测试红了——因为**空 D-Card 确实该被拦**
    （硬约束零沉降，D-040）。它们变红不是回归，是校验器终于在干活。

    所以必须有这一条钉住：装真件之后，空卡**必须**跑不过。
    否则「把那 3 条测试改绿」就可能变成了绕过校验器而非补全场景——
    **这两种修法在结果上都让测试变绿，只看绿灯分辨不出来。**
    """
    with build_app_from_workspace(工作区, llm=FakeLlm()).unwrap() as app:
        got = run_once(app)  # **不填卡**
        assert got.is_error, "空 D-Card 竟然过了硬约束"
        assert got.error is not None
        assert got.error.code == ErrorCode.GATE_REWRITE_EXHAUSTED

        装上的 = app.ports.validators
        结论 = 装上的.run_hard_constraint({"dcard": {}}).unwrap()
        assert not 结论.passed
        assert len(结论.issues) >= 8, "八个必填字段应逐条报出问题"
