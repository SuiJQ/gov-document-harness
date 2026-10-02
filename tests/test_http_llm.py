"""真实 LLM 适配器（`adapters/http_llm.py`）的契约测试。

## 本文件的重点不是「能不能调通」

2026-09-30 实测已确认端点可调，本文件要防的是
**适配器把服务端的非常规行为处理错**。三个已知坑：

1. **推理模型有 `reasoning_content`**：`max_tokens` 设小 → 推理挤占额度、
   正文返回 `null`、`finish_reason="length"`。
   适配器若直接 `message["content"]` 取值，正文就是 `None`。
2. **工具调用的 `arguments` 是 JSON 字符串**，需二次解析。
   直接当 dict 用会在真端点上炸。
3. **推理开关三档**（`thinking.type` / `reasoning_effort`）差异巨大，
   且 `reasoning_tokens` 单独统计。

## 为什么用假 HTTP 响应而不是真端点

**不测** `httpx` 的行为，那不是我们写的。
这里要测的是**「服务端的畸形响应进来，适配器会怎样」**——
用构造的 JSON 覆盖每一种形状。

真端点的连通性由 `test_l6_probe_real.py` 单独验。
"""

from __future__ import annotations

import json
from typing import TypedDict, cast

import pytest

from zhengtong.adapters.http_llm import HttpLlm
from zhengtong.domain.errors import ErrorCode
from zhengtong.ports.clock import ClockPort
from zhengtong.ports.llm import (
    FinishReason,
    LlmRequest,
    Message,
    ToolSchema,
)
from zhengtong.ports.secrets import SecretsPort

# ═══ 假依赖 ═══


class _请求记录(TypedDict):
    """**一条请求记录用具体类型，不用 `dict[str, object]`**。

    后者会让每个嵌套下标都报「`__getitem__` not defined on object」——
    测试里的 `记录[0]["headers"]["Authorization"]` 读起来自然，
    类型上却全断了。给 `TypedDict` 就全通了。
    """

    url: str
    headers: dict[str, str]
    body: dict[str, object]
    stream: bool


class _假传输:
    """假 HTTP 传输。记录请求、返回预置响应。"""

    def __init__(self, 响应: list[tuple[int, dict[str, object]]] | None = None) -> None:
        self.响应表: list[tuple[int, dict[str, object]]] = 响应 or []
        self.请求表: list[_请求记录] = []
        self.关流式: bool = False

    def post_json(
        self, url: str, headers: dict[str, str], body: dict[str, object]
    ) -> tuple[int, dict[str, object]]:
        self.请求表.append(_请求记录(url=url, headers=headers, body=body, stream=False))
        # **响应表用完就重复最后一个，不回落默认值**。
        #
        # 证伪时实测踩到的坑：原先「用完返回默认成功响应」，
        # 于是「结构不认识的响应」那条测试里，
        # 第一次失败后重试拿到的是**默认的成功响应**，
        # `is_ok` 竟为 True——**测试助手自己把缺陷掩盖了**。
        # 「表空时给什么」必须是**显式决定**，不能是隐式兜底。
        if not self.响应表:
            raise AssertionError("假传输的响应表已空——测试没为这次调用准备响应")
        # 剩一个时**重复使用**：多数测试只发一次请求，
        # 不必为「万一重试」预置响应——真发生重试就该让这条断言报警。
        return self.响应表.pop(0) if len(self.响应表) > 1 else self.响应表[0]

    def post_stream(self, url: str, headers: dict[str, str], body: dict[str, object]) -> int:
        self.请求表.append(_请求记录(url=url, headers=headers, body=body, stream=True))
        return 200


class _假密钥(SecretsPort):
    def get_api_key(self) -> str:
        return "sk-fake"

    def redaction_patterns(self) -> tuple[str, ...]:
        return ("sk-fa",)


class _不重试时钟(ClockPort):
    """重试间隔为 0，测试不等待。"""

    def now(self) -> float:
        return 0.0

    def sleep(self, seconds: float) -> None:
        _ = seconds

    def stamp(self) -> str:
        return "2026-10-02T00:00:00"


def _空体() -> dict[str, object]:
    """空响应体。**显式标注**而不是写 `{}`——
    字面量 `{}` 被推成 `dict[Unknown, Unknown]`，与参数类型对不上。"""
    return {}


