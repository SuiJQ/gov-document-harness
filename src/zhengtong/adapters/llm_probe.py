"""D-048 五项连接探测。

## 五项是什么

架构 §4.2 定的检测顺序——**前一项不过就不测后一项**：

| 序 | 项 | 怎么测 | 不过说明什么 |
|---|---|---|---|
| 1 | 网络可达 | 任何 HTTP 往返 | 断网 / 端点地址错 |
| 2 | 鉴权 | 带密钥发一次 | 密钥错、没填 |
| 3 | 模型可调 | 发一次极短请求 | 模型名错、服务端不认 |
| 4 | 支持工具调用 | 带 `tools` 发一次 | 专家团跑不了 |
| 5 | 流式能力 | 发一次流式收状态码 | **只检测不使用**（D-032） |

## 为什么逐项而不多发一次

四项检测塞进一个请求里，**失败时无法定位是哪一项坏了**。
用户看到「连接失败」不知道该换密钥还是换模型名。
逐项测的代价是多几次往返，换来的是能报出确切原因。

## 探测用什么提示词

`ping`。**不是**真实业务提示词——连接检测要回答的是
「通不通、能不能干这活」，不是「模型答得好不好」。
用业务提示词会把「模型写得不好」混进「连接有问题」。

## max_tokens 要给足

坑一：`max_tokens` 设小时推理会挤占额度，正文返回 `null`。
探测请求若因这个拿到空正文，会被误判成「模型不可用」。
故探测一律给 `max_tokens=64`，够放一个 `ping`。

## 为什么这里不 import HttpLlm

探测要用 `LlmRequest`/`ToolSchema`（来自端口，可以直接 import），
但**不需要** `HttpLlm` 本身——它只用到 `model`、`complete`、`探测流式` 三样。
故这里声明一个本地 `Protocol` 而不是 import 具体类：

- import `HttpLlm` 会与 `http_llm` 构成**环**（`http_llm.probe` 调本模块），
  类型检查器报 `Cycle detected`，而运行期可能某个 import 顺序才崩；
- 声明 `Protocol` 后依赖方向是单向的，且探测逻辑**可测**——
  给它一个假实现就能测，不必碰真网络。
"""

from __future__ import annotations

import contextlib
from typing import Protocol

from ..domain.errors import ErrorCode, Result
from ..ports.llm import LlmRequest, LlmResponse, Message, TokenUsage, ToolSchema


#: 探测只用得上这三样。**声明 Protocol 而非 import `HttpLlm`**，理由见模块文档。
class _可探测(Protocol):
    @property
    def model(self) -> str: ...

    # **返回 `LlmResponse` 而不是 `TokenUsage`**——
    # 探测要看正文（空正文要报错），TokenUsage 里没有正文。
    # 声明错了运行期照样跑通（`got.unwrap().text` 拿到的确实是响应），
    # 只在类型检查时暴露。**这类错最险：看起来对，跑起来也对，
    # 直到某次重构按类型去改它。**
    def complete(self, request: LlmRequest) -> Result[LlmResponse]: ...

    def 探测流式(self) -> int: ...


#: 探测提示词。**极短**——探测的是链路，不是写作能力。
_探测词 = "ping"

#: 探测给的输出额度。**给足**，理由见模块文档。
_探测额度 = 64


def _请求(
    模型: str,
    词: str = _探测词,
    *,
    工具: tuple[ToolSchema, ...] = (),
) -> LlmRequest:
    """构造探测请求。**参数逐个显式列出**，不用 `**kw: object`。

    `**kw` + `dict[str, object]` 会让每个字段都退化成 `object`，
    Pyright 一次报六个「不能赋给 `str`/`float`/`int`」——
    **用 `object` 绕过类型检查，本项目已多次否掉（D-159）**。
    """
    return LlmRequest(
        model=模型,
        messages=(Message(role="user", content=词),),
        temperature=0.0,
        tools=工具,
        max_tokens=_探测额度,
    )


def 探测五项(llm: _可探测) -> Result[TokenUsage]:
    """跑完五项，全过才返回用量。

    **任一项不过立即返回**，带该项对应的错误码与中文原因——
    用户要能据此知道该改哪一项，而不是笼统的「连接失败」。
    """
    # 1 网络可达 + 2 鉴权 + 3 模型可调：一次极短请求即可确认。
    #    拆成三次要多花两次往返，而这三项的失败原因可以从
    #    同一个响应的错误码里区分出来（见 http_llm 的错误映射）。
    got = llm.complete(_请求(llm.model))
    if got.is_error:
        err = got.error
        assert err is not None
        # 重试用尽不改变「连不上」这个事实，但错误码要说清
        if err.code is ErrorCode.LLM_RETRY_EXHAUSTED:
            return Result.fail(
                ErrorCode.LLM_REQUEST_FAILED,
                f"网络可达但调用失败：{err.detail}",
            )
        return Result.fail(err.code, err.detail)

    响应 = got.unwrap()
    if not 响应.text.strip():
        # 有响应但正文空——多半是 max_tokens 不够或模型异常
        return Result.fail(
            ErrorCode.LLM_REQUEST_FAILED,
            "模型返回了空正文（可能输出额度不足或模型不可用）",
        )

    # 4 工具调用支持
    工具请求 = _请求(
        llm.model,
        "调用 ping 工具",
        工具=(ToolSchema(name="ping", description="连通性探测工具", parameters={}),),
    )
    got2 = llm.complete(工具请求)
    if got2.is_error:
        err2 = got2.error
        assert err2 is not None
        return Result.fail(
            ErrorCode.LLM_REQUEST_FAILED,
            f"工具调用探测失败：{err2.detail}",
        )
    # **服务端接受但不触发不阻断**：有些模型不主动调工具。
    # 专家团运行时靠提示词引导，硬拦在这里会误杀可用端点。

    # 5 流式能力（只检测不使用，D-032）
    # `suppress` 而非 `try/except/pass`——后者读起来像漏写了处理。
    with contextlib.suppress(Exception):
        _ = llm.探测流式()

    # 这里没有类型标注可用：**实测过，标注只是把 `Unknown`
    # 从返回值搬到变量上，消不掉**。理由见 D-163。
    return Result.ok(响应.usage)
