"""HTTP 传输层（D-246）。

## 为什么这处的覆盖率必须补

它原先只有 64%，是全仓最低。原因很直白：
**它要真网络才能测，于是大家都不测**——
而它恰好是第 17 步跨平台验证的唯一通路。

`httpx.MockTransport` 让「网络」变成可注入的假件，
于是这些分支可以在**零网络**下测——本文件全部用例不联网。

## 测的是两条真分支

| 分支 | 真实场景 |
|---|---|
| `httpx.HTTPError` → `(0, {})` | 连不上、超时、TLS 失败 |
| 响应不是 JSON → `(状态码, {})` | 网关的 HTML 错误页 |

"""

from __future__ import annotations

import json

import httpx
import pytest

from zhengtong.adapters.http_llm import Transport
from zhengtong.adapters.httpx_transport import HttpxTransport


def _装(
    处理,
) -> HttpxTransport:
    """用假传输层装一个 transport。**不联网**。"""
    return HttpxTransport(客户端=httpx.Client(transport=httpx.MockTransport(处理)))


def _回(状态: int = 200, 体: object = None, *, 文本: str | None = None):
    """构造一个响应处理器。"""
    载荷 = 文本 if 文本 is not None else json.dumps(体 if 体 is not None else {"ok": True})
    return httpx.Response(状态, text=载荷)


class Test网络异常:
    def test_连不上转失败对(self) -> None:
        """**不抛异常**——适配层抛异常会让上层 `Result` 通路失效（D-064）。"""

        def 挂(_请求: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("连不上")

        传 = _装(挂)
        状态, 体 = 传.post_json("https://x.invalid/v1", {}, {})
        assert 状态 == 0, f"状态码应为 0（表示没发出去），实际 {状态}"
        assert 体 == {}, f"失败时体应为空，实际 {体}"

    def test_超时转失败对(self) -> None:
        def 挂(_请求: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("读超时")

        传 = _装(挂)
        assert 传.post_json("https://x.invalid/v1", {}, {})[0] == 0

    def test_每次失败都是失败对不是异常(self) -> None:
        """**判据核心**：连着调三次，三次都是失败对。

        只调一次的话，「第一次抛了但被吞了」也测不出来——
        而那正是「看起来能跑、一到真网就崩」的典型。
        """

        def 挂(_请求: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("连不上")

        传 = _装(挂)
        for _ in range(3):
            assert 传.post_json("https://x.invalid/v1", {}, {}) == (0, {})


class Test非JSON响应:
    def test_html错误页不当成功(self) -> None:
        """**真实场景**：网关返回 HTML 错误页。

        若 `resp.json()` 抛异常而没接住，整个调用会崩；
        接住后返回 `(状态码, {})`，让上层按状态码判错。
        """
        传 = _装(lambda _r: _回(502, 文本="<html>502 Bad Gateway</html>"))
        状态, 体 = 传.post_json("https://x.invalid/v1", {}, {})
        assert 状态 == 502, f"状态码应原样传回，实际 {状态}"
        assert 体 == {}, f"非 JSON 应转空体，实际 {体}"

    def test_空body也是失败对(self) -> None:
        传 = _装(lambda _r: httpx.Response(200, text=""))
        状态, 体 = 传.post_json("https://x.invalid/v1", {}, {})
        assert (状态, 体) == (200, {})

    def test_真JSON原样返回(self) -> None:
        """**反向**：正常 JSON 不能被误吞。"""
        传 = _装(lambda _r: _回(200, {"choices": [{"text": "你好"}]}))
        状态, 体 = 传.post_json("https://x.invalid/v1", {}, {})
        assert 状态 == 200
        assert 体 == {"choices": [{"text": "你好"}]}, f"JSON 被改动了：{体}"


class TestURL原样使用:
    def test_url不被改写(self) -> None:
        """**D-004 硬要求**：用户填什么就请求什么，
        不 `urljoin`、不拼路径、不改 scheme。"""
        见过: list[str] = []

        def 记(请求: httpx.Request) -> httpx.Response:
            见过.append(str(请求.url))
            return _回(200, {})

        # **用纯 ASCII 域名**：用户填的是 `api.deepseek.com` 这类地址。
        # 中文字符域名会被 httpx 按 RFC 转成 punycode（`例子.invalid`
        # → `xn--fsqu00a.invalid`）——那是 DNS 协议要求的正确行为，
        # 不是「改写 URL」，见 D-246 的记录。
        _装(记).post_json("https://api.example.com:8443/a/b/c?x=1", {}, {})
        assert 见过, "没记录到请求"
        assert 见过[0] == "https://api.example.com:8443/a/b/c?x=1", f"URL 被改写了：{见过[0]}"

    def test_中文域名按RFC转punycode而非报错(self) -> None:
        """**本条把上条踩到的坑钉住**。

        用户若真填了中文域名，httpx 会转 punycode——
        这是 RFC 5890 的要求（DNS 只收 ASCII），**不是缺陷**。
        本条记下实际行为，免得将来有人以为是回归而去"修"它。
        """
        见过: list[str] = []

        def 记(请求: httpx.Request) -> httpx.Response:
            见过.append(str(请求.url))
            return _回(200, {})

        _装(记).post_json("https://例子.invalid/v1", {}, {})
        assert "xn--" in 见过[0], f"中文域名未按 RFC 转 punycode：{见过[0]}"

    def test_请求体原样发出(self) -> None:
        """D-004 同样适用于体：不增删字段。

        **例外**：`stream` 字段由传输层强制设为 `True`（始终走流式接收）。
        其他字段原样透传。
        """
        见过: list[dict[str, object]] = []

        def 记(请求: httpx.Request) -> httpx.Response:
            见过.append(json.loads(请求.content.decode("utf-8")))
            return _回(200, {})

        体: dict[str, object] = {"model": "m", "messages": [], "temperature": 0.2}
        _装(记).post_json("https://x.invalid/v1", {}, 体)
        # 传输层强制流式，会多一个 stream=True
        期望 = dict(体)
        期望["stream"] = True
        assert 见过[0] == 期望, f"请求体被改动了：{见过[0]}"


class Test流式已整体移除:
    """D-295：流式探测作废，`post_stream` 连同它在传输层的三条判据一并删除。

    原先这里有三条判据（读到首块、失败返回 0、带 stream 标记）。
    删它们不是「少测了」，是**测的东西不存在了**——
    留着一条必然报红的判据，等于用红灯掩盖真实状态。

    那条判据背后的真实场景（有些服务端先回 200 再在流里报错）
    在流式删除后**已不适用于本项目**：所有请求都走 `post_json`，
    httpx 会把流内错误一并归入 `HTTPError` → 状态码 0。
    """

    def test_传输层不再暴露流式方法(self) -> None:
        """判据看**能力确已消失**，不只是「没人调用」。"""
        assert not hasattr(HttpxTransport, "post_stream")

    def test_端口协议也不再要求流式(self) -> None:
        assert not hasattr(Transport, "post_stream")


class Test关闭:
    def test_close可重复调用(self) -> None:
        """重复关闭不应抛异常——退出路径上可能被调多次。"""
        传 = _装(lambda _r: _回(200, {}))
        传.close()
        传.close()


_ = pytest