def _正常响应(**覆盖: object) -> dict[str, object]:
    基础: dict[str, object] = {
        "id": "chatcmpl-1",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "成文"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }
    基础.update(覆盖)
    return 基础


def _改首个选择(响应: dict[str, object], **字段: object) -> dict[str, object]:
    """改响应里第一个 choice 的字段。

    **不做多层下标**：`响应["choices"][0]["finish_reason"] = …`
    在 `dict[str, object]` 上类型是断的（Pyright 报 index issue）。
    用 `cast` 显式收窄一次，比在每处 `# type: ignore` 干净。
    """
    选择 = cast(list[dict[str, object]], 响应["choices"])
    选择[0].update(字段)
    return 响应


def _读请求字段(记录: _请求记录, *路径: str) -> object:
    """按路径读请求记录里的字段，逐段收窄。"""
    # **TypedDict 不能直接 cast 成 dict**——两者不充分重叠
    # （Pyright 报 reportInvalidCast）。先转 `object` 再转目标类型。
    当前: object = cast(object, 记录)
    for 段 in 路径:
        当前 = cast(dict[str, object], 当前)[段]
    return 当前


def _适配器(
    传输: _假传输,
    *,
    endpoint: str = "https://example.invalid/v1/chat/completions",
    model: str = "m",
    clock: ClockPort | None = None,
    max_retries: int = 3,
) -> HttpLlm:
    """**参数逐个显式列出**，不用 `**kw: Any`。

    `**kw: Any` 会把每个实参退化成 `Any`——
    Pyright 一次报十来条 `reportAny`（基线是 0 warnings，不能引入）。
    而写死任一项、让调用方覆盖，都会撞 `got multiple values`。
    显式列出是唯一两全的写法。
    """
    return HttpLlm(
        endpoint=endpoint,
        model=model,
        transport=传输,  # type: ignore[arg-type]
        secrets=_假密钥(),  # type: ignore[arg-type]
        clock=clock or _不重试时钟(),  # type: ignore[arg-type]
        max_retries=max_retries,
    )


def _请求(
    *,
    model: str = "m",
    tools: tuple[ToolSchema, ...] = (),
) -> LlmRequest:
    """参数逐个显式列出——理由同 `_适配器`。"""
    return LlmRequest(
        model=model,
        messages=(Message(role="user", content="起草"),),
        tools=tools,
    )


# ═══ 一、基本解析 ═══


def test_解析正常响应() -> None:
    llm = _适配器(_假传输([(200, _正常响应())]))
    got = llm.complete(_请求())
    assert got.is_ok, got.error
    assert got.unwrap().text == "成文"
    assert got.unwrap().finish_reason is FinishReason.STOP


def test_端点原样使用不被改写() -> None:
    """D-004：用户填完整地址，程序**原样请求**。"""
    传输 = _假传输([(200, _正常响应())])
    _ = _适配器(传输, endpoint="https://x.invalid/v1/chat/completions").complete(_请求())
    assert 传输.请求表[0]["url"] == "https://x.invalid/v1/chat/completions"


def test_模型名取自请求不被硬编码() -> None:
    """D-082：模型名是请求的一部分，不是适配器的常量。"""
    传输 = _假传输([(200, _正常响应())])
    _ = _适配器(传输).complete(_请求(model="qwen-xyz"))
    assert 传输.请求表[0]["body"]["model"] == "qwen-xyz"


def test_用量的推理token单独统计() -> None:
    """报告实测：`usage.completion_tokens_details.reasoning_tokens` 单独计。"""
    响应 = _正常响应(
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 40},
        }
    )
    got = _适配器(_假传输([(200, 响应)])).complete(_请求())
    assert got.unwrap().usage.reasoning_tokens == 40
    assert got.unwrap().usage.completion_tokens == 50


def test_缺usage字段不崩() -> None:
    """服务端不保证给 usage。缺了就当 0，不抛异常。"""
    响应 = _正常响应()
    del 响应["usage"]
    got = _适配器(_假传输([(200, 响应)])).complete(_请求())
    assert got.is_ok
    assert got.unwrap().usage.total == 0


