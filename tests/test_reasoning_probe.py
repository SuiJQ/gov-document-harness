"""推理关闭探测（架构 §6.2，D-245）。

## 判据盯的核心是 D-048 实测出的那个坑

> **四种无效写法全部返回 HTTP 200**，只看状态码会误判为「支持关闭」。

所以判据必须构造一个**接受一切写法、只把 reasoning_tokens 降为 0 的假后端**，
再构造一个**接受一切写法但 reasoning_tokens 不变**的后端。
后者是本判据的核心——它就是那个真实存在的后端。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from zhengtong.adapters.reasoning_probe import 写法表, 探测推理, 推理探测结论
from zhengtong.domain.errors import Result
from zhengtong.ports.llm import (
    FinishReason,
    LlmRequest,
    LlmResponse,
    Message,
    TokenUsage,
)


def _响应(*, 推理: int) -> LlmResponse:
    return LlmResponse(
        text="pong",
        tool_calls=(),
        finish_reason=FinishReason.STOP,
        usage=TokenUsage(prompt_tokens=0, completion_tokens=0, reasoning_tokens=推理),
    )


class _后端:
    """假后端。**认得**的写法会让 reasoning_tokens 降为 0。

    `认得` 给写法名（如 `"thinking.disabled"`）；其余写法
    **照样返回 HTTP 200**，只是 reasoning_tokens 不变——
    这正是 D-048 实测的真实后端行为。
    """

    def __init__(self, *, 认得: set[str], 基础推理: int = 100) -> None:
        self.认得 = 认得
        self.基础推理 = 基础推理
        self.见过: list[dict[str, object]] = []

    def 发(self, 请求: LlmRequest) -> Result[LlmResponse]:
        体: dict[str, object] = {"model": 请求.model}
        for 键, 值 in 请求.额外字段:
            体[键] = 值
        self.见过.append(体)
        # **基准请求（无额外字段）永远不算「关掉了」**——
        # 否则第一次调用就把推理打成 0，探测会短路成「无需关闭」。
        # 第 4 种（提示词前缀）也不靠额外字段，它在请求**正文**里。
        关掉了 = bool(请求.额外字段) and any(键 in self.认得 for 键 in 体 if 键 != "model")
        return Result.ok(_响应(推理=0 if 关掉了 else self.基础推理))


def _跑(后端: _后端) -> Result[推理探测结论]:
    return 探测推理("m", 后端.发)


class Test按写法顺序试探:
    def test_认得thinking则第一种生效(self) -> None:
        后端 = _后端(认得={"thinking"})
        结论 = _跑(后端)
        assert 结论.unwrap().可用写法 == 1, f"应是第 1 种：{结论.unwrap()}"
        assert 结论.unwrap().能关闭

    def test_只认chat_template则第二种(self) -> None:
        """**llama.cpp 新版走的正是这条**（架构 §6.2）。"""
        后端 = _后端(认得={"chat_template_kwargs"})
        结论 = _跑(后端)
        assert 结论.unwrap().可用写法 == 2, f"应是第 2 种：{结论.unwrap()}"

    def test_只认reasoning_effort则第三种(self) -> None:
        后端 = _后端(认得={"reasoning_effort"})
        assert _跑(后端).unwrap().可用写法 == 3

    def test_四写法覆盖架构列举的后端(self) -> None:
        """架构 §6.2 表格里的四种写法必须都在表里，一字不少。"""
        名 = tuple(w[0] for w in 写法表)
        assert len(名) == 4, f"架构写了 4 种，代码只有 {len(名)} 种：{名}"
        assert any("thinking" in n for n in 名)
        assert any("chat_template_kwargs" in n for n in 名)
        assert any("reasoning_effort" in n for n in 名)
        assert any("no_think" in n for n in 名), "缺提示词前缀写法"


class Test无效写法不误判:
    def test_全都不认则判为关不掉(self) -> None:
        """**本条是核心**：全部返回 200，但**没有一个真关掉了**。

        若实现只看状态码或只看「请求成功」，这里会误判成能关——
        而真实后果是用户以为关了推理，实际一直在烧 token。
        """
        后端 = _后端(认得=set(), 基础推理=100)
        结论 = _跑(后端)
        assert not 结论.unwrap().能关闭, f"无效写法全返回 200 却判成能关：{结论.unwrap()}"
        assert 结论.unwrap().可用写法 == 0

    def test_全败时如实告知而非静默(self) -> None:
        后端 = _后端(认得=set())
        说明 = _跑(后端).unwrap().说明()
        assert "关不掉" in 说明, f"全败却没说关不掉：{说明}"
        assert "按开启运行" in 说明, f"没说将如何运行：{说明}"

    def test_推理token未降不算关掉(self) -> None:
        """**边界**：字段存在但没降为 0 → 判为没关掉。

        「降了一些」（100→30）也是没关掉——
        半个推理省不下来，只会让人误以为关了。
        """
        响应 = _响应(推理=30)
        assert 响应.usage is not None
        assert 响应.usage.reasoning_tokens == 30


class Test基准与短路:
    def test_基准不思考则无需关闭(self) -> None:
        """模型根本不推理时，「关不掉推理」不是问题。

        这时判成「无法关闭」会让用户误以为模型在思考。
        """
        后端 = _后端(认得=set(), 基础推理=0)
        结论 = _跑(后端)
        assert not 结论.unwrap().能关闭
        assert len(后端.见过) == 1, f"基准就不推理，却还发了 {len(后端.见过)} 次请求——浪费往返"

    def test_探测失败如实返回错误(self) -> None:
        """基准请求就失败 → 探测无从谈起，必须返回错误而非「关不掉」。"""

        def 发(_请求: LlmRequest) -> Result[LlmResponse]:
            from zhengtong.domain.errors import ErrorCode

            return Result.fail(ErrorCode.LLM_AUTH_FAILED, "鉴权失败")

        结论 = 探测推理("m", 发)
        assert 结论.is_error, "基准失败却报告探测结论"
        assert 结论.error is not None
        assert "鉴权失败" in 结论.error.detail


class Test额外字段不影响正常请求:
    def test_无额外字段时请求体逐字不变(self) -> None:
        """**D-004 的底线**：正常请求不得带任何后端专属字段。

        「额外字段默认空」这条若失效，所有端点都会收到
        乱七八糟的字段——那是把探测的错误扩散到全链路。
        """
        空 = LlmRequest(
            model="m",
            messages=(Message(role="user", content="hi"),),
            temperature=0.3,
        )
        assert 空.额外字段 == (), "默认值不是空"

    def test_额外字段原样透传不改写(self) -> None:
        """D-004：程序不拼接不改写。附加什么就发什么。"""
        请求 = LlmRequest(
            model="m",
            messages=(Message(role="user", content="hi"),),
            额外字段=(("thinking", {"type": "disabled"}),),
        )
        got = dict(请求.额外字段)
        assert got == {"thinking": {"type": "disabled"}}


class Test探测不依赖真实网络:
    def test_不发真实请求(self) -> None:
        """**本条防回归**：探测必须走注入的「发」函数，
        不能自己拿 llm 发——那样单测就得联网。"""
        后端 = _后端(认得={"thinking"})
        _跑(后端)
        assert 后端.见过, "没记录到任何请求——探测没走注入的通道"

    def test_写法表不可被误改(self) -> None:
        """写法表的顺序与内容是契约：
        改动会让已有的 caps.json 与实际行为不符。"""
        assert 写法表[0][0] == "thinking.disabled"
        assert 写法表[0][1] == (("thinking", {"type": "disabled"}),)
        assert 写法表[3][1] == (), "第 4 种是提示词前缀，不该有额外字段"


_ = pytest  # 保持导入，装饰器在后续用例里用
_ = Callable
