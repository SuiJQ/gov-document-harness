"""模型调用端口。

**只要求兼容 OpenAI 风格接口**（D-030）：用户填完整请求地址，
程序**原样请求**，不拼接、不改写 URL。
所以端点在端口里就是一个 `str`——不是 `HttpUrl`，
因为"程序不得改写 URL"是硬要求，URL 对象化就意味着我们有权改它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..domain.errors import Result
from ..domain.event import Span


class FinishReason(StrEnum):
    """结束原因。适配层据此分流（D-032）。

    `CONTENT_FILTER` 与 `LENGTH` 走不同处置：
    前者是合规问题（红线，硬退回），后者是长度问题（可续写）。
    混成一个"失败"就会丢掉这个区分。
    """

    STOP = "stop"
    LENGTH = "length"  # 触到输出上限——可续写，不是错误
    CONTENT_FILTER = "content_filter"  # 合规拦截——硬退回
    TOOL_CALLS = "tool_calls"  # 正常：要执行工具


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """一个工具的声明。描述用中文（D-030：用户交互用严谨中文）。"""

    name: str
    description: str
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型请求调用一个工具。"""

    id: str
    name: str
    arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Message:
    """一条对话消息。`role` 用 OpenAI 风格的英文值，不做本地化——
    它是协议字段，不是界面文案（D-080 的反面：协议处不该本地化）。"""

    role: str  # system / user / assistant / tool
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""

    @property
    def is_assistant_tool_call(self) -> bool:
        return self.role == "assistant" and bool(self.tool_calls)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """用量。**只用于展示与台账**（D-077）。

    上下文占用的判断用 `estimate.py` 的估算值，
    不拿这里的数字当准入依据——它只在响应里才拿得到，
    而超限要在请求发出**之前**就拦住。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class LlmRequest:
    """一次模型调用请求。"""

    model: str
    messages: tuple[Message, ...]
    temperature: float = 0.3  # 基准温度（D-031），各角色可不同
    tools: tuple[ToolSchema, ...] = ()
    max_tokens: int = 0  # 0 表示交由服务端默认
    span: Span | None = None


@dataclass(frozen=True, slots=True)
class LlmResponse:
    """一次模型调用结果。

    `call_id` 在这里而不是由调用方事后拼：它必须与请求同时产生，
    否则重试场景下容易把响应记到错误的 `call_id` 上，
    断点续跑就会跳过一件没真做完的事（D-065）。
    """

    text: str
    finish_reason: FinishReason
    usage: TokenUsage
    tool_calls: tuple[ToolCall, ...] = ()
    call_id: str = ""
    raw_error_code: str = ""  # 服务端结构化错误码，供 error_map 映射中文
    #: 服务端返回的**原始** `finish_reason` 字符串。
    #:
    #: 为什么已有 `finish_reason` 还要留原文：不同厂商用不同词表示同一件事
    #: （OpenAI 用 `length`，部分服务商用 `max_tokens` / `model_length`）。
    #: 归一化后无法区分「正常结束」与「输出被截断」，
    #: 而后者若被当成正常完成，上层会认为内容完整而直接交付——**交付残稿**。
    #: 排查时「服务端到底返回了什么」也是第一个要看的。
    #:
    #: **统一留痕**（不只未知值），否则调用方会遇到「有时有有时没有」。
    raw_finish_reason: str = ""


@runtime_checkable
class LlmPort(Protocol):
    """模型调用。

    实现方负责重试（D-027：失败自动重试 3 次，递增间隔），
    上层不感知重试——否则每个调用点都要写一遍重试逻辑。
    """

    def complete(self, request: LlmRequest) -> Result[LlmResponse]:
        """发起一次调用。失败返回 `Result`，不抛异常（D-064）。"""
        ...

    def probe(self) -> Result[TokenUsage]:
        """连通性探测（D-048 五项：网络、鉴权、调用、工具调用、流式）。

        **流式只检测不使用**（D-032），故本端口不暴露流式方法。
        """
        ...