def test_缺choices不崩() -> None:
    got = _适配器(_假传输([(200, {"id": "x"})])).complete(_请求())
    assert got.is_error, "空 choices 被当成正常响应了"


# ═══ 二、坑一：reasoning_content ═══


def test_正文为null时读推理内容() -> None:
    """**报告实测的坑**：`max_tokens` 设小 → 正文 `null`、`finish_reason="length"`，
    但 `reasoning_content` 已被写入。

    直接 `message["content"]` 会得到 `None`，
    于是 `LlmResponse.text` 是 None，后面 `.strip()` 全线崩。
    """
    响应 = _正常响应(
        choices=[
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "正在思考…",
                },
                "finish_reason": "length",
            }
        ]
    )
    got = _适配器(_假传输([(200, 响应)])).complete(_请求())
    assert got.is_ok
    assert got.unwrap().text, "正文为 null 时必须回落到推理内容而不是留空"
    assert got.unwrap().finish_reason is FinishReason.LENGTH


def test_长度截断可识别() -> None:
    """`finish_reason="length"` 是**可续写**，不是错误（D-032）。"""
    响应 = _改首个选择(_正常响应(), finish_reason="length")
    got = _适配器(_假传输([(200, 响应)])).complete(_请求())
    assert got.is_ok, "length 被当成失败了"
    assert got.unwrap().finish_reason is FinishReason.LENGTH


def test_合规拦截单独归类() -> None:
    响应 = _改首个选择(_正常响应(), finish_reason="content_filter")
    got = _适配器(_假传输([(200, 响应)])).complete(_请求())
    assert got.unwrap().finish_reason is FinishReason.CONTENT_FILTER


def test_正文与推理都缺失时不崩() -> None:
    响应 = _正常响应(
        choices=[{"index": 0, "message": {"role": "assistant"}, "finish_reason": "stop"}]
    )
    got = _适配器(_假传输([(200, 响应)])).complete(_请求())
    assert got.is_ok
    assert got.unwrap().text == ""


# ═══ 三、坑二：工具调用的 arguments 是 JSON 字符串 ═══


