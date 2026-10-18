"""HTTP 传输层（`adapters/httpx_transport.py`）。

## 为什么单独一层

`http_llm.py` 只管「请求体长什么样、响应怎么解析」，
**不碰网络**。真发包在这里。分层的理由很实际：
测试要构造畸形响应（`choices` 缺失、`arguments` 不是 JSON），
真端点没法稳定造出这些。

## 依赖

`httpx`——项目运行时依赖里已有（`pyproject.toml`）。
本项目是**OpenAI 风格接口的通用客户端**（D-004），
不引入任何厂商 SDK：那会让「兼容 OpenAI 风格」变成「兼容某个 SDK」。

## URL 原样使用（D-004）

`post_json` 把 `url` **原样**交给 `httpx`。
不 `urljoin`、不拼路径、不改 scheme——
用户填什么就请求什么，这是硬要求。

## 错误不抛给上层

网络异常在这里被转成 `(0, {})` 这样的**失败对**，
由 `http_llm` 统一映射为 `Result`。适配层不抛异常（D-064）。
"""

from __future__ import annotations

import json
from typing import Any, final

import httpx

#: 连接与读取超时（秒）。
#: **都给上限**——D-031 允许档位高者自选较长思考时间，
#: 但那是「慢」，不是「挂死」。没有上限时一个卡住的连接会永远占着。
_连接超时秒 = 10.0
_读取超时秒 = 300.0


@final
class HttpxTransport:
    """`Transport` 协议的真实现。"""

    def __init__(self, *, 客户端: httpx.Client | None = None) -> None:
        self._客户端 = 客户端 or httpx.Client(
            timeout=httpx.Timeout(
                connect=_连接超时秒, read=_读取超时秒, write=_读取超时秒, pool=_连接超时秒
            )
        )

    def post_json(
        self, url: str, headers: dict[str, str], body: dict[str, object]
    ) -> tuple[int, dict[str, object]]:
        """POST 一个 JSON，返回 `(状态码, 解析后的 JSON)`。

        ## 流式兜底（2026-10-18 实测发现）

        `platform.agentscope.io` 网关有个行为：**非流式响应里
        `message.tool_calls` 被剥离了**，但 `finish_reason` 仍写
        `"tool_calls"`。直接用非流式发工具请求会拿到
        `content=""` + `finish_reason="tool_calls"` + 没有
        `tool_calls` 数组——编排器看到 `finish_reason=TOOL_CALLS`
        以为模型要调工具，去 `message.tool_calls` 里找却找不到。

        流式响应的 `delta.tool_calls` **有**完整数据（分片传）。

        故本方法改为：**始终用流式接收，积攒成非流式响应再返回**。
        上层 `http_llm` 完全不感知——它拿到的还是 `(状态码, JSON)`。

        这不是「为了兼容一个奇怪的网关而改协议」，
        而是 OpenAI 兼容接口本来就有流式/非流式两种模式，
        选哪种是客户端的自由（D-004 只约束 URL 原样使用）。
        """
        # 强制走流式
        stream_body = dict(body)
        stream_body["stream"] = True
        # 用 text/event-stream 让上游明确知道我们要流式
        stream_headers = dict(headers)
        stream_headers["Accept"] = "text/event-stream"

        try:
            with self._客户端.stream("POST", url, headers=stream_headers, json=stream_body) as resp:
                if resp.status_code != 200:
                    # 非 200 时流式响应体可能不含 SSE，直接读 JSON
                    try:
                        return resp.status_code, json.loads(resp.read().decode("utf-8"))
                    except (ValueError, json.JSONDecodeError):
                        return resp.status_code, {}

                # 先把所有行读出来，判断是不是 SSE 格式
                lines: list[str] = list(resp.iter_lines())

                # 检查是否包含 SSE data: 行
                sse_lines = [行 for 行 in lines if 行.startswith("data:")]

                if not sse_lines:
                    # 不是 SSE——可能是非流式网关返回的普通 JSON
                    # （如 mock 测试里的空 body 或 HTML 错误页）
                    # 走旧逻辑：尝试解析 JSON，失败返回 {}
                    raw = "\n".join(lines)
                    try:
                        return resp.status_code, json.loads(raw)
                    except (ValueError, json.JSONDecodeError):
                        return resp.status_code, {}

                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                tool_calls: dict[int, dict[str, Any]] = {}
                finish_reason: str | None = None
                usage: dict[str, Any] | None = None
                model_name = ""
                chat_id = ""

                for line in sse_lines:
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except Exception:
                        continue

                    if not chat_id:
                        chat_id = chunk.get("id", "")
                    if chunk.get("model"):
                        model_name = chunk["model"]

                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        # 可能是纯 usage chunk
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        continue

                    ch = choices[0]
                    delta = ch.get("delta", {})
                    fr = ch.get("finish_reason")
                    if fr:
                        finish_reason = fr

                    c = delta.get("content")
                    if c:
                        content_parts.append(c)

                    rc = delta.get("reasoning_content")
                    if rc:
                        reasoning_parts.append(rc)

                    tc_list = delta.get("tool_calls")
                    if tc_list:
                        for tc in tc_list:
                            idx = tc.get("index", 0)
                            if idx not in tool_calls:
                                tool_calls[idx] = {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            if tc.get("id"):
                                tool_calls[idx]["id"] = tc["id"]
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                tool_calls[idx]["function"]["name"] = fn["name"]
                            if fn.get("arguments"):
                                tool_calls[idx]["function"]["arguments"] += fn["arguments"]

                    if chunk.get("usage"):
                        usage = chunk["usage"]

                # 组装非流式响应
                message: dict[str, object] = {
                    "role": "assistant",
                    "content": "".join(content_parts),
                }
                if reasoning_parts:
                    message["reasoning_content"] = "".join(reasoning_parts)
                if tool_calls:
                    message["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        }
                        for tc in sorted(tool_calls.values(), key=lambda x: x.get("index", 0))
                    ]

                result: dict[str, object] = {
                    "id": chat_id,
                    "object": "chat.completion",
                    "created": 0,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": finish_reason or "stop",
                        }
                    ],
                }
                if usage:
                    result["usage"] = usage

                return resp.status_code, result

        except httpx.HTTPError:
            # **转成失败对而非抛出**：上层统一走 Result 通路（D-064）
            return 0, {}

    def close(self) -> None:
        self._客户端.close()
