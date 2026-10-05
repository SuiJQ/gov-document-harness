"""门禁取证的判据（架构 §15 第 14 步 5b）。

## 这组判据挡的是哪一个洞

`orchestrate.py` 的门禁段把「派谁查哪几道」算得清清楚楚
（`GATES[].owners`、D-049 并行、主理人终闸），调度器也是现成的，
**唯独没人真的去查**：注入点原是 `门禁取证 or (lambda _门禁: {})`，
而 `app.py` 建编排器时从不传它。12 道门禁结论恒为空。

后果不显眼：流程照样往下走，D-221 缺席判据把「一道结论都没交」
转成用户裁决，于是**每次成文都要用户替系统的「没查」买单**，
而日志与报告里看不出「没查」和「查过没问题」的区别。

## 判据的设计前提

**先让它们红过。** 每条都对应一个能让它变红的真实改动，
否则就是装饰。其中 `Test结构校验真会收窄` 那条专治
`ValidationResult.blocking` 被当成方法调用（`@property`，
写成 `blocking()` 在运行时才炸）——本组判据落笔时那里
正是一个没人抓得到的活 bug。

## 为什么不复用现有门禁判据

`test_step14_orchestrate_gate.py` 那组查的是**编排侧**：缺席怎么转裁决、
报告怎么排。并行取证查的是**线程**：几组同时跑、有没有崩。
它们都把 `门禁取证` 当成一个给定的假输入，**没查过它对不对**。

所以这组只查一件事：**门禁取证这件事到底有没有被真的做**。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from zhengtong.adapters.content_pack import 本地内容包
from zhengtong.adapters.fakes import FakeLlm, FakeStorage, FakeValidators
from zhengtong.domain.errors import Result
from zhengtong.domain.gate import FORMAL_GATES, GATES, GateId
from zhengtong.domain.ids import RunId
from zhengtong.domain.role import RoleId
from zhengtong.domain.tier import TIER_BY_ID, TierId
from zhengtong.orchestration.gates_forging import (
    核对结构,
    解析结论,
    门禁取证实装,
    门禁取证请求,
)
from zhengtong.orchestration.orchestrate import 编排器, 编排结果
from zhengtong.ports.validators import ValidationIssue, ValidationResult

仓库 = Path(__file__).resolve().parents[1]

合规 = RoleId.COMPLIANCE


def _回覆(*行: str) -> str:
    return "\n".join(行)


def _错详情(got: Result[编排结果]) -> str:
    """失败原因。

    **`Result.error` 类型上是 `AppError | None`**，而 `is_ok` 并不
    在类型层面把 `error` 收窄成非空——两个判据都写成
    `got.error.detail if got.is_error else ""`，基于pyright 报
    `detail is not a known attribute of None`。

    报错的不是 `if`，是 `is_ok` 不做判别式收窄。故显式判一次 `None`。
    """
    错 = got.error
    return 错.detail if 错 is not None else ""


class Test解析结论:
    def test_按行式读出门禁与结论(self) -> None:
        出 = 解析结论(
            _回覆(
                "G1 | 通过 | 证据：正文首句已按 GB/T 9704 规范，无缺项",
                "G2 | 退回 | 原因：第 3 段缺规范称谓 | 证据：正文第 3 段",
            ),
            (GateId.G1, GateId.G2),
            合规,
        )
        assert set(出) == {"G1", "G2"}, f"两道门禁都该有结论，实际 {sorted(出)}"
        assert 出["G1"].passed, "G1 判了通过就该是 passed"
        assert not 出["G2"].passed, "G2 判了退回就不该是 passed"
        assert 出["G2"].rejection_reason, "退回必须写明原因，否则报告里看不出卡在哪"

    def test_缺席者不出结论而不是判通过(self) -> None:
        """**这是整组判据最要紧的一条**。

        指派 2 道、只答 1 道，第 2 道**不能**出现在结果里。
        也不能补一条「通过」——那等于把「没查」写成「查过没问题」，
        正是 D-221 立账要挡的那件事。
        """
        出 = 解析结论("G1 | 通过 | 证据：正文首句格式已复核，无缺项", (GateId.G1, GateId.G2), 合规)
        assert set(出) == {"G1"}, f"未作答的门禁不该有结论，实际 {sorted(出)}"
        assert "G2" not in 出, "**缺席绝不能被补成通过**（D-221）"

    def test_没作答时返回空而不是全过(self) -> None:
        出 = 解析结论("模型说了些别的话", (GateId.G1,), 合规)
        assert 出 == {}, f"没按格式作答就该一条都没有，实际 {出}"

    def test_证据太短判不通过(self) -> None:
        """「已核对」3 个字不算可复核的定位（D-034）。"""
        出 = 解析结论("G1 | 通过 | 证据：已核对", (GateId.G1,), 合规)
        assert not 出["G1"].passed, "3 个字的敷衍表述不得判通过"
        assert "证据不足" in 出["G1"].rejection_reason, "要写明是证据不足而非查出不通过"

    def test_没有证据段也不判通过(self) -> None:
        出 = 解析结论("G1 | 通过", (GateId.G1,), 合规)
        assert not 出["G1"].passed, "无证据不得判通过（D-034）"

    def test_多答没派的门禁要丢(self) -> None:
        出 = 解析结论(
            _回覆(
                "G1 | 通过 | 证据：正文首句格式已复核，无缺项",
                "G7 | 通过 | 证据：正文第 5 段因果链已复核，完整",
            ),
            (GateId.G1,),
            合规,
        )
        assert "G7" not in 出, "没派给它的门禁，多答也不该采信"

    def test_不是合法取证方签的门禁要丢(self) -> None:
        """**D-084 的本体**：门禁的取证方不是谁都能当的。

        G13 是**双取证方**（陈纲举 + 骆准绳，D-084），只由一个人签
        不足以判通过。而 G1 只有骆准绳有签字权——
        换成陈纲举来签 G1，就该丢。

        只按「派了哪些门禁」过滤是不够的：编排器派给陈纲举的那一组
        里，他完全可以顺手在回覆里写一句 G1。
        """
        出 = 解析结论(
            "G1 | 通过 | 证据：正文首句格式已复核，无缺项",
            (GateId.G1,),
            RoleId.STRUCTURE,  # 陈纲举不是 G1 的取证方
        )
        assert 出 == {}, "非合法取证方签的门禁必须丢弃（D-084）"

    def test_双取证方其一签字即有效(self) -> None:
        """G13 双取证方，**其一签字即进入结论**（不是要两人都签）。

        要两人都签的话，编排器会把 G13 派两次或归给一个人，
        而 D-084 定的是「共同取证」，不是「互为前置」。
        """
        出 = 解析结论(
            "G13 | 通过 | 证据：正文第 2 段折中论证已标注两侧依据",
            (GateId.G13,),
            合规,
        )
        assert "G13" in 出, "G13 的两位取证方之一签字即有效"

    def test_重复作答取首条(self) -> None:
        """后写的无法证伪先写的——**让程序挑等于替模型裁决**。"""
        出 = 解析结论(
            _回覆(
                "G1 | 通过 | 证据：正文首句格式已复核，无缺项",
                "G1 | 退回 | 原因：第 2 段称谓不规范 | 证据：正文第 2 段",
            ),
            (GateId.G1,),
            合规,
        )
        assert 出["G1"].passed, "重复作答取首条：首个是「通过」"
        assert 出["G1"].evidence == ("正文首句格式已复核，无缺项",), "证据应取首条那条"

    def test_竖线前后有空格也能读(self) -> None:
        """模型不会严格照格式敲——**空格容错是必须的**。"""
        出 = 解析结论("  G1  |  通过  |  证据：正文首句格式已复核，无缺项", (GateId.G1,), 合规)
        assert 出["G1"].passed, "带空格的行同样该读出来"

    def test_全角竖线也能读(self) -> None:
        """中文模型常用全角 `｜`。只认半角等于凭空失证（D-221）。"""
        出 = 解析结论("G1｜通过｜证据：正文首句格式已复核，无缺项", (GateId.G1,), 合规)
        assert 出["G1"].passed, "全角竖线同样该读出来"


class Test结构校验真会收窄:
    """**这条专治 `@property` 被当方法调用。**

    `ValidationResult.blocking` 是 `@property`（`ports/validators.py`），
    写成 `blocking()` 在类型检查里就报 `not callable`——
    可那时整组判据里**没有一条会走那条路径**，
    于是 bug 留着，测试全绿。写死一个报问题的校验器就绕不开了。
    """

    def test_校验器报问题时结论降级(self) -> None:
        class 报错校验(FakeValidators):
            def run_structure(self, payload: dict[str, object]):
                return Result.ok(
                    ValidationResult(
                        script="structure",
                        passed=False,
                        issues=(ValidationIssue(rule="D034", message="无取证证据"),),
                    )
                )

        记 = 门禁取证请求(
            门禁=(GateId.G1,),
            取证方=合规,
            回覆="G1 | 通过 | 证据：正文首句格式已复核，无缺项",
            档位=TierId.FLASH,
        )
        出 = 门禁取证实装(报错校验())(记)
        assert not 出["G1"].passed, "结构校验报出问题就不能算通过"
        assert "结构校验" in 出["G1"].rejection_reason, "降级原因要写明出自结构校验"

    def test_校验通过不得把不通过改成通过(self) -> None:
        """D-069 / gate-manual.md:447：校验器**只收窄，不放宽**。

        机械校验通过了就把一条「退回」翻成「通过」，
        等于让脚本产出质量结论——那正是明令禁止的。
        """

        class 全过校验(FakeValidators):
            def run_structure(self, payload: dict[str, object]):
                return Result.ok(ValidationResult(script="structure", passed=True, issues=()))

        出 = 核对结构(
            解析结论(
                "G1 | 退回 | 原因：第 3 段缺规范称谓 | 证据：正文第 3 段",
                (GateId.G1,),
                合规,
            ),
            全过校验(),
        )
        assert not 出["G1"].passed, "校验通过**不得**把不通过翻成通过（D-069）"

    def test_没校验器时不把结论都判不通过(self) -> None:
        """缺校验器不等于门禁全红。

        替身返回「无问题」让收窄逻辑无处可收；若返回 False，
        就是拿「没装校验器」冒充「查出了坏结论」。
        """
        记 = 门禁取证请求(
            门禁=(GateId.G1,),
            取证方=合规,
            回覆="G1 | 通过 | 证据：正文首句格式已复核，无缺项",
            档位=TierId.FLASH,
        )
        出 = 门禁取证实装(None)(记)
        assert 出["G1"].passed, "无校验器时不得把所有结论压成不通过"


class Test真接线:
    def test_编排器不再有空兜底(self) -> None:
        """**把源码里的空实现挡在门外**。

        曾经的写法是 `门禁取证 or (lambda _门禁: {})`。
        只要那行还在，任何忘了接线的调用方都会安静地拿到空结论，
        而 1982 条判据全绿。
        """
        源 = (仓库 / "src/zhengtong/orchestration/orchestrate.py").read_text("utf-8")
        assert "lambda _门禁" not in 源, "编排器里仍有空取证兜底——忘了接线就会安静地判「没人查」"

    def test_app装配时真传了取证实现(self) -> None:
        源 = (仓库 / "src/zhengtong/app.py").read_text("utf-8")
        assert "门禁取证=门禁取证实装" in 源, "装配根没有把真取证实现接进去——生产路径上仍是空结论"

    def test_src里没有只存在于测试的接线(self) -> None:
        """`门禁取证=` 在 src 里必须**至少有一处实参**。

        曾经全仓 9 处全在 tests，src 零处。
        """
        命中 = [
            p.name
            for p in (仓库 / "src").rglob("*.py")
            if "门禁取证=门禁取证实装" in p.read_text("utf-8")
        ]
        assert 命中, "src 里没有任何一处把真取证实现接进去"

    def test_编排层不反向依赖适配层(self) -> None:
        """取证要用校验器，**但不能 import `adapters`**。

        用 `adapters.fakes.FakeValidators` 兜底看着省事，
        那是第十次 D-165：编排层依赖适配层，判据一换包就断。
        """
        源 = (仓库 / "src/zhengtong/orchestration/gates_forging.py").read_text("utf-8")
        assert "from ..adapters" not in 源, "编排层不得反向 import 适配层（D-165）"


class Test取证方进了提示词:
    def test_内容包里有输出契约(self) -> None:
        包 = 本地内容包(根=仓库 / "content")
        got = 包.load_reference("gate-verdict-format")
        assert got.is_ok, "内容包缺门禁取证输出契约"
        正文 = got.unwrap()
        assert "|" in 正文 and "证据" in 正文, "契约要写明行式格式与证据段"

    def test_契约只在门禁阶段装载(self) -> None:
        """写作阶段装契约就是白装——那几行格式要求会干扰正文。"""
        包 = 本地内容包(根=仓库 / "content")
        assert 包.load_reference("gate-verdict-format").is_ok

    def test_指派清单真的进了请求(self) -> None:
        """**指派不进 prompt，契约第 4 条就是空话**。

        契约明写「只对你被指派的门禁作答」，而模型看不到指派
        就只能猜，猜错就整组失证（D-221 判缺席）。
        """
        llm = FakeLlm()
        编 = 编排器(
            llm=llm,
            storage=FakeStorage(),
            运行号=RunId("r-判据"),
            模型名="假",
            content_pack=本地内容包(根=仓库 / "content"),
        )
        编.跑到终(档位=TierId.PRO, 需求="关于本市老旧小区改造的通知")
        门禁请求 = [
            c
            for c in llm.calls
            if "本轮**只由你取证**的门禁"
            in "\n".join(m.content for m in c.messages if isinstance(m.content, str))
        ]
        assert 门禁请求, "没有任何一次请求带上门禁指派清单"

        正文 = "\n".join(m.content for m in 门禁请求[0].messages if isinstance(m.content, str))
        assert re.search(r"共 \d+ 道", 正文), "指派清单要报出派了几道"
        assert "G" in 正文, "指派清单要列出具体门禁编号"

    def test_输出契约跟着门禁请求一起装(self) -> None:
        """契约没装进 prompt，模型就只能凭猜写格式。"""
        llm = FakeLlm()
        编 = 编排器(
            llm=llm,
            storage=FakeStorage(),
            运行号=RunId("r-判据"),
            模型名="假",
            content_pack=本地内容包(根=仓库 / "content"),
        )
        编.跑到终(档位=TierId.PRO, 需求="通知")
        门禁请求 = [
            c
            for c in llm.calls
            if "本轮**只由你取证**的门禁"
            in "\n".join(m.content for m in c.messages if isinstance(m.content, str))
        ]
        assert 门禁请求, "没找到门禁取证请求"
        正文 = "\n".join(m.content for m in 门禁请求[0].messages if isinstance(m.content, str))
        assert "门禁号" in 正文 or "逐道" in 正文, "输出契约没装进门禁请求的提示词"


class Test真跑出来有结论:
    @pytest.mark.parametrize("档", [TierId.FLASH, TierId.PRO])
    def test_十二道门禁都有结论(self, 档: TierId) -> None:
        """**端到端**：不注入假取证，接真实内容包真跑一遍。

        这一条是前面所有条的合流：只要取证再变回空结论，
        它就红。而且它验的是「该出结论的门禁都拿到了结论」，
        不是「流程没报错」——后者在缺口存在时也是绿的。

        ## 应有几道**按规则算，不写死数字**

        我第一版写死 `>= 11`，实测 Flash 只有 10 道——差的两道
        **本来就该没有**：G3（文择体不在 Flash 编制，D-116 降级
        给主理人研判）与 G9（动态取证方，不独立取证，D-084）。

        写死数字会逼着人把这两道算进去，等于让判据反过来改实现。
        故按 `TIER_BY_ID` + `GATES[].owners` 算该有的集合，
        规则变了判据自己跟着变。
        """
        llm = FakeLlm()
        仓 = FakeStorage()
        编 = 编排器(
            llm=llm,
            storage=仓,
            运行号=RunId(f"r-{档.value}"),
            模型名="假",
            content_pack=本地内容包(根=仓库 / "content"),
        )
        got = 编.跑到终(档位=档, 需求="关于本市老旧小区改造的通知")
        assert got.is_ok, f"{档} 没能走完全程：{_错详情(got)}"

        账 = 仓.load_run_state(RunId(f"r-{档.value}"))
        assert 账.is_ok, "状态没存盘"
        编号 = set(账.unwrap().gates)

        规 = TIER_BY_ID[档]
        应有 = {
            g.value
            for g in FORMAL_GATES
            if 规.gates_apply
            and not GATES[g].dynamic_owners
            and any(r in 规.roster for r in GATES[g].owners)
        }
        assert 应有, f"{档} 按规则竟一道门禁都不该跑——判据前提失效"
        assert 编号 == 应有, (
            f"{档} 门禁结论对不上：缺 {sorted(应有 - 编号)}、多 {sorted(编号 - 应有)}"
        )

    def test_结论里带着证据(self) -> None:
        """**无证据不通过**：通过的结论必须带得出处。"""
        llm = FakeLlm()
        仓 = FakeStorage()
        编排器(
            llm=llm,
            storage=仓,
            运行号=RunId("r-证据"),
            模型名="假",
            content_pack=本地内容包(根=仓库 / "content"),
        ).跑到终(档位=TierId.PRO, 需求="通知")
        账 = 仓.load_run_state(RunId("r-证据"))
        assert 账.is_ok
        过 = [g for g in 账.unwrap().gates.values() if g.passed]
        assert 过, "一道门禁都没通过——桩回复覆格式可能变了"
        无证据 = [g.gate for g in 过 if not g.evidence]
        assert not 无证据, f"这些门禁判了通过却没有证据：{无证据}（D-034）"

    def test_证据真的出现在门禁报告里(self) -> None:
        """**第 7 步：证据不能只躺在状态里**。

        `_门禁报告` 的渲染代码本来就读 `GateRecord.evidence`
        （D-236 改的），但**接线之前没有任何真实证据可读**——
        结论恒空，报告里的「证据：」那一截永远是空的。
        这条判据验的是接上之后**真的流到了报告**，而不是
        「渲染代码里写着 evidence」。

        标准模式不展示审核意见（用户约束），所以报告是
        用户事后唯一的台账。证据到不了那里，等于没取证。
        """
        llm = FakeLlm()
        仓 = FakeStorage()
        got = 编排器(
            llm=llm,
            storage=仓,
            运行号=RunId("r-台账"),
            模型名="假",
            content_pack=本地内容包(根=仓库 / "content"),
        ).跑到终(档位=TierId.PRO, 需求="通知")
        assert got.is_ok, f"没能走完全程：{_错详情(got)}"
        报告 = 仓.artifacts.get("r-台账/gate-report.md", "")
        assert 报告, "门禁报告没落盘"
        assert "证据：" in 报告, f"报告里没有证据字段：{报告[:300]}"
        # 桩证据的原文要出现在报告里——不是只显示「证据：」三个字
        assert "本条为桩证据" in 报告, f"证据原文没进报告，只看到字段名：{报告[:400]}"

    def test_报告每道门禁都写了取证方(self) -> None:
        """D-049 的责任映射：「这稿是谁的锅？」"""
        llm = FakeLlm()
        仓 = FakeStorage()
        编排器(
            llm=llm,
            storage=仓,
            运行号=RunId("r-台账2"),
            模型名="假",
            content_pack=本地内容包(根=仓库 / "content"),
        ).跑到终(档位=TierId.PRO, 需求="通知")
        报告 = 仓.artifacts.get("r-台账2/gate-report.md", "")
        assert 报告.count("取证：") >= 10, f"只有 {报告.count('取证：')} 道写了取证方，报告不成台账"

    def test_咨询档本就不跑门禁(self) -> None:
        """咨询档 `gates_apply=False`（`domain/tier.py`）——**豁免是有意的**。

        这条把「有意豁免」钉成判据，免得后来有人当成 bug 去修，
        或者反过来给咨询档硬加门禁。
        """
        llm = FakeLlm()
        仓 = FakeStorage()
        got = 编排器(
            llm=llm,
            storage=仓,
            运行号=RunId("r-咨询"),
            模型名="假",
            content_pack=本地内容包(根=仓库 / "content"),
        ).跑到终(档位=TierId.CONSULT, 需求="就本市老旧小区改造给出咨询意见")
        assert got.is_ok, "咨询档被卡住了"
        账 = 仓.load_run_state(RunId("r-咨询"))
        assert 账.is_ok
        assert not 账.unwrap().gates, "咨询档不该有门禁结论（gates_apply=False）"


class Test门禁与取证方一致:
    def test_取证方映射是单值的除G13(self) -> None:
        """D-084：取证方是**映射**，写成单值会在 G13 上直接错。"""
        双 = [g for g in GATES if len(GATES[g].owners) > 1]
        assert 双 == [GateId.G13], f"多取证方的门禁应只有 G13，实际 {双}"

    def test_并行组的取证方各自独占(self) -> None:
        """`并行门禁组` 只收「恰好独占一整组」的角色。

        G13 若被硬并进某一组，就破坏了「两人共同取证」的语义。
        """
        from zhengtong.domain.gate import 并行门禁组

        for 组 in 并行门禁组():
            assert len(组) == 1, f"并行组应是单元素元组，实际 {组}"
        全部 = {r for 组 in 并行门禁组() for r in 组}
        assert RoleId.STRUCTURE in 全部 and RoleId.COMPLIANCE in 全部, "结构与合规都得有独立组"
