"""第 13 步判据：编排层把专家团契约跑成状态机。

## 这一步为什么最容易写出一个「全绿但没真跑」的东西

前 12 步每一层都单独测过，**但没有任何代码把它们串起来**。
于是最省事、也最像回事的写法是：照着 `PhaseId` 顺序空转，
跑完最后一阶段就算「跑通」。测试会全绿，产物是空的。

D-165 已经同型发生七次（D-196 是第七次），本步必须防它第八次。

## 判据怎么写才算数

**「跑通了」不能靠断言 `is_ok`**——那是「没崩」，不是「跑对」。
每条判据都指向一个具体的偷懒实现，见下表：

| 偷懒实现 | 抓它的判据 |
|---|---|
| 空转到底，产物全空 | `test_每段都有产物落地` |
| 咨询档也走门禁 | `test_咨询档不触发门禁` |
| 档位人数写死在代码里 | `test_档位人数是数据不是代码` |
| 成员状态全是初始值 | `test_成员状态被真实流转过` |
| 「跑完了」但阶段被跳过 | `test_阶段序列逐段可查` |

**「产物落地」是本步的核心判据**：它同时杀掉了
「跳阶段」「空转」「产物没写进 storage」三种偷懒，一次覆盖三处。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.adapters.paths import Workspace, ensure_dirs
from zhengtong.domain.errors import Result
from zhengtong.domain.phase import PhaseId
from zhengtong.domain.role import RoleId, RoleState
from zhengtong.domain.tier import TIER_BY_ID, TierId
from zhengtong.orchestration import 咨询序列, 编排请求, 跑一次, 阶段序列
from zhengtong.ports.llm import FinishReason, TokenUsage


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return ensure_dirs(tmp_path / "ws").unwrap()


def _跑(档: TierId, *, llm: FakeLlm | None = None):
    return 跑一次(
        编排请求(
            档位=档,
            需求="就甲单位2026年第三季度安全生产工作起草一份通知",
        ),
        llm=llm or FakeLlm(),
        storage=FakeStorage(),
    )


# ═══ 判据一：三档都能跑通 ═══


class Test三档跑通:
    @pytest.mark.parametrize("档", [TierId.CONSULT, TierId.FLASH, TierId.PRO])
    def test_跑完不报错(self, 档: TierId) -> None:
        got = _跑(档)
        assert got.is_ok, got.error

    @pytest.mark.parametrize("档", [TierId.CONSULT, TierId.FLASH, TierId.PRO])
    def test_确实走到了终点(self, 档: TierId) -> None:
        """**不能只验 `is_ok`**——空转到最后一阶段也不报错。

        必须查状态真的到了 `DONE`。
        """
        结果 = _跑(档).unwrap()
        assert 结果.终态阶段 is PhaseId.DONE, f"停在 {结果.终态阶段}"
        assert 结果.完整阶段序列[-1] is PhaseId.DONE


# ═══ 判据二：产物真落地（核心）═══


class Test产物落地:
    @pytest.mark.parametrize("档", [TierId.CONSULT, TierId.FLASH, TierId.PRO])
    def test_每段都有产物落地(self, 档: TierId) -> None:
        """**本步最重要的一条**。

        它一次杀死三种偷懒：跳阶段、空转、产物没写进 storage。
        「跑通了」而不落产物，等于什么都没跑。
        """
        结果 = _跑(档).unwrap()
        assert 结果.产物, "全程零产物——这不叫跑通"

    def test_产物内容非空(self) -> None:
        """产物有名字但内容是空串，同样是空转。"""
        结果 = _跑(TierId.PRO).unwrap()
        assert all(p.内容.strip() for p in 结果.产物), "有空产物"

    def test_最终有一份交付物(self) -> None:
        """成文档必须产出终稿——不是「过程产物一堆」就完事。"""
        结果 = _跑(TierId.PRO).unwrap()
        assert 结果.交付物, "没有可交付的终稿"

    def test_咨询档也有交付物(self) -> None:
        """咨询档产出的是「专家答复」，不是成文（D-035）——
        但**它也得有交付物**，否则用户拿到的是空气。"""
        结果 = _跑(TierId.CONSULT).unwrap()
        assert 结果.交付物

    def test_阶段序列逐段可查(self) -> None:
        """每段都能查到它产出了什么。

        「跑完了」但中间某段没产物，是最难查的一类缺陷——
        流程看起来正常，产出就是少一块。
        """
        结果 = _跑(TierId.PRO).unwrap()
        for 阶段 in 结果.完整阶段序列:
            if 阶段 is PhaseId.DONE:
                continue
            assert 结果.该段产物(阶段), f"{阶段} 段没落任何产物"


# ═══ 判据三：档位是数据（D-067）═══


class Test档位是数据:
    def test_咨询档不触发门禁(self) -> None:
        """咨询档「不触发方向确认、不做政策检索、不走门禁」（§8.3）。"""
        结果 = _跑(TierId.CONSULT).unwrap()
        assert not 结果.走过的门禁, "咨询档竟然过了门禁"

    def test_咨询档只有主理人(self) -> None:
        结果 = _跑(TierId.CONSULT).unwrap()
        assert set(结果.动过的角色) == {RoleId.TEAM_LEAD}

    def test_Flash档不含文择体(self) -> None:
        """Flash 三人（范正章、陈纲举、辞达意）——架构 §8.3 说的是
        三人，但 `TierSpec.roster` 实际含骆准绳（D-049 配平需要）。
        **判据查的是「文择体不在编制内」，不是死记人数**——
        人数是数据，改数据不该让这条判据失效。
        """
        结果 = _跑(TierId.FLASH).unwrap()
        assert RoleId.DOC_ADVISOR not in 结果.动过的角色
        assert RoleId.DOC_ADVISOR not in TIER_BY_ID[TierId.FLASH].roster

    def test_Pro档全员参与(self) -> None:
        结果 = _跑(TierId.PRO).unwrap()
        assert set(结果.动过的角色) == set(TIER_BY_ID[TierId.PRO].roster)

    def test_Flash档G3带降级标注(self) -> None:
        """D-116 剩半笔的**代码侧**：Flash 档 G3 降级须带
        「文种由主理人研判」。本条查编排层有没有把标注带上——
        第 11 步算出了这个事实，第 13 步要把它**用起来**。
        """
        结果 = _跑(TierId.FLASH).unwrap()
        assert any("文种由主理人研判" in 标注 for 标注 in 结果.成果标注), (
            "Flash 档 G3 降级却没带标注（D-116）"
        )

    def test_Pro档无该标注(self) -> None:
        结果 = _跑(TierId.PRO).unwrap()
        assert not any("文种由主理人研判" in 标注 for 标注 in 结果.成果标注)

    def test_档位人数改了编排不认得(self) -> None:
        """**「档位是数据」的判据是：改数据能改行为**。

        若人数写死在编排代码里，这条无从验证——
        测一个 `len(roster) == 4` 既测不出是不是数据，也挡不住改动。
        """
        import dataclasses

        原档 = TIER_BY_ID[TierId.FLASH]
        少一人 = dataclasses.replace(
            原档, roster=tuple(r for r in 原档.roster if r is not RoleId.LANGUAGE)
        )
        import zhengtong.domain.tier as tier_mod

        原表 = dict(tier_mod.TIER_BY_ID)
        try:
            tier_mod.TIER_BY_ID[TierId.FLASH] = 少一人  # type: ignore[index]
            结果 = _跑(TierId.FLASH).unwrap()
            assert RoleId.LANGUAGE not in 结果.动过的角色, "改了编制却没影响编排"
        finally:
            tier_mod.TIER_BY_ID.update(原表)


# ═══ 判据四：成员状态真流转过 ═══


class Test成员状态机:
    def test_成员状态被真实流转过(self) -> None:
        """全 IDLE 说明编排压根没管成员状态——
        而 D-042 六态与「挂起时不发请求」是硬约束。"""
        结果 = _跑(TierId.PRO).unwrap()
        出现过 = set(结果.出现过的成员状态)
        assert RoleState.RUNNING in 出现过, "从没派发过成员"
        assert RoleState.DONE in 出现过, "成员从没走到终态"

    def test_终态不再接受流转(self) -> None:
        """D-042：「完成/失败」是**唯一不可逆的边**。"""
        from zhengtong.orchestration.machine import 允许流转

        assert 允许流转(RoleState.DONE, RoleState.RUNNING) is False
        assert 允许流转(RoleState.DONE, RoleState.SUSPENDED) is False
        assert 允许流转(RoleState.RUNNING, RoleState.SUSPENDED) is True

    def test_挂起不发起模型请求(self) -> None:
        """D-042 铁律一：挂起状态下**绝不发起任何模型请求**。

        不空转、不轮询、不烧算力。
        """
        from zhengtong.orchestration.machine import 该发请求

        assert 该发请求(RoleState.RUNNING) is True
        for 态 in (
            RoleState.SUSPENDED,
            RoleState.AWAITING_USER,
            RoleState.AWAITING_CONDITION,
            RoleState.IDLE,
        ):
            assert 该发请求(态) is False, f"{态} 状态下竟允许发请求"

    def test_不可逆边只有一条(self) -> None:
        """**枚举全部 36 组转移逐组查**，不靠抽查。

        ## 第一版这里把 `IDLE → RUNNING` 也算成不可逆，判据写错了

        架构 §8.5 原文：「四态之间可双向迁移
        （`运行中 ⇄ 挂起 / 等待用户 / 等待条件`），
        因为一次任务里一个成员可能被反复唤醒多次；
        但**唯一不可逆的边是进入「完成/失败」**」。

        关键在于**「四态」指的是运行中与那三个等待态**——
        `IDLE → RUNNING` 是**派发边**，成员一旦被派发就回不到空闲，
        它本来就不在「四态可双向」这句话的范围内。

        教训：把架构原话当判据时，**得先确认那句话的主语是哪些状态**。
        我把「四态」当成了「全部六态」，于是给实现套了个架构没要求的约束。
        """
        from zhengtong.orchestration.machine import 允许流转, 可逆

        不可逆 = {(a, b) for a in RoleState for b in RoleState if 允许流转(a, b) and not 可逆(a, b)}
        # 派发边（IDLE → RUNNING）与终态边（RUNNING → DONE）都不可逆
        assert 不可逆 == {
            (RoleState.IDLE, RoleState.RUNNING),
            (RoleState.RUNNING, RoleState.DONE),
        }, f"不可逆边与架构不符：{不可逆}"

    def test_四态之间可双向迁移(self) -> None:
        """架构原话那一半：**运行中 ⇄ 三个等待态，全部双向**。

        「一次任务里一个成员可能被反复唤醒多次」——
        单向的话唤醒第二次就非法了。
        """
        from zhengtong.orchestration.machine import 可逆

        等待三态 = (
            RoleState.SUSPENDED,
            RoleState.AWAITING_USER,
            RoleState.AWAITING_CONDITION,
        )
        for 态 in 等待三态:
            assert 可逆(RoleState.RUNNING, 态), f"运行中 → {态} 不可逆"
            assert 可逆(态, RoleState.RUNNING), f"{态} → 运行中 不可逆"

    def test_终态不可复活(self) -> None:
        """「六态机不含完成态复活」——终态出去哪儿都回不来。"""
        from zhengtong.orchestration.machine import 允许流转

        for 到 in RoleState:
            assert 允许流转(RoleState.DONE, 到) is False, f"终态竟能到 {到}"


# ═══ 判据五：编制内却不当值（D-049 配平铁律的动态侧）═══


class Test编制内必须当值:
    """本步查出的两个真缺陷都是同一类：**角色在编制内，却从没被派发**。

    - 文择体（Pro）：G3 无人取证
    - 骆准绳（Pro/Flash）：G1/G2/G7/G10/G11 五道门禁无人取证

    这类缺陷能静默潜伏：`test_Pro档全员参与` 一条就同时暴露了它们，
    但**若那条判据只查「动了几个」而不查「动了谁」**，就会漏掉。

    故这里逐个点名查，且**查的是门禁 owners 而不是人数**。
    """

    def test_Pro档每个取证方都真被派发(self) -> None:
        结果 = _跑(TierId.PRO).unwrap()
        动过 = set(结果.动过的角色)
        编制 = set(TIER_BY_ID[TierId.PRO].roster)
        缺 = 编制 - 动过
        assert not 缺, f"编制内却从未被派发：{缺}"

    def test_编制内的取证方都当值(self) -> None:
        """**逐个门禁 owners 查**，不查人数。

        人数对了但派错人、或漏派一个人，
        「人数 == 4」这类断言都分辨不出来。
        """
        from zhengtong.domain.gate import GATES

        编制 = set(TIER_BY_ID[TierId.PRO].roster)
        动过 = set(_跑(TierId.PRO).unwrap().动过的角色)
        for 门禁, 规 in GATES.items():
            if not 规.owners:
                continue  # G9 动态取证
            该当值 = {r for r in 规.owners if r in 编制}
            assert 该当值 <= 动过, f"{门禁} 的取证方 {该当值 - 动过} 在编制内却没被派发"

    def test_咨询档不因门禁owners被拉来人(self) -> None:
        """反向：编制外的角色**绝不能被派发**。

        GATES 里有文择体，但咨询档编制只有主理人——
        若按「所有 owners 都派一遍」实现，咨询档会多出一个人，
        而用户明确要求咨询只由范正章一人参与。
        """
        结果 = _跑(TierId.CONSULT).unwrap()
        assert set(结果.动过的角色) <= set(TIER_BY_ID[TierId.CONSULT].roster)

    def test_Flash档合规角色仍当值(self) -> None:
        """Flash 编制含骆准绳（架构原话是「三人」，
        但 `TierSpec.roster` 实际含四人——D-049 配平需要合规取证）。

        **判据查的是「在编制内就该当值」，不是死记人数**：
        人数是数据，改数据不该让这条失效。
        """
        assert RoleId.COMPLIANCE in TIER_BY_ID[TierId.FLASH].roster
        assert RoleId.COMPLIANCE in _跑(TierId.FLASH).unwrap().动过的角色

    def test_阶段分叉按is_consult而非人数(self) -> None:
        """**补一条证伪漏掉的判据。**

        第一轮证伪把 `TIER_BY_ID[档].is_consult` 换成
        `len(roster) == 1`，**零红**——
        当时那句「查 `is_consult` 不查人数」只写在文档里，没钉成测试。

        ## 为什么这条不能靠文档守住

        现在的 `roster` 恰好咨询 1 人、成文 3–4 人，
        两个判据**结果完全一样**，所以行为上等价。
        但一旦哪天咨询档加入第二人（例如「咨询可带随员」），
        人数判据就会把咨询档送进成文线——
        **触发条件是配置变更，触发时机是半年后，那时没人会想到这条。**

        故必须钉死：**改人数不得改变阶段分叉**。
        """
        import dataclasses

        import zhengtong.domain.tier as tier_mod

        原档 = TIER_BY_ID[TierId.CONSULT]
        try:
            tier_mod.TIER_BY_ID[TierId.CONSULT] = dataclasses.replace(  # type: ignore[index]
                原档, roster=(*原档.roster, RoleId.LANGUAGE)
            )
            assert 阶段序列(TierId.CONSULT) == 咨询序列, (
                "咨询档人数一变就走了成文线——说明分叉判的是人数不是 is_consult"
            )
        finally:
            tier_mod.TIER_BY_ID.update({TierId.CONSULT: 原档})


# ═══ 判据六：编排层真的问过模型 ═══


class Test真调模型:
    """**D-165 第八次**——由 Ruff 的 `ARG001` 抓出来的。

    现象：`跑一次(..., llm=llm, ...)` 收了 `llm` 参数却从头到尾没用它。
    编排层一次都没真问过模型，却能把三档「跑通」，
    产物内容全是 `_产内容` 现编的字符串，**测试全绿**。

    ## 为什么静态检查比测试先抓到

    判据查的是「产物里有没有模型的回复」，
    而第一版的产物**格式上就自洽**——每段都有内容、非空、能落盘。
    「编造的内容」和「模型给的内容」在没有标记时**长得一模一样**。

    Ruff 看到的是「参数收了不用」，那是纯语法事实，不受内容像不像影响。
    **本项目反复吃亏的地方是「形状对但没接线」，
    而形状对的东西，测试最难分辨。**

    故这里查两件事：调用次数 > 0，且**回复原文出现在产物里**。
    """

    def test_确实调用过模型(self) -> None:
        from zhengtong.adapters.fakes import FakeLlm
        from zhengtong.ports.llm import LlmRequest, LlmResponse

        计数 = {"n": 0}

        class _计数Llm(FakeLlm):
            def complete(self, request: LlmRequest) -> Result[LlmResponse]:
                计数["n"] += 1
                return super().complete(request)

        _跑(TierId.PRO, llm=_计数Llm())
        assert 计数["n"] > 0, "编排层一次都没问过模型"

    def test_模型回复原文进了产物(self) -> None:
        """**光调了不够**——回复可能被调完就丢。

        故查回复原文出现在产物内容里，这才证明「产物来自模型」。
        """
        from zhengtong.adapters.fakes import FakeLlm
        from zhengtong.ports.llm import LlmRequest, LlmResponse

        标记 = "【这是模型的真实回复】"

        class _回标记Llm(FakeLlm):
            def complete(self, request: LlmRequest) -> Result[LlmResponse]:
                return Result.ok(
                    LlmResponse(
                        text=标记,
                        tool_calls=(),
                        finish_reason=FinishReason.STOP,
                        usage=TokenUsage(0, 0, 0),
                    )
                )

        结果 = _跑(TierId.PRO, llm=_回标记Llm()).unwrap()
        assert any(标记 in p.内容 for p in 结果.产物), "模型回了但产物里没有——回复被调完就丢了"

    def test_问谁问的是真编制(self) -> None:
        """**请求里带的角色必须在本档编制内**。

        第一版这条我写成「咨询档零调用」，结果红——
        而**那条判据本身写错了**：咨询档要走成文线的五段、
        每段都要问主理人，零调用才对。

        写错的原因是**把「D-042 挂起不发请求」和「咨询档不发请求」混成一条**。
        前者是关于**状态**的规则（已在 `test_挂起不发起模型请求` 里查），
        后者根本不存在这条规则。

        故改为查真正该查的：**问的人不能超出编制**——
        这才是档位配平（D-029/D-049）关心的事。
        """
        from zhengtong.adapters.fakes import FakeLlm
        from zhengtong.ports.llm import LlmRequest, LlmResponse

        被问: list[str] = []

        class _记录Llm(FakeLlm):
            def complete(self, request: LlmRequest) -> Result[LlmResponse]:
                被问.extend(m.content for m in request.messages if m.role == "system")
                return super().complete(request)

        _跑(TierId.CONSULT, llm=_记录Llm())
        编制外 = {r.value for r in RoleId} - {r.value for r in TIER_BY_ID[TierId.CONSULT].roster}
        被问到的 = {r.value for 串 in 被问 for r in RoleId if f"你是{r.value}" in 串}
        assert 被问到的, "一次都没问——那这条判据就没验到东西"
        assert not (被问到的 & 编制外), f"问了编制外的角色：{被问到的 & 编制外}"

    def test_咨询档序列里根本没有门禁段(self) -> None:
        """**补一条证伪漏掉的判据。**

        第一轮证伪把 `_跑门禁` 里的 `gates_apply` 判断删掉，
        **零红**。查下来原因值得记：咨询档的 `咨询序列` **压根没有
        `GATING` 段**，`_跑门禁` 根本不会被调到——
        `gates_apply` 是**第二道保险**，不是唯一闸门。

        这暴露了原判据的问题：`test_咨询档不触发门禁` 查的是
        **结果**（没走过门禁），而结果相同的原因可以有两个：
        「不进 GATING 段」与「进了但判断跳过」。

        ## 真正的闸门是哪一道

        **阶段序列才是第一道**——咨询档不走成文线（§8.3），
        这是业务分叉不是参数。故必须钉死它：
        咨询序列里**不得出现** `GATING`。

        否则一旦有人把 `GATING` 加进咨询序列，
        而 `gates_apply` 又恰好是 True，咨询档就会静默走门禁——
        而 `test_咨询档不触发门禁` 要到那时才会红，且看不出是被什么触发的。
        """
        assert PhaseId.GATING not in 咨询序列, "咨询档序列里混进了门禁段"
        assert PhaseId.GATING in 阶段序列(TierId.PRO)
        assert PhaseId.GATING in 阶段序列(TierId.FLASH)

    def test_两档都有双保险(self) -> None:
        """**两道闸门都要在**：序列不含 GATING，且 `gates_apply` 为假。

        少任何一道，另一道就成了唯一闸门——
        而单闸门意味着「那一处写错了没人发现」。
        """
        assert not TIER_BY_ID[TierId.CONSULT].gates_apply
        assert PhaseId.GATING not in 阶段序列(TierId.CONSULT)
        assert TIER_BY_ID[TierId.PRO].gates_apply
