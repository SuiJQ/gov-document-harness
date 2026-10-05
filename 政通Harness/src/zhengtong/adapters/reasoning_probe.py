"""推理关闭探测（架构 §6.2，D-245）。

## 为什么要单独一个文件

架构 §6.2 原文：它是「**唯一一处**需要知道后端烙印的地方」。

正常请求体**不得**带上任何后端专属字段——那是 D-004
「端点原样请求，程序不拼接不改写」的延伸。
所以三种关闭推理的写法全部收在这里，不散进 `http_llm.py`。

## 四种写法与各自的后端

| # | 写法 | 后端 |
|---|---|---|
| 1 | `thinking: {"type": "disabled"}` | vLLM、多数 OpenAI 风格服务 |
| 2 | `chat_template_kwargs: {"enable_thinking": false}` | llama.cpp 新版、transformers |
| 3 | `reasoning_effort: "none"` | OpenAI 风格后端 |
| 4 | 提示词前缀 `/no_think` | llama.cpp 老版、Qwen 模板 |

## 判定依据是 reasoning_tokens，不是 HTTP 状态码

架构 §6.2 原文强调：

> **四种无效写法全部返回 HTTP 200**（D-048 实测），
> 只看状态码会误判为「支持关闭」。

故判定是：**关掉之后 `reasoning_tokens` 是否降为 0**。

## 全败不静默

四种全败时如实报告「这个服务关不掉推理，将按开启运行」，
并把用哪种写法记下来（D-051 重试时要用）。

**不静默失败**——用户需要知道模型在思考，
否则「为什么这次这么慢」「为什么输出这么长」无从解释。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..domain.errors import ErrorCode, Result
from ..domain.reasoning import 推理开关, 推理状态
from ..ports.llm import LlmRequest, LlmResponse, Message

#: 探测用的提示词。**带 `/no_think` 前缀**是第 4 种写法之一，
#: 但它同时也是最短的合法提示词，故四种写法都用它——
#: 这样第 4 种天然生效，其余三种测的是「参数有没有被认」。
_探测词 = "/no_think ping"

#: 输出额度。**给足**：推理模型要先思考再回答，
#: 额度太小会在思考阶段就被截断，正文为空，
#: 那样测出来的是「额度不够」而不是「关不掉推理」。
_探测额度 = 512

#: 四种写法。**顺序即尝试顺序**——前面的是主流写法，
#: 少发一次请求就少一次往返。
#:
#: 每项是「要附加到请求体的键值对」。第 4 种是提示词前缀，
#: 不需要额外字段，故为空元组。
写法表: tuple[tuple[str, tuple[tuple[str, object], ...], str], ...] = (
    ("thinking.disabled", (("thinking", {"type": "disabled"}),), "vLLM 等"),
    (
        "chat_template_kwargs",
        (("chat_template_kwargs", {"enable_thinking": False}),),
        "llama.cpp 新版、transformers",
    ),
    ("reasoning_effort.none", (("reasoning_effort", "none"),), "OpenAI 风格"),
    ("提示词前缀 /no_think", (), "llama.cpp 老版、Qwen 模板"),
)


def _请求(模型: str, 额外: Sequence[tuple[str, object]]) -> LlmRequest:
    return LlmRequest(
        model=模型,
        messages=(Message(role="user", content=_探测词),),
        temperature=0.0,
        max_tokens=_探测额度,
        额外字段=tuple(额外),
    )


def _推理token(响应: LlmResponse) -> int:
    """取 `reasoning_tokens`。**字段缺失返回 `-1`，不是 0**。

    响应里没这个字段本身就是「这个后端不报推理用量」，
    按 0 处理会让判定误以为「关掉了」——
    故这里与调用方约定：**只有字段确实存在才参与判定**（即 `-1` 不参与）。

    ## 为什么这个约定必须在**调用处**也被遵守

    本函数早先的 docstring 第一句写的是「没有就是 0」，
    与实际返回值 `-1` 自相矛盾；而调用方又用 `基准推理 <= 0` 一刀切，
    于是 `-1` 被当成「不推理」——**docstring 警告的陷阱，实现自己踩了**。

    现在 `-1` 有自己的终态（`推理状态.无法判定`），
    不再依赖调用方记得特判即可正确。
    """
    if 响应.usage is None:
        return -1
    return 响应.usage.reasoning_tokens


#: 「发一个请求」的函数类型。**用 Callable 而非 Protocol**：
#: 调用方（`HttpLlm.complete`）本就是方法，直接传即可，
#: 让它满足一个协议反而要写一个包装类。
发请求 = Callable[[LlmRequest], "Result[LlmResponse]"]


def 探测推理(模型: str, 发: 发请求) -> Result[推理开关]:
    """按序试探四种写法，返回**五态**结论（不是「能不能关」两个值）。

    `发` 是一个「发请求」的函数：接收 `LlmRequest` 返回
    `Result[LlmResponse]`。用函数而非 Protocol 是为了让
    调用方（`HttpLlm`）不必为探测另开一个类。

    ## 判定顺序不可调换

    先看基准，再谈关闭——因为「不需要关」必须先于「能不能关」判定：
    一个本来就不推理的模型，四种写法测下来必然「关不掉」，
    若先跑写法表，它会被误判成「强制开启、关不掉」。
    这正是修复前的缺陷。
    """
    基准 = _实测(模型, 发, ())
    if 基准.is_error:
        错 = 基准.error
        assert 错 is not None
        # **类型必须一致**：返回 `Result[LlmResponse]` 而这里要
        # `Result[推理开关]`，故显式重建错误而不是直接转发
        return Result.fail(错.code, 错.detail)
    基准值 = 基准.value
    assert 基准值 is not None
    基准推理 = _推理token(基准值)

    # **先特判 -1**（后端不报推理用量）——`-1 <= 0` 成立，
    # 若不先拦，它会被下一条当成「基准没有推理」。
    # 这正是 `_推理token` docstring 警告过的契约，两次踩了同一个坑。
    if 基准推理 < 0:
        return Result.ok(推理开关(状态=推理状态.无法判定))
    # 基准就没有推理 → 本模型根本不思考，**不需要关**。
    # 这不是「四种全败」，也不是「关不掉」。
    if 基准推理 == 0:
        return Result.ok(推理开关(状态=推理状态.无需关闭))

    for 序, 写法 in enumerate(写法表, start=1):
        额外 = 写法[1]
        if not 额外:
            # 第 4 种是提示词前缀，已在 _探测词 里，无需额外字段
            got = _实测(模型, 发, ())
        else:
            got = _实测(模型, 发, 额外)
        if got.is_error:
            continue  # 该写法被拒（有的后端会 400），试下一种
        值 = _推理token(got.unwrap())
        # **判定看 reasoning_tokens 是否降为 0**，不看 HTTP 状态码——
        # 四种无效写法全部返回 200（D-048 实测）。
        # 该次返回 -1 时**不算关掉**：判不出来就不能当成关掉了，
        # 宁可继续试下一种。
        if 值 == 0:
            return Result.ok(推理开关(状态=推理状态.能关闭, 可用写法=序))
    return Result.ok(推理开关(状态=推理状态.关闭不了))


def _实测(模型: str, 发: 发请求, 额外: Sequence[tuple[str, object]]) -> Result[LlmResponse]:
    请求 = _请求(模型, 额外)
    结果 = 发(请求)
    if 结果.is_error:
        错 = 结果.error
        assert 错 is not None
        return Result.fail(错.code, f"推理探测请求失败：{错.detail}")
    值 = 结果.value
    if 值 is None:
        return Result.fail(ErrorCode.LLM_REQUEST_FAILED, "推理探测无响应")
    return Result.ok(值)


__all__ = ["写法表", "发请求", "探测推理"]
