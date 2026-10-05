"""模型调用端口。

**只要求兼容 OpenAI 风格接口**（D-030）：用户填完整请求地址，
程序**原样请求**，不拼接、不改写 URL。
所以端点在端口里就是一个 `str`——不是 `HttpUrl`，
因为"程序不得改写 URL"是硬要求，URL 对象化就意味着我们有权改它。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Protocol, runtime_checkable

from ..domain.errors import Result
from ..domain.event import Span
from ..domain.reasoning import 推理开关


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


#: 服务端 `finish_reason` → 本地枚举。
#:
#: **别名是实测来的**：OpenAI 用 `length`，部分服务商用
#: `max_tokens` / `model_length` 表示同一件事。**只认 `length`
#: 会把「输出被截断」判成「正常完成」**——上层认为内容完整而交付，
#: 交付的是残稿。
#:
#: 键统一转小写后查，故大小写差异也能命中。
#:
#: **放在端口而不在适配器**（D-268）：这张表是**协议知识**，
#: 不是某一家的实现细节。编排层要判断「这个结束原因认不认识」
#: （不认识就不能断定内容完整），适配器与编排层必须查同一张表——
#: 各存一份的话，改了一处忘了另一处，静默把残稿当完整稿交付。
结束原因对照: dict[str, FinishReason] = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "max_tokens": FinishReason.LENGTH,  # 常见别名
    "model_length": FinishReason.LENGTH,  # 常见别名
    "content_filter": FinishReason.CONTENT_FILTER,
    "content-filter": FinishReason.CONTENT_FILTER,  # 连字符变体
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,  # 旧版工具调用
}


def 是已知结束原因(原值: object) -> bool:
    """服务端这个 `finish_reason` 认不认识。

    **不认识就不能断定内容完整**（D-268）。原先未知值一律归 `STOP`，
    而 `STOP` 的含义是「正常结束」——于是厂商新增一个词，
    程序就会把可能截断的稿子当完整的交付出去。
    """
    return isinstance(原值, str) and 原值.lower() in 结束原因对照


def 归一化结束原因(原值: object) -> FinishReason:
    """把服务端的 `finish_reason` 归一化。

    **未知值归 `STOP` 但原文另行留痕**（`LlmResponse.raw_finish_reason`）：
    归一化是给分流逻辑用的，留痕是给排查与兜底判断用的，两者都要。

    **调用方若要区分「真的正常停」与「不认识」，须先用
    `是已知结束原因` 判一次**——只拿归一化结果会看不出区别。
    """
    if isinstance(原值, str):
        命中 = 结束原因对照.get(原值.lower())
        if 命中 is not None:
            return 命中
    return FinishReason.STOP


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
    #: **原样附加到请求体的额外字段**（D-245）。
    #:
    #: 只为推理关闭探测而设（架构 §6.2 的 `thinking` /
    #: `chat_template_kwargs` / `reasoning_effort` 三种写法）。
    #:
    #: 为什么要有这个字段：那些写法**不是 OpenAI 标准**，
    #: 但 D-004 要求「端点原样请求，程序不拼接不改写」——
    #: 于是既不能把三种写法硬编进 `_请求体`（那会让正常请求
    #: 带上后端专属字段），也不该让探测模块去拼 URL 或改体。
    #:
    #: **默认空**：正常路径完全不受影响，序列化时什么都不加。
    额外字段: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class LlmResponse:
    """一次模型调用结果。

    `call_id` 在这里而不是由调用方事后拼：它必须与请求同时产生，
    否则重试场景下容易把响应记到错误的 `call_id` 上，
    断点续跑就会跳过一件没真做完的事（D-065）。
    """

    text: str
    finish_reason: FinishReason
    #: **可以是 `None`**（D-384）。
    #:
    #: 早先标注为 `TokenUsage`（非可选），而 `reasoning_probe._推理token`
    #: 与 `orchestrate._记用法` 都在判 `usage is None`——**标注与运行时行为
    #: 相反**。后果是每个构造「后端不回报用量」的响应都得骗类型
    #: （`cast(TokenUsage, None)` 或 `# type: ignore`），而 D-018 禁后者。
    #:
    #: 让如实状态能如实存在，比保住一个「非空」的漂亮标注重要。
    usage: TokenUsage | None
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


@dataclass(frozen=True, slots=True)
class 探测结论:
    """五项探测的逐项结论。**三态**：通过 / 未通过 / 未测。

    ## 为什么不能只有一个总判

    五项探测**有先后依赖**：鉴权不过，工具调用与推理探测**根本发不出请求**。
    笼统报一条「连接失败」等于让用户自己猜还有几项没验；
    报「未通过」是撒谎（它没失败，它没机会跑）。

    故 `未测` 单列一态，`取()` 返回 `True/False/None` 三值（D-295）。

    ## 为什么是「通过项」一个元组，而不是五个布尔字段

    五个布尔字段能表示**互相矛盾**的状态——比如
    「网络连通=True 且它在未测里」「三态之外多出第四种组合」。
    用 `通过项` 一个元组则**矛盾状态不可表示**：
    不在 `通过项` 里、也不在 `未测` 里的项，就是未通过。

    附带好处：端口层不再有五个「只在本类里被读」的字段——
    `check_port_fields.py` 会把那种字段报成零消费候选，
    而它们本该由本类消费，是**真消费**。
    """

    #: 通过的项名。**其余不在 `未测` 里的项即未通过**。
    通过项: tuple[str, ...] = ()
    #: 逐项详情。**键是项名**，值给用户看（失败原因或通过说明）。
    详情: Mapping[str, str] = field(default_factory=dict)
    #: 因前项失败而没跑的项名。
    未测: tuple[str, ...] = ()

    #: **实测出的推理开关结论**（D-380/D-384）。
    #:
    #: ## 为什么必须带着它穿过探测层
    #:
    #: 此前 `llm_probe` 把探测结果压成 `详情["推理开关"] =
    #: "推理可关闭（第 2 种写法生效）"` —— **一句话，序号留在了适配器里**。
    #: 于是运行时永远不知道该用哪种写法，
    #: `写法表` 里的四道关闭写法**从未用于关闭任何一次请求**：
    #: 测了、报告了、用户看得懂，然后丢掉（D-260/D-263 同型）。
    #:
    #: ## 为什么存整个结论而不仅一个序号
    #:
    #: 早先存 `推理写法: int`，而 `0` 同时表示「关不掉」
    #: 「模型本来就不推理」「后端不报推理用量」三件事——
    #: 运行时读回 0 就分不清该对用户说哪句话，只能一律说「关不掉」。
    #: 故存 `推理开关`（含 `状态`），让五种情形各有各的话。
    #:
    #: 字段名不叫 `推理开关` 是为了**不遮蔽类型名**：
    #: 类体内的 `default_factory` 在字段绑定前求值，
    #: 同名字段会把 `推理开关` 从「类型」变成「自己」，默认工厂就废了。
    推理结论: 推理开关 = field(default_factory=推理开关)

    #: 项序常量。**不要另写一份**——探测层、展示层与判据都按它遍历。
    项序: ClassVar[tuple[str, ...]] = ("网络连通", "鉴权", "模型调用", "工具调用", "推理开关")

    def 取(self, 项名: str) -> bool | None:
        """三态取值：`True` 通过、`False` 未通过、**`None` 未测**。"""
        if 项名 in self.未测:
            return None
        return 项名 in self.通过项


@runtime_checkable
class LlmPort(Protocol):
    """模型调用。

    实现方负责重试（D-027：失败自动重试 3 次，递增间隔），
    上层不感知重试——否则每个调用点都要写一遍重试逻辑。
    """

    def complete(self, request: LlmRequest) -> Result[LlmResponse]:
        """发起一次调用。失败返回 `Result`，不抛异常（D-064）。"""
        ...

    def probe(self) -> Result[探测结论]:
        """连通性探测（D-295 五项：网络连通、鉴权、模型调用、工具调用、推理开关）。

        **返回逐项结论**：前项失败则后项记为「未测」，不谎报未通过。
        流式探测已按 D-295 作废，取代它的是推理开关（D-048）。

        流式探测已按 D-295 整体作废，故本端口**不暴露任何流式方法**。
        """
        ...
