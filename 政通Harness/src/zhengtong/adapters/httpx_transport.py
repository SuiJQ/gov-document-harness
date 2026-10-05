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
from typing import final

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
        try:
            resp = self._客户端.post(url, headers=headers, json=body)
        except httpx.HTTPError:
            # **转成失败对而非抛出**：上层统一走 Result 通路（D-064）
            return 0, {}
        try:
            return resp.status_code, resp.json()
        except (ValueError, json.JSONDecodeError):
            # 服务端返回的不是 JSON——可能是网关的 HTML 错误页
            return resp.status_code, {}

    def close(self) -> None:
        self._客户端.close()
