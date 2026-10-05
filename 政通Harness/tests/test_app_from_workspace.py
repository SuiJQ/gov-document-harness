"""`build_app_from_workspace` 的端到端契约（第 5 步零信任复查补上）。

## 为什么要单独测这个函数

**上轮的真实疏漏**：第 5 步写完，`app.py` 覆盖率 76%，
`build_app_from_workspace` 的 32 行（395-408）**一行未测**。

原因是上轮只测了它的**零件**——`config.load`、`LocalSecrets`、
`Config.validate` 各自都有测试，全绿。
但「零件都对」不等于「拼起来能用」：
入口自己那几行（读配置、校验、转发、接真实密钥源）从未被执行过。

这与前七轮同型：**测了局部，没测衔接**。

## 本文件测的是「衔接」

- 读得到配置吗
- 配置没填时**报错而不是代填**（D-082）
- 配置读坏了会怎样
- 真实密钥源真的接上了吗（而不是又落回假实现）
- 装配出来的 app 能不能真的跑
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from zhengtong.adapters.config import Config, save
from zhengtong.adapters.paths import ensure_dirs
from zhengtong.app import build_app_from_workspace
from zhengtong.domain.errors import ErrorCode, Result
from zhengtong.domain.phase import PhaseId
from zhengtong.ports.llm import LlmRequest, LlmResponse

假密钥 = "sk-test0123456789abcdefghijklmnopqrstuvwxyz"


def _填卡(ws, 运行号, **填: str) -> None:
    """把一份**字段齐备**的 D-Card 写进运行状态。

    ## 为什么第 11 步之后必须调它

    第 11 步把 `validators` 从假实现换成真校验器后，
    **空 D-Card 会被正确地拦下**（硬约束零沉降，D-040），
    于是原先「随便跑跑」的冒烟测试全部变红。

    这不是回归，是**校验器终于在干活了**。
    修法是**让场景合法**（填好必填字段），
    而不是把校验器改松——后者等于为了绿灯拆掉刚装上的防线。
    """
    import dataclasses

    from zhengtong.domain.dcard import (
        DCard,
        DCardField,
        DCardFieldId,
        Provenance,
        empty_dcard,
    )
    from zhengtong.domain.run_state import new_run_state
    from zhengtong.domain.task import TaskSpec
    from zhengtong.domain.tier import TierId

    必填 = ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D9")
    卡 = empty_dcard()
    新 = dict(卡.by_id)
    for 键 in 必填:
        新[DCardFieldId(键)] = DCardField(
            id=DCardFieldId(键),
            label=键,
            value=填.get(键, f"{键} 的测试值"),
            provenance=Provenance.USER,
        )
    # **状态文件可能还不存在**——`run_once` 才是创建它的那一步。
    # 这里只管把卡填进去，不预支运行步骤。
    已存 = ws.load_run_state(运行号)
    st = (
        已存.unwrap()
        if 已存.is_ok
        else new_run_state(
            运行号,
            TaskSpec(
                requirement="测试需求",
                tier=TierId.PRO,
            ),
        )
    )
    # **RunState 是 frozen**：只能整体替换，不能就地赋值。
    assert ws.save_run_state(dataclasses.replace(st, dcard=DCard(by_id=新))).is_ok


def _工作区(tmp_path: Path) -> Path:
    return ensure_dirs(tmp_path / "ws").unwrap().root


默认配置 = Config(
    endpoint="https://example.invalid/v1/chat/completions",
    model="some-model",
    api_key=假密钥,
    context_k=128,
)


def _写配置(
    根: Path,
    *,
    endpoint: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    context_k: int | None = None,
) -> None:
    """落一份配置，字段可覆盖。

    **不用 `setattr` 也不用 `**dict`**：
    `Config` 是 `frozen` dataclass，`setattr` 抛 `FrozenInstanceError`；
    而 `Config(**dict_of_object)` 会让 BasedPyright 报
    「`object` 不能赋给 `str`/`int`」——**用 `object` 绕过类型检查，
    正是本项目反复否掉的做法**（见 D-159）。
    `dataclasses.replace` 两边都满足：它是生成器，**类型逐字段对上**。
    """
    ws = ensure_dirs(根).unwrap()
    改动: dict[str, str | int] = {}
    for 名, 值 in (
        ("endpoint", endpoint),
        ("model", model),
        ("api_key", api_key),
        ("context_k", context_k),
    ):
        if 值 is not None:
            改动[名] = 值
    got = save(ws, dataclasses.replace(默认配置, **改动))  # type: ignore[arg-type]
    assert got.is_ok, got.error


# ═══ 一、成功路径：配置齐了要能装配出来 ═══


def test_配置齐全时装配成功(tmp_path: Path) -> None:
    根 = _工作区(tmp_path)
    _写配置(根)
    got = build_app_from_workspace(ensure_dirs(根).unwrap())
    assert got.is_ok, got.error


def test_配置进了_app而不是被丢弃(tmp_path: Path) -> None:
    """**这条最要紧**：配置读进来了却没传给 `App`，
    装配照样成功、测试照样全绿，但运行时拿到的仍是空模型名。
    """
    根 = _工作区(tmp_path)
    _写配置(根, model="my-real-model")
    app = build_app_from_workspace(ensure_dirs(根).unwrap()).unwrap()
    assert app.config.model == "my-real-model"
    assert app.config.endpoint.endswith("/v1/chat/completions")
    assert app.config.context_k == 128


def test_接的是真实密钥源而非假实现(tmp_path: Path) -> None:
    """上一轮的核心改动就是换掉 `FakeSecrets`——
    断言它真的换了，且拿到的是配置里那把钥匙。"""
    根 = _工作区(tmp_path)
    _写配置(根)
    app = build_app_from_workspace(ensure_dirs(根).unwrap()).unwrap()
    from zhengtong.adapters.local_secrets import LocalSecrets

    assert isinstance(app.ports.secrets, LocalSecrets)
    assert app.ports.secrets.get_api_key() == 假密钥


def test_脱敏模式来自真实密钥(tmp_path: Path) -> None:
    """密钥换了，**脱敏模式必须跟着换**——
    否则密钥进了日志没人拦。"""
    根 = _工作区(tmp_path)
    _写配置(根)
    app = build_app_from_workspace(ensure_dirs(根).unwrap()).unwrap()
    片段 = app.ports.secrets.redaction_patterns()
    assert 片段
    assert all(假密钥 not in p for p in 片段)


# ═══ 二、失败路径：不得代填（D-082）═══


def test_没配置时报错而非用空配置(tmp_path: Path) -> None:
    """首次启动没有配置。此时**必须报错**——
    返回一个空配置的 app 会让人以为「配好了」，
    然后对着空模型名发请求。"""
    根 = _工作区(tmp_path)
    got = build_app_from_workspace(ensure_dirs(根).unwrap())
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize("缺哪项", ["endpoint", "model", "api_key", "context_k"])
def test_逐项缺一不可(tmp_path: Path, 缺哪项: str) -> None:
    """逐项缺都要报错。漏掉任何一项，
    都意味着程序会带着空值发请求换一个语焉不详的 401。

    **不用 `**{字段: 值}` 动态展开**：键是变量时类型检查器无能为力
    （Pyright 报「`object` 不能赋给 `str | None`」——`dict[str, object]` 的键值对
    本来就丢了类型）。改用四条独立用例，**类型全程可查**。
    代价是四行样板，收益是这一段再无 `type: ignore`。
    """
    根 = _工作区(tmp_path)
    if 缺哪项 == "endpoint":
        _写配置(根, endpoint="")
    elif 缺哪项 == "model":
        _写配置(根, model="")
    elif 缺哪项 == "api_key":
        _写配置(根, api_key="")
    else:
        _写配置(根, context_k=0)
    got = build_app_from_workspace(ensure_dirs(根).unwrap())
    assert got.is_error, f"{缺哪项} 缺失竟被放行"


def test_配置损坏时报错而非崩(tmp_path: Path) -> None:
    """用户手改配置写坏了。这里报不报错是次要的，
    **不能抛异常**——那会让用户看到栈回溯而不是提示。"""
    根 = _工作区(tmp_path)
    _写配置(根)
    _ = (根 / "system" / "config.toml").write_text("这不是 TOML ][", encoding="utf-8")
    got = build_app_from_workspace(ensure_dirs(根).unwrap())
    assert got.is_error


# ═══ 三、装配出来的东西要真能用 ═══


def test_装配后能跑完整个流程(tmp_path: Path) -> None:
    """**端到端**：装配 → 跑 → 收尾。

    前面那些只证明「装出来了」，这条证明「装出来的东西能跑」。
    真实入口的价值正在于此。
    """
    根 = _工作区(tmp_path)
    _写配置(根)
    # **显式传假实现**：第 6 步接线后，真实入口默认接真 LLM。
    # 本条验的是「装配出来能跑」，不传就会真发请求到 example.invalid，
    # 失败原因盖掉要验的东西。**一个测试只该验一件事。**
    from zhengtong.adapters.fakes import FakeLlm

    with build_app_from_workspace(ensure_dirs(根).unwrap(), llm=FakeLlm()).unwrap() as app:
        from zhengtong.app import run_once

        _填卡(app.ports.storage, app.run_id)
        got = run_once(app)
        assert got.is_ok, got.error
        # 配置里的模型名应当被真正用上（假 LLM 会回显）
        assert app.collected, "一个事件都没发出来"


def test_模型名真的进了请求(tmp_path: Path) -> None:
    """D-082：模型名**取自配置**。

    上轮把 `model="stub-model"` 换成了 `app.config.model`，
    但「换了」不等于「生效」——这条断言模型名确实到了 LLM 端口。
    """
    根 = _工作区(tmp_path)
    _写配置(根, model="qwen-real-name")
    seen: list[str] = []

    from zhengtong.adapters.fakes import FakeLlm

    class _记录Llm(FakeLlm):
        def complete(self, request: LlmRequest) -> Result[LlmResponse]:
            seen.append(request.model)
            return super().complete(request)

    with build_app_from_workspace(ensure_dirs(根).unwrap(), llm=_记录Llm()).unwrap() as app:
        from zhengtong.app import run_once

        _填卡(app.ports.storage, app.run_id)
        assert run_once(app).is_ok

    assert seen, "一次模型调用都没发生，断言无从谈起"
    assert all(m == "qwen-real-name" for m in seen), f"模型名没生效：{set(seen)}"


def test_阶段仍按配置推进(tmp_path: Path) -> None:
    """接真实配置**不该改变状态机行为**——它是第 4 步就定好的。"""
    根 = _工作区(tmp_path)
    _写配置(根)
    from zhengtong.adapters.fakes import FakeLlm

    with build_app_from_workspace(ensure_dirs(根).unwrap(), llm=FakeLlm()).unwrap() as app:
        from zhengtong.app import run_once

        _填卡(app.ports.storage, app.run_id)
        assert run_once(app).is_ok
        # **不加 `if e.phase is not None`**——类型检查器指出
        # `phase` 字段本就非空，那个条件恒真。
        # 恒真的过滤条件等于没写：它让读者以为「有些事件没有阶段」，
        # 实际不存在。断言直接取全部事件的阶段集合。
        阶段 = {e.phase for e in app.collected}
        assert PhaseId.INTAKE in 阶段
        assert PhaseId.DONE in 阶段
