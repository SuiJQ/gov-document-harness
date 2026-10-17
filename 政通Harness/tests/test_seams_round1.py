"""第一轮增量检查（接缝专项）：编排层与前 11 层的接缝。

## 为什么单独一个文件

第 13 步交出时，35 条判据全绿，而五问接缝**全部为零**：
`bus`/`emit` 出现 0 次、`call_id` 出现 0 次、无一处 import 适配层、
发给模型的是手搓的两条 Message、`model=""`。

**判据查的是「编排层自己能不能跑」，接缝缺陷恰恰是
「编排层能跑，但和别的层对不上」。**
判据范围太窄——与第 13 步那次「判据注释吹牛」是同一类，
只是方向不同（那次是能力吹大，这次是范围缩窄）。

## 这里的每条判据都对应一个具体接缝

| 接缝 | 判据 |
|---|---|
| 编排 → 事件流 | `test_编排能产事件且seq连续` |
| 编排 → 内核 drive | `test_编排可被drive驱动` |
| 编排 → 装配器 | `test_发出去的是装配器的产物` |
| 编排 → call_id 幂等 | `test_同一语义键重试id不变` |
| 编排 → 装配根 | `test_走build_app装真件` |
| 编排 → 端口契约 | `test_模型名来自配置不是空串` |
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from zhengtong.adapters.fakes import FakeContentPack, FakeLlm, FakeStorage
from zhengtong.adapters.paths import Workspace, ensure_dirs
from zhengtong.app import 内容包根
from zhengtong.domain.errors import Result
from zhengtong.domain.event import EventKind, make_call_id
from zhengtong.domain.ids import RunId
from zhengtong.domain.role import RoleId
from zhengtong.domain.run_state import GateRecord
from zhengtong.domain.tier import TierId
from zhengtong.orchestration.gates_forging import 门禁取证请求
from zhengtong.orchestration.orchestrate import 编排器
from zhengtong.ports.llm import LlmRequest, LlmResponse


def _全过(请求: 门禁取证请求) -> dict[str, GateRecord]:
    """门禁全过的取证脚本。"""
    return {g.value: GateRecord(gate=g.value, passed=True) for g in 请求.门禁}


def _全过编排器() -> 编排器:
    """门禁全部通过的编排器。

    第 14 步起编排按全集核对取证结论，**缺席即无证据**（D-221/E041）。
    本文件这批接缝判据关心事件流与幂等，不关心门禁判定，故统一注入
    全过结论——不是放宽，驳回与无证据由 test_step14_* 覆盖。

    **顺手定档 PRO**：`取一步()` 要求档位已设（D-380 起编排器不再有
    静默默认档）。本文件的 `drive` 接缝判据**故意不走 `跑到终`**——
    走它就验不到「drive 才是调度边界」，所以只能自己把档设上。
    设的是编排器自己那个公开字段（`跑到终` 写的也是它），
    不开测试专用的设档口子：那等于再造一条生产不走的路径
    （D-406「另一条路径上没传」就是这么来的）。
    """
    器 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 门禁取证=_全过)
    器.档位 = TierId.PRO
    return 器


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """**配置齐全**的工作区。

    `build_app_from_workspace` 读的是真配置，缺字段直接报 `CONFIG_INVALID`
    ——而「模型名只能来自配置」（D-082），所以这条接缝判据
    **必须有一份真配置**，否则测的是「配置没填」不是「模型名接上了」。
    """
    from zhengtong.adapters.config import Config, save

    工作区 = ensure_dirs(tmp_path / "ws").unwrap()
    got = save(
        工作区,
        Config(
            endpoint="https://example.invalid/v1/chat/completions",
            model="配置里的真模型名",
            api_key="sk-test0123456789abcdefghijklmnopqrstuvwxyz",
            context_k=128,
        ),
    )
    assert got.is_ok, got.error
    return 工作区


# ═══ 接缝一：编排 → 事件流 ═══


class Test编排发射事件:
    def test_编排能产事件(self) -> None:
        """**第 13 步交出时这里是 0 次。**

        而架构 §8.1 要求编排层产事件流、第 16 步 TUI 靠它拿进度。
        零事件意味着 TUI 接不上。
        """

        器 = _全过编排器()
        器.跑到终(档位=TierId.PRO, 需求="测试需求")
        assert 器.事件, "编排跑完了却一个事件都没发"

    def test_seq连续无重复无空洞(self) -> None:
        """**seq 是事件的唯一排序锚点**（D-046）。

        重复会让回放错乱，空洞会让「漏了一条」查不出来。
        """

        器 = _全过编排器()
        器.跑到终(档位=TierId.PRO, 需求="测试需求")
        序号 = [e.seq for e in 器.事件]
        assert 序号 == list(range(1, len(序号) + 1)), f"seq 不连续：{序号[:20]}"

    def test_每个阶段都有started与exited(self) -> None:
        """阶段边界必须成对——**只 started 不 exited 等于没结束**。

        只发 started 的阶段在界面上永远转圈。
        """

        器 = _全过编排器()
        器.跑到终(档位=TierId.PRO, 需求="测试需求")
        起点 = [e for e in 器.事件 if e.kind is EventKind.PHASE_STARTED]
        终点 = [e for e in 器.事件 if e.kind is EventKind.PHASE_EXITED]
        assert len(起点) == len(终点), f"started {len(起点)} 个、exited {len(终点)} 个——不成对"
        assert 起点, "一个阶段都没开始过"

    def test_运行有始有终(self) -> None:

        器 = _全过编排器()
        器.跑到终(档位=TierId.PRO, 需求="测试需求")
        种类 = [e.kind for e in 器.事件]
        assert EventKind.RUN_STARTED in 种类
        assert EventKind.RUN_FINISHED in 种类

    def test_阶段成果事件载全文(self) -> None:
        """D-072/D-046：`ARTIFACT_READY` **必须带成果全文**，
        不是摘要——界面拿到的与落盘的要是同一份。"""

        器 = _全过编排器()
        器.跑到终(档位=TierId.PRO, 需求="测试需求")
        成果 = [e for e in 器.事件 if e.kind is EventKind.ARTIFACT_READY]
        assert 成果, "没有成果事件"
        assert all(e.data.get("content") for e in 成果), "成果事件没带全文"


# ═══ 接缝二：编排 → 内核 drive ═══


class Test可被drive驱动:
    def test_编排可被drive驱动(self) -> None:
        """架构 §8.1：三种接入方式**共用同一份驱动循环**（D-037）。

        第 13 步的编排是 `for` 循环直跑，`drive()` 一次没用——
        于是第 16 步 TUI 接不上，且 `app.py` 的 `run_once`
        与它是两套并行的流程逻辑，将来必然分叉。
        """
        from zhengtong.kernel.bus import EventBus
        from zhengtong.kernel.driver import drive
        from zhengtong.kernel.redactor import Redactor
        from zhengtong.orchestration.orchestrate import 编排器

        总线 = EventBus(Redactor())
        器 = 编排器(llm=FakeLlm(), storage=FakeStorage(), bus=总线, 门禁取证=_全过)
        器.档位 = TierId.PRO  # 同 `_全过编排器`：本条自带总线，故自己定档
        got = drive(
            run_id=RunId("r-drive"),
            bus=总线,
            initial_state=0,
            step=器.取一步(),
        )
        assert got.is_ok, got.error
        assert got.unwrap().ok, "drive 报失败"
        # `drive` 自己数步数（`DriveOutcome.steps`）——
        # 查它而不查编排器的字段，因为**查编排器自己的计数
        # 等于让它自己给自己打分**。
        assert got.unwrap().steps >= 5, f"只走了 {got.unwrap().steps} 步"

    def test_一次一阶段而非一口气跑完(self) -> None:
        """`drive` 每调一次 `step` 走一个阶段——**它才是调度边界**。

        若 `step` 里一口气跑完全部阶段，`drive` 就退化成空壳，
        而续跑（`resume_from`）与 TUI 的检查点插话全部失效。
        """

        器 = _全过编排器()
        一步 = 器.取一步()
        第一次 = 一步(0)
        assert 第一次.is_ok
        assert not 第一次.unwrap().done, "第一步就说做完了——没分段"
        阶段一 = 第一次.unwrap().phase

        第二次 = 一步(1)
        assert 第二次.unwrap().phase is not 阶段一, "两次同一阶段——没推进"


# ═══ 接缝三：编排 → 装配器 ═══


class Test用装配器:
    def test_发出去的是装配器的产物(self) -> None:
        """**第 13 步交出时发的是手搓 Message**：

        ```
        Message(role="system", content=f"你是{角色.value}，当前阶段 …")
        ```

        第 11 步那套装配（五块 + 按需装载 + D-Card 事实块 + 预算闸门）
        一次都没被调用过，**角色提示词正文（内容包里那份）完全没用上**。

        判据查的是「发出去的内容里有没有角色提示词正文」——
        而第 13 步那版永远不会包含它。
        """
        from zhengtong.domain.role import RoleId
        from zhengtong.orchestration.orchestrate import 编排器
        from zhengtong.services.prompting import 装配提示词, 装配请求, 预算闸门

        期望 = 装配提示词(
            请求=装配请求(角色=RoleId.STRUCTURE),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        ).提示词正文

        器 = 编排器(llm=FakeLlm(), storage=FakeStorage(), content_pack=FakeContentPack())
        器.跑到终(档位=TierId.PRO, 需求="测试需求")
        发出的 = [r.messages[0].content for r in 器.发出的请求]
        assert any("structure 的角色提示词" in m for m in 发出的), (
            "发出去的消息里没有角色提示词正文——装配器没被调用"
        )
        assert not any(m.startswith("你是") for m in 发出的), "还在发手搓的「你是 xxx」"
        assert 期望  # 装配器本身能工作（本条只是守住前提）

    def test_无内容包时显式标降级而非冒充(self) -> None:
        """**第 13 步发的是「你是 xxx，当前阶段 yyy」**——
        看着像角色提示词，实际不含任何写作标准。

        判据查的是：没内容包时**必须明说这是降级提示**。
        否则「有没有装配」这件事在日志里看不出来。
        """

        器 = _全过编排器()
        器.跑到终(档位=TierId.PRO, 需求="测试需求")
        发出的 = [r.messages[0].content for r in 器.发出的请求]
        assert any("内容包未接入" in m for m in 发出的), (
            "没内容包却没标降级——与第 13 步的手搓 Message 无法区分"
        )
        assert not any(m.startswith("你是") for m in 发出的)

    def test_预算闸门在编排里生效(self) -> None:
        """装配器有预算闸门，**编排必须真的传一个进去**。

        不传就等于闸门不存在——而预算是 D-189 的硬要求。
        """
        from zhengtong.orchestration.orchestrate import 编排器

        器 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 预算=50)
        器.跑到终(档位=TierId.PRO, 需求="测试需求")
        for r in 器.发出的请求:
            内容 = "".join(m.content for m in r.messages)
            assert len(内容) < 4000, "预算 50 却发了几千字——闸门没生效"


# ═══ 接缝四：编排 → call_id 幂等 ═══


class Test幂等:
    def test_同一语义键重试id不变(self) -> None:
        """D-074：id 由**语义唯一键**生成，不含轮次与序号。

        同一件事重试多次必须是同一个 id，否则幂等失效、重复记账。
        """
        a = make_call_id("publish", "r-1", "drafting", artifact="大纲")
        b = make_call_id("publish", "r-1", "drafting", artifact="大纲")
        assert a == b, "重试后 id 变了——幂等失效"

    def test_不同语义键id不同(self) -> None:
        """两件不同的事**绝不能**撞成同一个 id，否则一件会跳过另一件。"""
        assert make_call_id("publish", "r-1", "drafting", artifact="大纲") != (
            make_call_id("publish", "r-1", "drafting", artifact="初稿")
        )

    def test_编排用call_id记账(self) -> None:
        """**第 13 步交出时 `call_id` 出现 0 次**——幂等完全没接。

        判据查的是状态里有 `completed_call_ids` 记录。
        """

        器 = _全过编排器()
        got = 器.跑到终(档位=TierId.PRO, 需求="测试需求")
        assert got.is_ok, got.error
        assert got.unwrap().已完成调用, "没有任何 call_id 记账——续跑会重复执行"

    def test_已完成的不重复执行(self) -> None:
        """**行为判据**：把已完成的 call 注入进去，它不该再问模型。"""
        from zhengtong.adapters.fakes import FakeLlm
        from zhengtong.orchestration.orchestrate import 编排器

        计数 = {"n": 0}

        class _计数Llm(FakeLlm):
            def complete(self, request: LlmRequest) -> Result[LlmResponse]:
                计数["n"] += 1
                return super().complete(request)

        第一次 = 编排器(llm=_计数Llm(), storage=FakeStorage(), 门禁取证=_全过)
        第一次.跑到终(档位=TierId.PRO, 需求="测试需求")
        首轮 = 计数["n"]
        assert 首轮 > 0

        # 续跑：把首轮的**已完成清单**带进去（这才是续跑的真实形态——
        # 上次跑完留下了一串 call_id，本次要认出来）
        状态 = 第一次.终态
        已完成 = 第一次.跑到终(档位=TierId.PRO, 需求="测试需求").unwrap().已完成调用
        计数["n"] = 0
        第二次 = 编排器(
            llm=_计数Llm(),
            storage=FakeStorage(),
            初始状态=状态,
            初始已完成=已完成,
            门禁取证=_全过,
        )
        第二次.跑到终(档位=TierId.PRO, 需求="测试需求")
        assert 计数["n"] < 首轮, f"续跑又问了 {计数['n']} 次（首轮 {首轮}）——已完成的事重做了"


# ═══ 接缝五：编排 → 装配根 ═══


class Test走装配根:
    def test_走build_app装真件(self, ws: Workspace) -> None:
        """**唯一一条「编排进真实世界」的判据**。

        第 13 步的 `跑一次` 只接受构造参数，
        真实运行路径 `build_app_from_workspace` 里**根本没有编排**——
        它走的是自己那套 `run_once` 六阶段循环。
        两套并行的流程逻辑，将来必然分叉。

        本条钉住：**装配根里能拿到编排器，且它带的是真件**。
        """
        from zhengtong.app import build_app_from_workspace

        got = build_app_from_workspace(ws)
        assert got.is_ok, got.error
        app = got.unwrap()
        器 = app.编排器()
        assert 器 is not None, "装配根里拿不到编排器——真实路径走不通"
        assert not isinstance(器.llm, FakeLlm), "编排器接的是假 LLM"
        assert not isinstance(器.storage, FakeStorage), "编排器接的是假存储"

    def test_编排器从app拿到的模型名非空(self, ws: Workspace) -> None:
        """**第 13 步交出时 `LlmRequest(model="")`**——空模型名。

        真发出去会被端点拒绝，而判据里没有这一问，
        所以第 13 步 35 条全绿。
        """
        from zhengtong.app import build_app_from_workspace

        app = build_app_from_workspace(ws).unwrap()
        器 = app.编排器()
        器.跑到终(档位=TierId.PRO, 需求="测试需求")
        assert 器.发出的请求, "一次请求都没发"
        for r in 器.发出的请求:
            assert r.model, "请求里模型名是空的——真发会被端点拒"


# ═══ 接缝六：编排层不得碰适配层（D-165 的第十次教训）═══


class Test编排层不碰适配层:
    def test_编排层不import适配层(self) -> None:
        """**这条是整个修复的底线**。

        D-165 已发生九次，第 13 步是第十次。
        本次修接缝时若在 `orchestration/` 里写一句
        `from ..adapters.fakes import FakeContentPack`，
        就是**为了测试方便而把假实现钉进真实路径**——
        而假实现接在真路径上正是本项目反复吃亏的那件事。

        判据用 AST 查 import，**不用 grep**：
        grep 会漏掉 `importlib.import_module("…adapters…")` 这类写法。
        """
        import ast
        from pathlib import Path

        根 = Path(__file__).parent.parent / "src" / "zhengtong" / "orchestration"
        违例: list[str] = []
        for 文件 in 根.rglob("*.py"):
            树 = ast.parse(文件.read_text(encoding="utf-8"))
            for 节点 in ast.walk(树):
                if isinstance(节点, ast.ImportFrom):
                    模块 = 节点.module or ""
                    # `..kernel.redactor` 里的 Redactor 属「最小可用总线」，
                    # 不是构造业务真件，故只豁免这一个模块；
                    # `adapters` 一律不允许。
                    豁免 = 文件.name == "orchestrate.py" and 模块.endswith("kernel.redactor")
                    if "adapters" in 模块 or ("kernel.redactor" in 模块 and not 豁免):
                        违例.append(f"{文件.name}: from {模块}")
                elif isinstance(节点, ast.Import):
                    for 别名 in 节点.names:
                        if "adapters" in 别名.name:
                            违例.append(f"{文件.name}: import {别名.name}")
        assert not 违例, f"编排层碰了适配层：{违例}"

    def test_编排器端口从参数进来(self) -> None:
        """**构造器必须显式收端口**——
        缺省成某个具体实现，就等于在代码里选定了「跑的是真是假」。"""
        import inspect

        from zhengtong.orchestration.orchestrate import 编排器

        参数 = inspect.signature(编排器.__init__).parameters
        assert 参数["llm"].default is inspect.Parameter.empty, "llm 有缺省值"
        assert 参数["storage"].default is inspect.Parameter.empty, "storage 有缺省值"

    def test_全部端口齐备(self) -> None:
        """本轮接的五条缝用到的端口都该在构造器上——
        **漏一个就是又一处假实现能悄悄顶上来**。"""
        import inspect

        from zhengtong.orchestration.orchestrate import 编排器

        参数 = inspect.signature(编排器.__init__).parameters
        for 名 in ("llm", "storage", "bus", "content_pack", "validators", "模型名"):
            assert 名 in 参数, f"构造器缺 {名}"


# ═══ 接缝七：真内容包（D-210，第十一次 D-165）═══


class Test真内容包:
    """**这是本轮最实质的一处修复。**

    `adapters/` 里**从来没有过 `content_pack.py`**——
    第 12 步交付了内容包（21 个文件、192 项检查全绿），
    而 `build_app_from_workspace` 把 `content_pack=None` 直接转发，
    `build_app` 见到 None 装 `FakeContentPack`。

    于是真实运行路径上：角色提示词是「【桩】xxx 的角色提示词」，
    第 11 步装配器辛苦按需装载的规则条目**全是桩**。

    **内容包本是「唯一不硬编码公文内容」的落点（D-055），
    而它自己在真实路径上是假的。**
    """

    def test_内容包适配器存在且是真件(self) -> None:
        from zhengtong.adapters.content_pack import 本地内容包

        包 = 本地内容包(根=内容包根())
        assert getattr(包, "是否真实", False), "没标明自己是真件"

    def test_真适配器读出的不是桩(self) -> None:
        """桩文本里带「假实现产出」字样，真内容包不带。"""
        from zhengtong.adapters.content_pack import 本地内容包

        包 = 本地内容包(根=内容包根())
        got = 包.load_role_prompt(RoleId.STRUCTURE)
        assert got.is_ok, got.error
        正文 = got.unwrap()
        assert "假实现" not in 正文, "读出来的还是桩"
        assert "桩" not in 正文, "读出来的还是桩"
        assert len(正文) > 200, "角色提示词短得不正常"

    def test_真适配器读得出参考条目(self) -> None:
        from zhengtong.adapters.content_pack import 本地内容包

        got = 本地内容包(根=内容包根()).load_reference("error-memory")
        assert got.is_ok, got.error
        assert "红线" in got.unwrap() or "E1" in got.unwrap()

    def test_哈希校验通过(self) -> None:
        """**内容包是软数据**：Ruff 与类型检查器都看不见它（D-055）。

        改了提示词却没同步 manifest 的哈希，运行期察觉不到——
        而「我改了内容包吗」正是排障时最常问的问题。
        """
        from zhengtong.adapters.content_pack import 本地内容包

        got = 本地内容包(根=内容包根()).verify_manifest()
        assert got.is_ok, got.error.detail if got.error else ""

    def test_哈希不符必须报错不降级(self) -> None:
        """**不能因为一个文件被改就整个降级成假内容包**——
        那是「坏了就用假的」，用户拿到的会是无声无息的内容错乱。"""
        import shutil

        from zhengtong.adapters.content_pack import 本地内容包

        with _临时内容包() as 假根:
            shutil.copytree(内容包根(), 假根, dirs_exist_ok=True)
            目标 = next(假根.glob("prompts/*.md"))
            目标.write_text(目标.read_text(encoding="utf-8") + "\n篡改\n", encoding="utf-8")
            got = 本地内容包(根=假根).verify_manifest()
        assert got.is_error, "文件被改了却仍校验通过——内容包校验形同虚设"

    def test_真实路径装的是真内容包(self, ws: Workspace) -> None:
        """**本体判据**：`build_app_from_workspace` 装出来的必须是真件。"""
        from zhengtong.adapters.content_pack import 本地内容包
        from zhengtong.app import build_app_from_workspace

        包 = build_app_from_workspace(ws).unwrap().ports.content_pack
        assert isinstance(包, 本地内容包), f"装的是 {type(包).__name__}"

    def test_真实路径读出的不是桩(self, ws: Workspace) -> None:
        from zhengtong.app import build_app_from_workspace

        包 = build_app_from_workspace(ws).unwrap().ports.content_pack
        got = 包.load_role_prompt(RoleId.STRUCTURE)
        assert got.is_ok
        assert "假实现" not in got.unwrap(), "真实路径读到的还是桩"

    def test_内容包根定位得到且在程序旁(self) -> None:
        """内容包**与程序放在一起、视为只读**（架构 §9），
        不在工作区里——故不能走 `ws.dir(...)`。"""
        assert (内容包根() / "manifest.json").is_file(), "定位不到内容包"
        assert 内容包根().name == "content"


@contextlib.contextmanager
def _临时内容包() -> Iterator[Path]:
    """临时内容包根。**退出即删**——
    测试里篡改副本是必要的，但不该在工作区留下痕迹。"""
    import tempfile

    with tempfile.TemporaryDirectory() as 目录:
        yield Path(目录) / "content"
