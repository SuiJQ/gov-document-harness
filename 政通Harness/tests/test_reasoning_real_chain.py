"""推理探测**经真实适配器**的行为（D-384）。

## 为什么这组判据不能省

`test_reasoning_probe.py` 用注入的假 `发` 函数测判定逻辑，
而真实链路上判定之前还隔着 `HttpLlm._用量` 解析响应体。

那一层早先把**缺失的整个 `usage`** 解析成 `TokenUsage()`（全 0）。
于是「后端不回报推理用量」在探测层看来就是「推理为 0」，
判定成 **「本模型不生成推理内容，无需关闭」**——
咨询/Flash 两档于是照常付推理的钱，而体检报告说「不用关」。

**这不是探测层的错**：判定逻辑是对的，错的是适配器把
「没有值」填成了「值为 0」。假 `发` 的测试**照不到这一层**，
所以必须有一组判据真的走过 `HttpLlm`。

## 「填 0」与「没有值」为什么必须分开

展示层两者等价（台账记 0，D-277），但**判定层含义相反**：

- 填 0 → 「确实没有推理」→ 无需关闭 → 不提示
- 没有值 → 「判不出来」→ 必须提示，否则用户以为没花钱

混为一谈的后果不是显示难看，是**用户花了钱而没人告诉他**。
"""

from __future__ import annotations

from test_http_llm import _假传输, _适配器
from zhengtong.adapters.reasoning_probe import 探测推理
from zhengtong.domain.reasoning import 推理状态


def _正文(usage: dict[str, object] | None = None) -> dict[str, object]:
    体: dict[str, object] = {
        "id": "x",
        "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
    }
    if usage is not None:
        体["usage"] = usage
    return 体


def _用量(推理: int) -> dict[str, object]:
    return {
        "prompt_tokens": 9,
        "completion_tokens": 2,
        "completion_tokens_details": {"reasoning_tokens": 推理},
    }


def _探测(响应们: list[dict[str, object]]):
    传输 = _假传输([(200, 体) for 体 in 响应们])
    return 探测推理("m", _适配器(传输).complete).unwrap()


def test_不报usage判为无法判定而非无需关闭() -> None:
    """**本组的核心**：缺 usage ≠ 推理为 0。

    修复前这里得到「无需关闭」，用户被告知「不用关推理」，
    而后端其实在推理、只是不说——咨询档照常计费。
    """
    got = _探测([_正文()])
    assert got.状态 is 推理状态.无法判定, f"判成 {got.状态}：{got.说明()}"
    assert "无法判定" in got.说明(), got.说明()


def test_报零推理判为无需关闭() -> None:
    """**有 usage 且 reasoning_tokens=0** 才是「确实不推理」。"""
    got = _探测([_正文(_用量(0))])
    assert got.状态 is 推理状态.无需关闭, f"判成 {got.状态}：{got.说明()}"


def test_强制开启且报usage判为关闭不了() -> None:
    """五种写法全被忽略（每种都返回 200，推理不降）→ 关不掉。"""
    got = _探测([_正文(_用量(80))] * 5)
    assert got.状态 is 推理状态.关闭不了, f"判成 {got.状态}：{got.说明()}"


def test_不报usage时不得给出可用写法() -> None:
    """判不出来就不能拿写法去关——
    否则会对着一个不接受该字段的后端发无意义请求。"""
    got = _探测([_正文()])
    assert got.可用写法 == 0
    assert not got.能关闭