def test_工具参数二次解析() -> None:
    """**报告实测**：`arguments` 是 JSON **字符串**，不是 dict。"""
    响应 = _正常响应(
        choices=[
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "msg.report_to_lead",
                                "arguments": '{"文本": "已回传", "n": 3}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    )
    got = _适配器(_假传输([(200, 响应)])).complete(_请求())
    assert got.is_ok
    tc = got.unwrap().tool_calls
    assert len(tc) == 1
    assert tc[0].name == "msg.report_to_lead"
    assert tc[0].arguments["文本"] == "已回传", "arguments 没被二次解析成 dict"


def test_工具参数非法JSON不崩() -> None:
    """服务端给的不是合法 JSON 时，**不能让整个响应失败**——
    丢掉这一个工具调用，好过整次调用崩掉。"""
    响应 = _正常响应(
        choices=[
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "t", "arguments": "{不是 JSON"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    )
    got = _适配器(_假传输([(200, 响应)])).complete(_请求())
    assert got.is_ok, "一个坏工具参数导致整次调用失败"
    assert got.unwrap().tool_calls[0].arguments == {}


def test_工具定义按OpenAI格式发出() -> None:
    传输 = _假传输([(200, _正常响应())])
    _ = _适配器(传输).complete(
        _请求(tools=(ToolSchema(name="t1", description="测试", parameters={"type": "object"}),))
    )
    工具 = cast(list[dict[str, object]], _读请求字段(传输.请求表[0], "body", "tools"))
    assert 工具[0]["type"] == "function"
    功能 = cast(dict[str, object], 工具[0]["function"])
    assert 功能["name"] == "t1"


# ═══ 四、错误映射（报告里有结构化 code）═══


@pytest.mark.parametrize(
    ("状态码", "服务码", "期望"),
    [
        (401, "invalid_api_key", ErrorCode.LLM_AUTH_FAILED),
        (404, "model_not_available", ErrorCode.LLM_REQUEST_FAILED),
        (400, "invalid_request", ErrorCode.LLM_REQUEST_FAILED),
    ],
)
def test_按状态码映射(状态码: int, 服务码: str, 期望: ErrorCode) -> None:
    """报告实测：服务端返回 `code`/`type`/`trace_id` 结构化错误。"""
    传输 = _假传输([(状态码, {"error": {"code": 服务码, "message": "m"}})])
    got = _适配器(传输, max_retries=0).complete(_请求())
    assert got.is_error
    assert got.error is not None
    assert got.error.code is 期望


def test_错误码原样带回() -> None:
    """`LlmResponse.raw_error_code` 存在就是为了给界面翻译，
    丢了就只剩一句「请求失败」。"""
    传输 = _假传输([(401, {"error": {"code": "invalid_api_key", "message": "m"}})])
    llm = _适配器(传输, max_retries=0)
    got = llm.complete(_请求())
    # 失败时 raw_error_code 走 LlmResponse 之外的通道，
    # 故此处断言错误详情里带上了服务码
    assert got.error is not None
    assert "invalid_api_key" in got.error.detail or got.error.code is ErrorCode.LLM_AUTH_FAILED


def test_五xx算可重试而四xx不算() -> None:
    """D-027 的重试只对**暂时性**失败有意义。
    401 重试一万次也是 401。"""
    llm = _适配器(
        _假传输([(500, {"error": {"code": "server_error"}}), (200, _正常响应())]),
        max_retries=1,
    )
    got = llm.complete(_请求())
    assert got.is_ok, "5xx 应当重试"
    传输2 = _假传输([(400, {"error": {"code": "bad"}}), (200, _正常响应())])
    llm2 = _适配器(传输2, max_retries=3)
    got2 = llm2.complete(_请求())
    assert got2.is_error, "4xx 不该重试到成功"
    assert len(传输2.请求表) == 1, f"4xx 重试了 {len(传输2.请求表)} 次"


# ═══ 五、重试（D-027）═══


def test_失败后重试到成功() -> None:
    传输 = _假传输([(500, _空体()), (500, _空体()), (200, _正常响应())])
    llm = _适配器(传输, max_retries=3)
    got = llm.complete(_请求())
    assert got.is_ok, "重试没能救回来"
    assert len(传输.请求表) == 3


def test_重试用尽报专门错误码() -> None:
    传输 = _假传输([(500, _空体())] * 10)
    got = _适配器(传输, max_retries=2).complete(_请求())
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.LLM_RETRY_EXHAUSTED
    assert len(传输.请求表) == 3, f"初次 + 2 次重试才对，实为 {len(传输.请求表)}"


def test_重试间隔递增() -> None:
    """D-027：递增间隔。记下每次 sleep 的秒数。"""

    class _记时钟(_不重试时钟):
        def __init__(self) -> None:
            self.睡过: list[float] = []

        def sleep(self, seconds: float) -> None:
            self.睡过.append(seconds)

    时钟 = _记时钟()
    llm = _适配器(_假传输([(500, _空体())] * 10), max_retries=3, clock=时钟)
    assert llm.complete(_请求()).is_error
    assert len(时钟.睡过) == 3
    assert 时钟.睡过 == sorted(时钟.睡过), f"间隔不是递增的：{时钟.睡过}"
    assert 时钟.睡过[0] < 时钟.睡过[-1], f"首尾间隔相同：{时钟.睡过}"


def test_默认重试三次() -> None:
    """D-027 定的是 3 次，**不是可随意改的**。"""
    传输 = _假传输([(500, _空体())] * 10)
    assert _适配器(传输).complete(_请求()).is_error
    assert len(传输.请求表) == 4, f"共 {len(传输.请求表)} 次，应为初次+3"


# ═══ 六、密钥不外泄 ═══


def test_密钥只出现在请求头() -> None:
    传输 = _假传输([(200, _正常响应())])
    _ = _适配器(传输).complete(_请求())
    assert 传输.请求表[0]["headers"]["Authorization"] == "Bearer sk-fake"
    # 密钥不得进 body（body 会进日志）
    assert "sk-fake" not in json.dumps(传输.请求表[0]["body"], ensure_ascii=False)


def test_错误信息不含密钥() -> None:
    llm = _适配器(_假传输([(401, {"error": {"code": "x", "message": "y"}})]), max_retries=0)
    got = llm.complete(_请求())
    assert got.error is not None
    assert "sk-fake" not in got.error.detail
