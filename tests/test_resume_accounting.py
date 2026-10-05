"""缺陷 8 与 9：幂等记账与断点续跑。

## 为什么这两条能在 1957 条判据全绿的情况下存在

- `RunState.mark_completed` / `is_call_completed` / `put_artifact`
  只在 `tests/test_serialization.py` 里被测过，**生产零调用**
- `load_run_state` 唯一出现的地方是 `app.py` 的 `run_once`，
  而那是个**死函数**（只有 tests 调）

于是判据测的是「这个方法能用」，不是「运行时有没有人用它」。
实跑之后 `state.json` 里 `completed_call_ids=[]`、`artifacts={}`。

## 这组判据断的是「真跑一遍之后账上有没有东西」

不查方法存不存在（那个必绿），只查跑完之后的账。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from zhengtong.adapters.content_pack import 本地内容包
from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.domain.errors import Result
from zhengtong.domain.ids import RunId
from zhengtong.domain.phase import PhaseId
from zhengtong.domain.role import RoleId
from zhengtong.domain.run_state import RunState, new_run_state
from zhengtong.domain.task import TaskSpec
from zhengtong.domain.tier import TierId
from zhengtong.orchestration.orchestrate import 编排器
from zhengtong.ports.llm import LlmRequest, LlmResponse


def _编排器源码() -> str:
    """编排器模块的源码。

    `inspect.getmodule` 返回 `ModuleType | None`——直接喂给 `getsource`
    类型上就说不通。更要紧的是：模块找不到时它给 `None`，
    而 `getsource(None)` 抛 `TypeError`——**判据会红在类型错误上，
    而不是红在「方法真的没被调用」这件事上**。
    故先断言再取源码。
    """
    模 = inspect.getmodule(编排器)
    assert 模 is not None, "定位不到编排器模块——判据本身失效，不是被测代码的问题"
    return inspect.getsource(模)


def _末态(仓: FakeStorage, 运行号: RunId | None = None) -> RunState:
    """取该运行号的末态。

    **注意 `states` 是 dict 不是 list**——我第一版写 `_末态(仓)`
    直接 `KeyError: -1`。判据自己也会有错，跑一遍才知道。
    """
    # **`RunId` 是 NewType，`.value` 是函数不是字符串**——
    # 第一版写 `运行号.value` 拿到函数对象，dict 查不到，落成 KeyError。
    键 = str(运行号 or RunId("r-0001"))
    assert 仓.states, "save_run_state 一次都没调用"
    return 仓.states[键]


def _编排(storage: FakeStorage | None = None) -> 编排器:
    # **必须接真实内容包**：不接就落到 `[内容包未接入]` 降级提示，
    # 而门禁取证的输出契约与「本轮门禁指派」两块**都在内容包里**。
    # 降级下模型既不知道要守什么格式、也不知道该答哪几道门禁，
    # 于是 12 道门禁全缺席、D-221 转用户决策，Pro/Flash 第一遍
    # 走不到 FINAL——**失败点离真因隔着两层**，极难查。
    #
    # 这就是 D-165 的又一次：真配置跑通、弱配置绿的判据。
    # 判据要验「跑完之后账上有没有东西」，就该在真装配下跑。
    return 编排器(
        llm=FakeLlm(),
        storage=storage or FakeStorage(),
        运行号=RunId("r-0001"),
        模型名="假模型",
        content_pack=_内容包(),
    )


_内容包缓存: 本地内容包 | None = None


def _内容包() -> 本地内容包:
    """真实内容包（仓库里的 `content/`），**只建一次**。

    每次 `manifest.verify_manifest()` 要重算 22 个文件的 sha256，
    1981 条判据里反复建实例纯属浪费。
    """
    global _内容包缓存
    if _内容包缓存 is None:
        _内容包缓存 = 本地内容包(根=Path(__file__).resolve().parents[1] / "content")
        assert _内容包缓存.verify_manifest().is_ok, "内容包 sha256 对不上，改过文件没同步清单"
    return _内容包缓存


class Test产物真记账:
    def test_跑完state里有产物(self) -> None:
        """**产物落盘了就得记账**。

        此前 `put_artifact` 生产零调用，于是文件在盘上、账上空着。
        """
        仓 = FakeStorage()
        编 = _编排(storage=仓)
        got = 编.跑到终(档位=TierId.CONSULT, 需求="咨询老旧小区改造")
        assert got.is_ok, got.error

        存 = _末态(仓)
        assert 存.artifacts, "跑完 state.json 的 artifacts 仍是空的——产物落盘了却没记账"

    def test_产物账与实际落盘一致(self) -> None:
        """**账上的东西必须真有文件**，反之亦然。"""
        仓 = FakeStorage()
        编 = _编排(storage=仓)
        编.跑到终(档位=TierId.CONSULT, 需求="咨询老旧小区改造")

        记了 = set(_末态(仓).artifacts)
        真有 = {k.rsplit("/", 1)[-1] for k in 仓.artifacts}
        assert {p.rsplit(":", 1)[-1] for p in 记了} == 真有, f"账上 {记了} 与盘上 {真有} 对不上"


class Test已完成调用真记账:
    def test_跑完state里有已完成调用(self) -> None:
        仓 = FakeStorage()
        编 = _编排(storage=仓)
        编.跑到终(档位=TierId.CONSULT, 需求="咨询老旧小区改造")
        assert _末态(仓).completed_call_ids, "跑完 completed_call_ids 仍是空的——续跑无从判幂等"

    def test_每个跑过的阶段都记了一条(self) -> None:
        """**逐段对照**：咨询档两段，该有两条。

        **按 `is_call_completed` 查，不按字符串找阶段名**——
        `call_id` 是 sha256，里面没有阶段名。原版写
        `any(段.value in i for i in ids)`，**永远为假**，
        于是这条判据一直红着，而它本该是最直接的那条。
        """
        from zhengtong.domain.event import make_call_id

        仓 = FakeStorage()
        _编排(storage=仓).跑到终(档位=TierId.CONSULT, 需求="咨询老旧小区改造")
        态 = _末态(仓)
        for 段 in (PhaseId.INTAKE, PhaseId.FINAL):
            cid = make_call_id("prompt", "r-0001", 段.value, role=RoleId.TEAM_LEAD.value)
            assert 态.is_call_completed(cid), f"{段.value} 段没记进已完成（查的 id：{cid}）"

    def test_完成身份稳定不随次数变(self) -> None:
        """**同一段重跑必须算出同一个 id**。

        用时间戳或序号的话，每次跑出来的 id 都不同，
        幂等判定永远不命中——**等于没记**。
        """
        两次 = []
        for _ in range(2):
            仓 = FakeStorage()
            _编排(storage=仓).跑到终(档位=TierId.CONSULT, 需求="同一需求")
            两次.append(_末态(仓).completed_call_ids)
        assert 两次[0] == 两次[1], f"完成身份不稳定：{两次[0]} vs {两次[1]}"

    def test_记账方法在生产代码里被调用(self) -> None:
        """**掐死「又变回零调用」**。"""
        源 = _编排器源码()
        assert ".mark_completed(" in 源, "编排器不再调用 mark_completed——记账退回死代码"
        assert ".put_artifact(" in 源, "编排器不再调用 put_artifact"


class Test续跑真的续:
    def test_第二次跑不重发已完成的段(self) -> None:
        """**缺陷 9 的正面判据**。

        先跑一遍落盘，再用**同一个运行号**跑第二遍——
        第二遍应当因为 `completed_call_ids` 而不重复请求。

        此前 `app.py` 从不传 `初始状态`，`编排器` 也从不读盘，
        于是第二次从零开始，**全部请求重发一遍**。
        """

        class _记账Llm(FakeLlm):
            """数请求次数。计数器的增删只在此一处，其余判据只看它最后的数。"""

            def __init__(self) -> None:
                super().__init__()
                self.请求数 = 0

            # **按真签名覆盖，不用 `*a: object`**：
            # 那样 `super().complete(*a)` 展开出来是 `object`，
            # 传给 `LlmRequest` 形参类型上不通——而两个
            # `type: ignore` 盖住的正是「懒得写对签名」。
            # 端口签名真变了，这里本来就该跟着断。
            def complete(self, request: LlmRequest) -> Result[LlmResponse]:
                self.请求数 += 1
                return super().complete(request)

        仓 = FakeStorage()
        第一 = _记账Llm()
        编排器(
            llm=第一,
            storage=仓,
            运行号=RunId("r-续跑"),
            模型名="假",
        ).跑到终(档位=TierId.CONSULT, 需求="咨询老旧小区改造")
        assert 第一.请求数 > 0, "第一遍就没发请求，判据无从对比"

        第二 = _记账Llm()
        编排器(
            llm=第二,
            storage=仓,
            运行号=RunId("r-续跑"),
            模型名="假",
        ).跑到终(档位=TierId.CONSULT, 需求="咨询老旧小区改造")

        assert 第二.请求数 == 0, f"续跑又发了 {第二.请求数} 次请求——completed_call_ids 没被读回"

    @pytest.mark.parametrize("档", list(TierId))
    def test_三档续跑都不重发(self, 档: TierId) -> None:
        """三档都验一遍，别只挑咨询档。"""
        仓 = FakeStorage()

        class _数(FakeLlm):
            def __init__(self) -> None:
                super().__init__()
                self.n = 0

            # **按真签名覆盖，不用 `*a: object`**：
            # 那样 `super().complete(*a)` 展开出来是 `object`，
            # 传给 `LlmRequest` 形参类型上不通——而两个
            # `type: ignore` 盖住的正是「懒得写对签名」。
            # 端口签名真变了，这里本来就该跟着断。
            def complete(self, request: LlmRequest) -> Result[LlmResponse]:
                self.n += 1
                return super().complete(request)

        一 = _数()
        编排器(
            llm=一,
            storage=仓,
            运行号=RunId(f"r-{档.value}"),
            模型名="假",
            content_pack=_内容包(),  # type: ignore[arg-type]
        ).跑到终(档位=档, 需求="需求")
        二 = _数()
        编排器(
            llm=二,
            storage=仓,
            运行号=RunId(f"r-{档.value}"),
            模型名="假",
            content_pack=_内容包(),  # type: ignore[arg-type]
        ).跑到终(档位=档, 需求="需求")
        assert 一.n > 0, f"{档} 第一遍没发请求"
        assert 二.n == 0, f"{档} 续跑又发了 {二.n} 次请求——已完成的调用没被读回"

    @pytest.mark.parametrize("档", [TierId.FLASH, TierId.PRO])
    def test_成文档第一遍就走完全程(self, 档: TierId) -> None:
        """**续跑判据的前置条件**：成文档第一遍得真走到 FINAL。

        ## 这条曾长期红，根因**不是**它当初写的那个

        旧注释断言「那一段卡住属门禁退回重写（D-050，零实现零判据）」。
        实测不是：门禁段根本没进到「退回重写」那一步——

        1. `门禁取证` 注入点没有真实现（`lambda _门禁: {}` 兜底），
           12 道门禁的结论恒为空；
        2. D-221 缺席判据把「一道结论都没交」转成用户裁决，
           Pro/Flash 第一遍就停在 `drafting`，走不到 FINAL；
        3. 而门禁取证的输出契约与指派清单都在内容包里，不接内容包
           就落到 `[内容包未接入]` 降级提示——即便接线了也照样缺席。

        三层里任何一层都能让这条红，而「D-050 退回重写」是第四层，
        压根没走到。**归因错了会让人去修错的地方**。
        """
        仓 = FakeStorage()
        编排器(
            llm=FakeLlm(),
            storage=仓,
            运行号=RunId("r-x"),
            模型名="假",
            content_pack=_内容包(),  # type: ignore[arg-type]
        ).跑到终(档位=档, 需求="需求")
        assert "r-x/final.md" in 仓.artifacts, (
            f"{档} 第一遍没走到 FINAL（产物只有 {sorted(仓.artifacts)}）——"
            "续跑判据在此档位上无法真正验证"
        )

    def test_续跑会读盘(self) -> None:
        """**掐死「读盘那行被删掉」**。"""
        源 = _编排器源码()
        assert "load_run_state" in 源, "编排器不再读盘——续跑退回从头开始"

    def test_首次运行读不到状态不算错(self) -> None:
        """**没有状态文件是正常的**（首跑、或上次崩在落盘前）。

        读盘失败不应当成运行失败——否则首跑直接报错。
        """
        got = _编排().跑到终(档位=TierId.CONSULT, 需求="首跑")
        assert got.is_ok, f"首跑被读盘失败挡住了：{got.error}"

    def test_调用方显式传入的初始状态优先(self) -> None:
        """显式传入的初始状态比盘上的新——**参数优先于隐式读取**。"""
        仓 = FakeStorage()
        编排器(llm=FakeLlm(), storage=仓, 运行号=RunId("r-x"), 模型名="假").跑到终(
            档位=TierId.CONSULT, 需求="跑过"
        )
        显式 = new_run_state(RunId("r-x"), TaskSpec(tier=TierId.CONSULT, requirement="显式"))
        编 = _编排(storage=仓)
        编.初始状态 = 显式
        编.运行号 = RunId("r-x")
        got = 编.跑到终(档位=TierId.CONSULT, 需求="显式的")
        assert got.is_ok
