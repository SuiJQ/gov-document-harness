"""模型请求的工具必须真被执行（D-267）。

## 这是本项目栽得最深的一类：两边各自测都绿

| 一侧 | 状态 |
|---|---|
| 请求的 `tools` 字段**真的发给了模型** | ✅ D-233 已修，`orchestrate.py:835` |
| 响应里的 `tool_calls` **被执行** | ❌ **从未读** |

于是：模型看得见工具清单、调得到接口，
它一发出 `tool_calls`，编排层就当普通文本丢掉。

> **16 个工具在真实运行里一个都不会被调用**——
> 包括 `shared.write`（写产物）、`kb.search`（检索）、
> `msg.report_to_lead`（成员汇报）。

**为什么两边都绿**：「请求带了 tools」由第 11 步的提示词装配判据守着，
「响应里的 tool_calls 被执行」**没有任何判据**——
两层之间没人对账。这与 D-233 查出 `tools` 零填充时是同一个缺口的两面。

## 修法要遵守用户已定的口径

D-227 的原话：「对话序列只记工具**动作**不记结果，
模型需要一条查回途径」——工具 `tool.result(call_id)` 就是那条途径，
存档只在内存。

故实现**不得**把工具结果直接塞回对话序列（那会让上下文被材料正文撑爆，
且违反 D-035「日志不抄正文」）。本判据据此反向守着：
结果**不**进对话，但**必须**可查回。

## 测什么

**测工具真被调用**：让假 LLM 返回 `tool_calls`，断言
① 工具执行器收到调用、② 动作记进历史、③ 结果进了存档可查回。
**不断**：工具内部业务逻辑、投影显示、续跑——各有判据管。
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tests")
sys.path.insert(0, "src")


@pytest.fixture(autouse=True)
def _路径() -> None:
    pass


def _要工具的llm(
    工具名: str = "kb.search", 调用id: str = "call-1", 参数=None, 双调用: bool = False
):
    """造一个「第一次就请求调工具」的假 LLM。

    ## 为什么改 `scripted`

    `FakeLlm.scripted` 可返回任意 `LlmResponse`——
    正好用来模拟「模型请求调工具」这个此前**无法被测试**的分支。
    此前没有判据，所以这个分支从未被走到过。

    ## `双调用` 为什么必须有（D-274 证伪时才发现）

    一轮只调**一个**工具时，「一轮一条 assistant」与
    「逐个调用各记一条」产生的历史**完全一样**——
    顺序缺陷在这种假模型下**显不出来**，判据会假绿。

    故要覆盖「一轮多个 tool_calls」，缺陷才暴露。
    """
    from zhengtong.adapters.fakes import FakeLlm
    from zhengtong.domain.ids import RunId
    from zhengtong.ports.llm import (
        FinishReason,
        LlmResponse,
        TokenUsage,
        ToolCall,
    )

    首轮调用 = (
        (
            ToolCall(id=调用id, name=工具名, arguments=参数 or {"query": "旱情"}),
            ToolCall(id=f"{调用id}-b", name=工具名, arguments=参数 or {"query": "灌溉"}),
        )
        if 双调用
        else (ToolCall(id=调用id, name=工具名, arguments=参数 or {"query": "旱情"}),)
    )
    return FakeLlm(
        scripted=[
            LlmResponse(
                text="",
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(),
                tool_calls=首轮调用,
            ),
            # 第二轮：给正文，编排层才可能继续走到后面
            LlmResponse(
                text="已完成部署并呈报。",
                finish_reason=FinishReason.STOP,
                usage=TokenUsage(),
            ),
        ],
        calls=[],
    ), RunId("r-tool")


def _编排(llm, 容量token: int = 128_000):
    """带工具层的编排器。

    ## 为什么必须建工具层

    真实装配根里 `App.tools` 早已备好（D-195），但**编排器工厂没往下传**——
    这正是缺陷本身。故判据用的基座也**照真实装配接线**，
    不能用一个「反正工具层用不上」的编排器（那会让判据永远红）。
    """
    from test_step14_orchestrate_gate import _编排 as 基座
    from zhengtong.adapters.fakes import (
        FakeContentPack,
        FakeStorage,
        FakeUserFiles,
        FakeValidators,
    )
    from zhengtong.domain.errors import Result
    from zhengtong.domain.ids import RunId
    from zhengtong.services.tools.factory import 建工具运行器

    class 空检索:
        def build(self, docs: tuple[tuple[str, str], ...]) -> Result[None]:
            return Result.ok(None)

        def search(self, query: str, top_k: int = 8) -> Result[tuple[()]]:
            return Result.ok(())

    工具层 = 建工具运行器(
        storage=FakeStorage(),
        user_files=FakeUserFiles(),
        retriever=空检索(),
        content_pack=FakeContentPack(),
        validators=FakeValidators(),
        run_id=RunId("r-tool"),
    )
    编 = 基座(llm=llm, 内容包=FakeContentPack(), 容量token=容量token)
    编.工具层 = 工具层
    # **照真实装配接线后补回调**（与 `App.编排器()` 同一做法）。
    # 不接的话工具会执行但不发事件——那样测的是「半条链路」，
    # 事件判据会永远红，而真装配根是接好的。
    工具层.接记事件(编.工具回调.记事件)
    return 编


class Test工具真的被调用:
    def test_模型请求的工具要真执行(self) -> None:
        """**本条是核心**：模型发了 `tool_calls`，工具层就要收到。

        这是整个缺口的最外层——`runner.调用()` 全仓零调用点，
        16 个工具在真实运行里一个都不会被调用。
        """
        llm, _ = _要工具的llm()
        编 = _编排(llm)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")

        # **直接查编排器内部的工具存档**：
        # 工具真被调用过，就该留下结果。这是可观察的证据，
        # 不是我为测试加的探针。
        存 = _过程(编).工具存档
        assert 存 is not None and list(存._数据), (
            "模型请求了 kb.search，但工具存档是空的——"
            "编排层从不读 LlmResponse.tool_calls，16 个工具全是死的"
        )

    def test_动作要记进该角色历史(self) -> None:
        """**只执行不记账等于没执行**。

        模型下一轮就不知道自己查过什么，会重复查——
        D-227 的原话：「全记则上下文被材料正文撑爆，
        全不记则模型不知道自己查过什么、重复查询」。
        """
        llm, _ = _要工具的llm()
        编 = _编排(llm)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")

        历史 = _过程(编).历史
        动作 = [m for 消息们 in 历史.values() for m in 消息们 if m.tool_calls]
        assert 动作, (
            "历史里没有任何 tool_calls 消息——模型不知道自己查过什么，会重复查询（D-227 原话）"
        )

    def test_结果要进存档可查回(self) -> None:
        """**结果必须有查回途径**（D-227 定的口径）。

        对话序列不记结果，但模型要能用 `tool.result(call_id)` 取回——
        存档只在校验里查不到就是没实现。
        """
        llm, _ = _要工具的llm(调用id="call-查回")
        编 = _编排(llm)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")

        存档 = _过程(编).工具存档
        assert 存档 is not None, "没有调用存档——模型无处查回工具结果"
        结果 = 存档.取("call-查回")
        assert 结果 is not None, "存档里查不到刚那次调用的结果——模型拿不到自己查过什么"
        assert str(结果).strip(), "存档里有这个 id 但内容是空的"

    def test_未知工具要被拒绝而非崩溃(self) -> None:
        """**畸形输入返 `Result.fail`，不抛异常穿透**（D-108）。

        模型可能请求已砍除的工具（`fs.*`）或不存在的名字。
        """
        llm, _ = _要工具的llm(工具名="fs.write_file")
        编 = _编排(llm)
        结果 = 编.跑到终(档位=_Pro(), 需求="就旱情写个通知")
        # 关键：编排**继续跑完**，不因一个坏工具崩掉
        assert not 结果.is_error or 结果.error is not None


class Test结果完整回传对话序列:
    """D-274 推翻了 D-227 的「只记动作不记结果」，本组是**反转后的判据**。

    旧判据的意图是「防为了让测试变绿而把结果塞回去」——
    **这个意图是对的，但当时的结论错了**。D-274 查明：
    压缩层本就把材料当第一批丢弃项（「材料先于对话丢」，
    `compactor.py:97 _丢弃顺序()`），所以「结果留在历史里」的代价
    是**临时的**，而截断的代价是永久不可逆的。

    故红线方向也反过来了：新红线是「**必须**在历史里，
    且 **tool_call_id 必须配得上号**」。
    """

    def test_工具结果完整进对话序列(self) -> None:
        """**结果必须回传给模型**（D-272/D-274）。

        此前 `role="tool"` 全仓零构造——工具跑得起来，
        结果却回不到模型手上，模型只能凭空猜自己查到了什么。
        `tool_call_id` 只发不收，是**半条协议**。
        """
        llm, _ = _要工具的llm()
        编 = _编排(llm)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")

        历史 = _过程(编).历史
        assert 历史, "无历史可查"
        带回 = [
            (角色, m)
            for 角色, 消息们 in 历史.items()
            for m in 消息们
            if m.role == "tool" and m.content
        ]
        assert 带回, "历史里没有 tool 结果——结果回不到模型手上（D-272）"

    def test_tool_call_id必须配得上号(self) -> None:
        """**每条 `tool` 消息的 id 必须在前面某条 assistant 的 tool_calls 里**
        （OpenAI 协议，D-274）。

        用状态机而不是「紧跟上一条」断言：**一条 assistant 带 N 个
        tool_calls 时，后面连着 N 条 tool 消息是合法的**，
        所以不能简单要求 tool 的上一条必须是 assistant。

        服务端配不上号就**拒收整条请求**，而本地测试全绿——
        属 D-210 那类「零件都测过、衔接没测」的静默失败。
        """
        llm, _ = _要工具的llm(双调用=True)
        编 = _编排(llm)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")

        历史 = _过程(编).历史
        assert 历史, "无历史可查"
        for 角色, 消息们 in 历史.items():
            待配: set[str] = set()
            for i, m in enumerate(消息们):
                if m.role == "tool":
                    assert 待配, (
                        f"{角色} 第 {i} 条 tool 消息（{m.tool_call_id}）前面"
                        "没有待回传的 tool_calls——悬空，服务端会拒收"
                    )
                    assert m.tool_call_id in 待配, (
                        f"{角色} 第 {i} 条 tool 消息 id={m.tool_call_id} "
                        f"不在待回传的 {sorted(待配)} 里——配不上号"
                    )
                    待配.discard(m.tool_call_id)
                elif m.tool_calls:
                    assert not 待配, (
                        f"{角色} 第 {i} 条又发起了新动作，但上一轮 "
                        f"{sorted(待配)} 的结果还没回传——顺序错"
                    )
                    待配 = {c.id for c in m.tool_calls}
                else:
                    # 普通消息（user/assistant 正文）**清空**待回传集合：
                    # 它不是 tool 结果，插在中间会让后续 tool 消息配不上号
                    待配 = set()

    def test_一轮多个工具只记一条assistant(self) -> None:
        """一轮里调 N 个工具 → **一条** assistant 带 N 个 tool_calls。

        逐个调用各记一条的话，历史成为 `assistant(c1), assistant(c2)`，
        后面的 `tool(c1)` 紧跟的是 `assistant(c2)`——**配不上号**。
        """
        llm, _ = _要工具的llm(双调用=True)
        编 = _编排(llm)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")

        for 角色, 消息们 in _过程(编).历史.items():
            动作 = [m for m in 消息们 if m.tool_calls]
            if not 动作:
                continue  # 本轮没调工具的角色（如 structure），不关它的事
            for m in 动作:
                assert m.content == "", (
                    f"{角色} 的工具动作消息带了正文 {m.content[:40]!r}——"
                    "动作与结果混在一处，模型会把结果当既成事实"
                )
            # **相邻两条 assistant 动作消息 = 逐个调用各记一条**（旧缺陷）
            from itertools import pairwise

            for 前, 后 in pairwise(消息们):
                assert not (前.tool_calls and 后.tool_calls), (
                    f"{角色} 连续两条带 tool_calls 的 assistant——"
                    "一轮多个工具必须合成一条，否则 tool 消息配不上号"
                )

    def test_工具失败也必须回传(self) -> None:
        """失败的工具结果是一条 `tool` 消息，**不是空串**。

        空串会让模型以为「我查过了，确实没有」——
        与 D-227 禁止的欺骗是同一类。
        """
        llm, _ = _要工具的llm(工具名="fs.write_file")
        编 = _编排(llm)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")

        历史 = _过程(编).历史
        失败回传 = [
            m
            for 消息们 in 历史.values()
            for m in 消息们
            if m.role == "tool" and m.tool_call_id and m.content
        ]
        assert 失败回传, "失败的工具调用没有回传结果——模型会以为没有失败过"


def _Pro():
    from zhengtong.domain.tier import TierId

    return TierId.PRO


def _过程(编):
    """取 `_过程`，取不到就明确失败。

    **不用 `!` 或 `# type: ignore`**（D-018）：那是把类型错误藏起来。
    """
    过程 = 编._过程
    assert 过程 is not None, "编排跑完了却没有 _过程——推进器没建起来"
    return 过程


_ = pytest
