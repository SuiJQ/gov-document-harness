"""七个编排回调的**真实装配路径**判据（D-411）。

## 为什么这组判据非有不可

D-408 把七个回调接上了。接完立刻发现一件事：
**既有判据全都看不见这次修复**。

`tests/test_tools.py` 里那些判据用的是**手搓的假依赖**：

```python
d.挂起 = 收到.append
运行 = 工具运行器(依赖=d, 审计=审计)
r = 运行.调用("flow.sleep", ...)
assert 收到 == ["等门禁"]
```

它验的是「处理器收到非 None 的回调会转发参数」——
**与真实装配根接的是哪个回调毫无关系**。

实测把「挂起」错接到「唤醒」上（两者都不为 `None`）：

- `check_wiring_runtime.py` 报 **0**（它只查 `is None`）
- **2143 个测试全绿**

即：接线**裸奔**。这与 D-165 同型——测试全绿，真跑起来什么也没做。

## 本文件验三件

1. **端到端**：造真 `build_app_from_workspace`（真工作区 + 真配置），
   真调工具，断言回调**真的被调用**且**参数原样送到**。
   不是断言 `is_ok`——未接线时 `is_ok` 是 False，接错时它**照样是 True**。
2. **接错可抓**：逐条锁定每个回调该连编排器哪个方法。
   用**可区分的假编排器**——每个方法打上自己的名字，
   错接任何一个，断言立刻对不上号。
3. **异常路径**：`编排器._派发中角色` 的 `try/finally` 真的复原了
   （D-408 加了防护但**没人验证防护有效**，与「加了却没验」同型）。

## 为什么用真工作区而不是 `build_app`

`build_app` 是测试入口、不持有工作区根（见 `app.py` 里导出器的注释）。
而**真实运行走的是 `build_app_from_workspace`**。
用 `build_app` 验接线，等于验了一条测试专用路径——
本轮 D-406 那七个缺陷当初就藏在「另一条路径上没传」。

## 端点地址

配置里写 `http://127.0.0.1:9/v1/chat/completions`（丢弃端口）。
**本文件一个模型请求都不该发出去**——只验接线，不验出稿。
真要有请求发出，`::tests` 会在 7 秒内失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from zhengtong.adapters.fakes import (
    FakeContentPack,
    FakeRetriever,
    FakeStorage,
    FakeUserFiles,
    FakeValidators,
)
from zhengtong.adapters.paths import Workspace
from zhengtong.app import build_app_from_workspace
from zhengtong.domain.errors import ErrorCode, Result
from zhengtong.domain.ids import RunId
from zhengtong.domain.role import RoleId
from zhengtong.ports.llm import FinishReason, LlmResponse

if TYPE_CHECKING:
    from zhengtong.app import App
    from zhengtong.orchestration.orchestrate import 编排器


def _写配置(根: Path) -> None:
    """造一份真配置。**直接写程序真会读的那份**（照 D-296 的做法）。"""
    (根 / "system").mkdir(parents=True, exist_ok=True)
    (根 / "system" / "config.toml").write_text(
        '[encoding]\ngb18030 = "false"\n\n'
        '[llm]\nendpoint = "http://127.0.0.1:9/v1/chat/completions"\n'
        'model = "m"\napi_key = "sk-x"\ncontext_k = 128\ntemperature = 0.3\n',
        encoding="utf-8",
    )


def _真应用(根: Path) -> tuple[App, 编排器]:
    """走**真实运行路径**装一个应用，并**建好编排器**。

    ## 为什么必须建编排器才返回

    七个回调是**后补**的（要持有编排器，而编排器在工具层之后才建——
    D-266 的鸡生蛋）。刚 `build_app_from_workspace` 出来时它们全是 `None`，
    `编排器()` 之后才接上。

    这个事实本身由 `test_装配前后对比` 固化；本辅助函数一律建到编排器，
    否则每条判据都要自己记得调一次，漏一处就报假失败
    （本次第一版就栽在这：断言写在 `编排器()` 之前，16 条全红）。
    """
    _写配置(根)
    got = build_app_from_workspace(Workspace(root=根))
    assert got.is_ok, got.error.detail if got.error else ""
    应用 = got.unwrap()
    return 应用, 应用.编排器()  # 建编排器 = 触发后补


# ═══ 判据 1：端到端，回调真的被调用且参数原样送到 ═══


class Test端到端七个回调:
    def test_汇报回调真被调用且内容原样送到(self, tmp_path: Path) -> None:
        """**不只断言 `is_ok`**——未接线时 `is_ok` 是 False，
        但接错成别的回调时它**照样是 True**，而内容就丢了。"""
        应用, 编排 = _真应用(tmp_path)
        try:
            编排._建过程()
            编排._派发中角色 = RoleId.STRUCTURE
            原句 = "汛情已核实，缺口三处"
            r = 应用.tools.调用(
                工具名="msg.report_to_lead",
                角色=RoleId.STRUCTURE,
                参数={"summary": 原句},
            )
            assert r.is_ok, f"汇报失败：{r.error}"
            # 成员的话要真的进主理人历史，且**原样**（不截断、不改写）。
            过程 = 编排._过程
            assert 过程 is not None
            历史 = 过程.历史.get(RoleId.TEAM_LEAD, ())
            正文 = "\n".join(m.content for m in 历史)
            assert 原句 in 正文, f"汇报内容没进主理人上下文：{正文[:200]!r}"
        finally:
            应用.close()

    def test_挂起回调把成员真置为挂起(self, tmp_path: Path) -> None:
        应用, 编排 = _真应用(tmp_path)
        try:
            编排._建过程()
            编排._派发中角色 = RoleId.STRUCTURE
            r = 应用.tools.调用(
                工具名="flow.sleep", 角色=RoleId.STRUCTURE, 参数={"reason": "等补充材料"}
            )
            assert r.is_ok, f"挂起失败：{r.error}"
            状态 = 编排._成员状态表().get("structure")
            assert 状态 == "挂起", f"成员状态没变：{编排._成员状态表()}"
        finally:
            应用.close()

    def test_唤醒回调登记条件且不立即放回运行中(self, tmp_path: Path) -> None:
        """**不立即放回运行中**——条件命中才该唤醒。"""
        应用, 编排 = _真应用(tmp_path)
        try:
            编排._建过程()
            编排._派发中角色 = RoleId.STRUCTURE
            r = 应用.tools.调用(
                工具名="flow.wake_on",
                角色=RoleId.STRUCTURE,
                参数={"event": "gate.passed"},
            )
            assert r.is_ok, f"设唤醒条件失败：{r.error}"
            assert 编排._唤醒条件.get(RoleId.STRUCTURE) == "gate.passed", (
                f"唤醒条件没登记：{编排._唤醒条件}"
            )
            assert 编排._成员状态表().get("structure") == "等待条件"
        finally:
            应用.close()

    def test_请示不回已批准而是置等待条件(self, tmp_path: Path) -> None:
        """**「严禁编造」**：主理人可能正忙，硬造批准就是编排器替它决定。"""
        应用, 编排 = _真应用(tmp_path)
        try:
            编排._建过程()
            编排._派发中角色 = RoleId.STRUCTURE
            r = 应用.tools.调用(
                工具名="msg.request_from_lead",
                角色=RoleId.STRUCTURE,
                参数={"question": "口径按哪一版？"},
            )
            assert r.is_ok, f"请示失败：{r.error}"
            回话 = r.unwrap()
            assert "已批准" not in 回话 and "同意" not in 回话, f"请示被硬造了批复：{回话}"
            assert 编排._成员状态表().get("structure") == "等待条件"
        finally:
            应用.close()

    def test_状态回调返回真实成员状态表(self, tmp_path: Path) -> None:
        应用, 编排 = _真应用(tmp_path)
        try:
            编排._建过程()
            r = 应用.tools.调用(工具名="flow.status", 角色=RoleId.TEAM_LEAD, 参数={})
            assert r.is_ok, f"查状态失败：{r.error}"
            assert "structure" in r.unwrap(), f"状态表不含成员：{r.unwrap()}"
        finally:
            应用.close()

    def test_回滚回调留下痕迹而不直接改门禁账(self, tmp_path: Path) -> None:
        """**只发事件、不改门禁账**——上限 2 次（D-050）是门禁循环的硬约束，
        另起一套就等于没有上限。"""
        应用, 编排 = _真应用(tmp_path)
        try:
            编排._建过程()
            编排._派发中角色 = RoleId.STRUCTURE
            过程 = 编排._过程
            assert 过程 is not None
            退回数 = 过程.轮次
            r = 应用.tools.调用(
                工具名="flow.rollback",
                角色=RoleId.TEAM_LEAD,
                参数={"step": "G3 措辞不合规"},
            )
            assert r.is_ok, f"回滚失败：{r.error}"
            assert 过程.轮次 == 退回数, "回滚私自改轮次——它无权决定重做次数"
            # **`Event` 没有 `detail` 字段**——正文在 `data` 里（D-266 的载荷口径）。
            退痕 = [e for e in 编排.事件 if "退回" in str(getattr(e, "data", {}))]
            assert 退痕, f"回滚没有留痕：{[e.kind.value for e in 编排.事件]}"
        finally:
            应用.close()

    def test_调用存档可查回工具输出(self, tmp_path: Path) -> None:
        """`tool.result` 据此查回上一轮的工具输出（D-408 补的那条）。"""
        应用, 编排 = _真应用(tmp_path)
        try:
            编排._建过程()
            r = 应用.tools.调用(工具名="tool.result", 角色=RoleId.TEAM_LEAD, 参数={"call_id": "无"})
            # 查不到时**必须失败且说清**，不得返回空串——
            # 空串会让模型以为「我查过了，确实没有」（编造证据的另一形态）。
            assert r.is_error, "查不到调用却报告成功"
            assert r.error is not None
        finally:
            应用.close()


# ═══ 判据 2：「接错」可抓 ═══


class _可区分编排器:
    """每个回调方法打上**自己的名字**。

    接错任何一个，断言立刻对不上号——
    而接错时返回值仍是成功，`is_ok` 断言**完全看不出来**。
    """

    def __init__(self) -> None:
        self.调用过: list[tuple[str, tuple[object, ...]]] = []

    def _记(self, 名: str, *参: object) -> None:
        self.调用过.append((名, 参))

    # 六个回调（记事件与转达是后补的，不在本组）
    def 汇报(self, 内容: str, 依据: dict[str, object]) -> str:
        self._记("汇报", 内容)
        return "答复"

    def 请示(self, 事项: str, 紧急: bool) -> str:
        self._记("请示", 事项, 紧急)
        return "已记下"

    def 挂起(self, 原因: str) -> None:
        self._记("挂起", 原因)

    def 唤醒(self, 事件名: str, 角色: str) -> None:
        self._记("唤醒", 事件名, 角色)

    def 回滚(self, 原因: str, 强制: bool) -> None:
        self._记("回滚", 原因)

    def 状态(self) -> dict[str, str]:
        self._记("状态")
        return {}


@pytest.mark.parametrize(
    ("工具名", "参数", "角色", "该走的回调"),
    [
        ("msg.report_to_lead", {"summary": "x"}, RoleId.STRUCTURE, "汇报"),
        ("msg.request_from_lead", {"question": "x"}, RoleId.STRUCTURE, "请示"),
        ("flow.sleep", {"reason": "x"}, RoleId.STRUCTURE, "挂起"),
        ("flow.wake_on", {"event": "x"}, RoleId.STRUCTURE, "唤醒"),
        ("flow.rollback", {"step": "x"}, RoleId.TEAM_LEAD, "回滚"),
        ("flow.status", {}, RoleId.TEAM_LEAD, "状态"),
    ],
)
def test_每个工具只走它自己那一个回调(
    工具名: str,
    参数: dict[str, object],
    角色: RoleId,
    该走的回调: str,
) -> None:
    """**接错时返回值仍是成功**——只有「哪个回调被调用」才区分得开。

    这条就是 D-408 那次注入证伪的固化：
    把「挂起」错接到「唤醒」上，本组判据会红，既有 2143 条不会。
    """
    from zhengtong.services.tools.handlers import 工具依赖
    from zhengtong.services.tools.runner import 工具运行器

    假 = _可区分编排器()
    d = 工具依赖(
        # 用**真实现**而非 None 占位：None 会让本组判据变成
        # 「在残缺依赖上验转发」，与「测试替身刚好够用」同族。
        storage=FakeStorage(),
        user_files=FakeUserFiles(),
        retriever=FakeRetriever(),
        content_pack=FakeContentPack(),
        validators=FakeValidators(),
        run_id=RunId("r-1"),
        汇报=假.汇报,
        请示=假.请示,
        挂起=假.挂起,
        唤醒=假.唤醒,
        回滚=假.回滚,
        状态=假.状态,
    )
    from zhengtong.services.tools.audit import 审计器

    运行 = 工具运行器(依赖=d, 审计=审计器())
    r = 运行.调用(工具名=工具名, 角色=角色, 参数=参数)
    assert r.is_ok, f"{工具名} 应当成功：{r.error}"
    走过 = [名 for 名, _ in 假.调用过]
    assert 走过 == [该走的回调], f"{工具名} 应只走「{该走的回调}」，实际走了 {走过}"


def test_装配根把六个回调逐一接到对应方法上(tmp_path: Path) -> None:
    """**装配根层面**的接错也要抓。

    上一条验的是「运行器转发到哪个回调」；这条验「装配根接的是哪个方法」——
    两处都可能接错，而 `check_wiring_runtime` 只查 `is None`，两处都漏。
    """
    应用, 编排 = _真应用(tmp_path)
    try:
        编排._建过程()
        d = 应用.tools._d
        for 名, 该连 in [
            ("汇报", 编排.工具回调.汇报),
            ("请示", 编排.工具回调.请示),
            ("挂起", 编排.工具回调.挂起),
            ("唤醒", 编排.工具回调.唤醒),
            ("回滚", 编排.工具回调.回滚),
            ("状态", 编排.工具回调.状态),
        ]:
            assert getattr(d, 名) == 该连, f"「{名}」接错了对象"
    finally:
        应用.close()


# ═══ 判据 3：异常路径的防护真的有效 ═══


def _空响应() -> LlmResponse:
    """一个**零工具调用**的响应。`usage` 必填——`LlmResponse` 没有默认。"""
    return LlmResponse(text="完成", finish_reason=FinishReason.STOP, usage=None)


@pytest.mark.parametrize("炸不炸", [True, False])
def test_派发无论成败都复原派发中角色(tmp_path: Path, 炸不炸: bool) -> None:
    """**加了 `try/finally` 却没人验证它有效**——与「加了却没验」同型。

    不复原的后果：残留上一位成员，于是下一个工具回调会把
    「谁在干活」报错到别人身上，而**不报任何错**。

    **成功路径也必须验**：只测异常路径的话，
    把 `finally` 写成 `except` 一样能通过。

    ## 为什么走 `_派发` 而不是直接调 `_跑工具`

    `try/finally` 在 `_派发` 的工具轮**调用点**（`orchestrate.py:1160`），
    不在 `_跑工具` 内部。直接调 `_跑工具` 会绕过那个包装——
    本条第一版就是这么写的，于是断言「未复原」而实际上代码是对的。
    """
    from zhengtong.domain.errors import Result
    from zhengtong.domain.tier import TierId
    from zhengtong.orchestration.orchestrate import _该段角色

    应用, 编排 = _真应用(tmp_path)
    try:
        过程 = 编排._建过程()
        真实角色 = _该段角色(TierId.PRO, 过程.状态.phase)[0]

        class 工具模型:
            def complete(self, req: object) -> Result[LlmResponse]:
                return Result.ok(_空响应())

        # 换掉模型是**本条的测试手段**，不是业务改动。
        过程.llm = 工具模型()  # pyright: ignore[reportAttributeAccessIssue]

        if 炸不炸:
            # **炸在 `_跑工具` 里**——即 `try/finally` 之内。
            # 炸在 `_问模型`（try 之外）时 `_派发中角色` 从未被设置，
            # 复原无从谈起，那不是缺陷。**这个区分必须守住**，
            # 否则本条要么测不到防护，要么测出一个不存在的问题。
            def 会炸(角色: object, 阶段: object, 响应: object) -> None:
                raise RuntimeError("boom")

            # 同上：替换方法是测试手段。
            过程._跑工具 = 会炸  # pyright: ignore[reportAttributeAccessIssue]
            with pytest.raises(RuntimeError):
                过程._派发(TierId.PRO, 过程.状态.phase)
        else:
            过程._派发(TierId.PRO, 过程.状态.phase)

        assert 编排._派发中角色 is None, f"未复原：{编排._派发中角色}"
        assert 真实角色 is not None
    finally:
        应用.close()


def test_未接线的错误码不再冒充角色越权(tmp_path: Path) -> None:
    """D-408 B1：`_未接线` 曾借用 `TOOL_NOT_ALLOWED`（E065「角色不在白名单内」）。

    实测过白名单**通过**却报 E065——排障一路查到角色配置去，
    而白名单没问题。撞码的代价是每次都得读完调用链才知道真因。
    """
    from zhengtong.services.tools.handlers import _未接线

    got = _未接线("flow.sleep", "挂起回调")
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.TOOL_NOT_WIRED, "未接线又借用了别人的错误码"
    assert got.error.code is not ErrorCode.TOOL_NOT_ALLOWED


# ═══ 判据 4：后补这件事本身要被看见 ═══


def test_装配前后对比_六个回调是后补的而非建时就接上(tmp_path: Path) -> None:
    """**把「后补」这个事实固化成判据**。

    七个回调要持有编排器，而编排器在工具层之后才建（D-266 的鸡生蛋），
    所以 `build_app_from_workspace` 刚返回时它们必然是 `None`。

    本条不是为了「测出 None」——那看着像缺陷。它是为了**说明白**：

    - `check_wiring_runtime.py` 必须建编排器再读值，否则误报一片；
    - 谁将来把 `编排器()` 改成缓存式单例，本条会立刻提示
      「后补的前提变了，判据要跟着改」。

    **不要因为它红就删它**——删了就把这个前提变成口头约定，
    而口头约定正是本项目反复吃亏的地方。
    """
    应用 = _裸应用(tmp_path)
    try:
        六 = ("汇报", "请示", "挂起", "唤醒", "回滚", "状态")
        建编排前 = [f for f in 六 if getattr(应用.tools._d, f) is None]
        assert set(建编排前) == set(六), (
            f"建编排器前不该全为 None，实际缺 {建编排前}——后补前提可能已变"
        )
        应用.编排器()
        建编排后 = [f for f in 六 if getattr(应用.tools._d, f) is None]
        assert not 建编排后, f"建编排器后仍未接：{建编排后}"
    finally:
        应用.close()


def _裸应用(根: Path) -> App:
    """**不**建编排器的应用——专供上一条用。"""
    _写配置(根)
    got = build_app_from_workspace(Workspace(root=根))
    assert got.is_ok, got.error.detail if got.error else ""
    return got.unwrap()


# ═══ 判据 5：重试次数必须从适配器带出来（D-412）═══


class _会重试的模型:
    """先失败 n 次再成功。**用真实失败路径**产出诊断，
    而不是在成功响应上手写 `重试次数=n`——后者验不到带出这一步。

    **参数是「先失败几次」**：`_会重试的模型(1)` = 第一次失败、第二次成功。
    写成 0 就直接成功了，于是没有 retry 可发——
    本条第一版就栽在这（事件列表空，还以为判据不对）。

    ## 为什么有第二个参数

    成功响应的 `重试次数` 是**另一个来源**：失败路径的次数来自
    `AppError.诊断`（适配器内部算的），成功路径的次数来自
    `LlmResponse.重试次数`（适配器在成功时挂上去的）。
    两者走的是**不同的代码分支**，必须分别验。

    第一版 `_会重试的模型(0)` 直接返回 `_空响应()`，
    而 `_空响应()` 的 `重试次数` 是默认 0——
    于是「成功路径也发事件」这条断言**永远不可能红**：
    它要的是一条带次数的成功响应，拿到的却是一条没次数的。
    这正是 `test_重试后成功也要留痕` 挂掉的原因。
    """

    def __init__(self, n: int, 成功重试次数: int = 0) -> None:
        self.剩下 = n
        self._成功重试次数 = 成功重试次数

    def complete(self, req: object) -> Result[LlmResponse]:
        from zhengtong.domain.errors import ErrorCode, Result

        if self.剩下 > 0:
            self.剩下 -= 1
            return Result.fail(
                ErrorCode.LLM_REQUEST_FAILED,
                "端点不通",
                {"重试次数": self.剩下 + 1, "已用间隔秒": 1.0},
            )
        return Result.ok(
            LlmResponse(
                text="完成",
                finish_reason=FinishReason.STOP,
                usage=None,
                重试次数=self._成功重试次数,
            )
        )


def test_重试次数从失败诊断里读出并发成事件(tmp_path: Path) -> None:
    应用, 编排 = _真应用(tmp_path)
    try:
        过程 = 编排._建过程()
        # **换掉模型**：`_过程.llm` 的静态类型是 `LlmPort` 协议，
        # 而本条要测的正是「失败路径带出诊断」这个实现细节。
        # 故 cast 成 `Any`——**不是**用 ignore 掩盖类型错误（D-018）。
        假模型 = cast(Any, _会重试的模型(2))
        过程.__dict__["llm"] = 假模型
        for _ in range(2):
            问 = 过程._构造请求(RoleId.STRUCTURE, 过程.状态.phase)
            过程._问模型(问, 过程.状态.phase, RoleId.STRUCTURE)
        重试 = [e for e in 编排.事件 if e.kind.value == "model.retry"]
        assert len(重试) == 2, f"两次重试应有 2 条：{[e.kind.value for e in 编排.事件]}"
        assert 重试[0].data["重试次数"] == 2
        assert 重试[0].data["最终失败"] is True
    finally:
        应用.close()


def test_重试后成功也要留痕(tmp_path: Path) -> None:
    """**成功路径同样要发**——「重试了 2 次才成功」与「一次就成」，
    在台账上是两回事，不记就看不出抖动。"""
    应用, 编排 = _真应用(tmp_path)
    try:
        过程 = 编排._建过程()
        # **先让模型失败两次**（真实失败路径带出诊断），
        # **第三次成功且带重试次数**——验的是「成功路径也发事件」。
        假模型 = cast(Any, _会重试的模型(2))
        过程.__dict__["llm"] = 假模型
        for _ in range(2):
            问 = 过程._构造请求(RoleId.STRUCTURE, 过程.状态.phase)
            过程._问模型(问, 过程.状态.phase, RoleId.STRUCTURE)
        # **换模型而不是复用**：`_会重试的模型` 是有状态的，
        # 复用同一个实例会让 `剩下` 已为 0，第三次虽成功却**不带次数**。
        # 故重新造一个，并显式传「成功时挂几次」——
        # 那个次数来自 `LlmResponse.重试次数`（成功路径），
        # 与失败路径的 `AppError.诊断` 是两个来源，得分别给值。
        假模型 = cast(Any, _会重试的模型(0, 成功重试次数=2))
        过程.__dict__["llm"] = 假模型
        问 = 过程._构造请求(RoleId.STRUCTURE, 过程.状态.phase)
        过程._问模型(问, 过程.状态.phase, RoleId.STRUCTURE)
        重试 = [e for e in 编排.事件 if e.kind.value == "model.retry"]
        assert len(重试) == 3, (
            f"应有 3 条（2 次失败 + 1 次成功）：{[e.kind.value for e in 编排.事件]}"
        )
        assert 重试[-1].data["重试次数"] == 2
        assert 重试[-1].data["最终失败"] is False
    finally:
        应用.close()


def test_未重试时不发_model_retry(tmp_path: Path) -> None:
    """**没重试就不该有事件**——否则满屏噪声，
    而满屏噪声的检查等于没有检查（D-247）。"""
    应用, 编排 = _真应用(tmp_path)
    try:
        过程 = 编排._建过程()
        假模型 = _会重试的模型(0)
        过程.__dict__["llm"] = 假模型
        问 = 过程._构造请求(RoleId.STRUCTURE, 过程.状态.phase)
        过程._问模型(问, 过程.状态.phase, RoleId.STRUCTURE)
        assert not [e for e in 编排.事件 if e.kind.value == "model.retry"]
    finally:
        应用.close()


def test_真适配器端点不通时真的带出次数(tmp_path: Path) -> None:
    """**用真 `HttpLlm`**：上面的判据验的是编排层能读，
    这条验适配器真的把次数带出来了。

    端点写 `127.0.0.1:9`（丢弃端口）——连接必然失败，
    且把退避 sleep 换掉，否则真等 1+2 秒。
    """
    应用, 编排 = _真应用(tmp_path)
    try:
        过程 = 编排._建过程()
        # **换掉退避等待**：否则真等 1+2 秒。次数上限调到 2，测试才跑得快。
        # `过程.llm` 静态类型是 `LlmPort`，而退避时钟与重试上限是
        # `HttpLlm` 的实现细节——本条要测的正是实现细节，故 cast 成 `Any`。
        # **不是**用 ignore 掩盖类型错误（D-018）。
        真件 = cast(Any, 过程.llm)
        真件._clock.sleep = lambda _秒: None  # 换掉退避等待，否则真等 1+2 秒
        真件._max_retries = 2  # 上限降到 2 次，测试才跑得快
        # **传真请求**：`None` 会让 `_请求体` 在 `request.messages` 上炸，
        # 根本走不到重试循环——那就白测了。
        问 = 过程._构造请求(RoleId.STRUCTURE, 过程.状态.phase)
        过程._问模型(问, 过程.状态.phase, RoleId.STRUCTURE)
        重试 = [e for e in 编排.事件 if e.kind.value == "model.retry"]
        assert 重试, f"真适配器重试了却没留痕：{[e.kind.value for e in 编排.事件]}"
        assert 重试[0].data["重试次数"] == 2
        assert 重试[0].data["最终失败"] is True
        错 = [e for e in 编排.事件 if e.kind.value == "model.error"]
        assert 错, "最终失败却没有 model.error"
    finally:
        应用.close()
