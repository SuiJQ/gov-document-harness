"""token 用量进入输出（D-277，修 D-269）。

## 修的是什么

`MODEL_REQUEST` / `MODEL_RESPONSE` 两个 kind 在 `domain/event.py`
的枚举里躺了很久，`tests/test_projection.py` 与 `test_redactor.py`
都测了它们，**但生产代码一处都没发过**。

与 D-118（分支挂在白名单外因而永不执行）、D-237
（事件两端都在却没连线）完全同型——**东西齐了，就是没接上**。

后果：`TokenUsage` 三个字段用户路径零消费，
用户跑完一稿**永远不知道花了多少 token**。

## 为什么标准模式也显示（用户 2026-10-03 裁决）

> 「a，标准模式也显示 Token」

D-023/D-046 禁的是「模型间交互」——谁问了什么、谁答了什么。
而「这一稿花了多少 token」是**账单**，
架构 §686 明写「界面展示用量 → API 返回的真实值」。

**边界**：进白名单的是**一个 kind**，载荷里**只有数字**，
没有正文字段。看得见的是用量，不是内容。

## 为什么不再建 `system/telemetry.json`

D-031 修订已定该文件不建，同时明写「token 台账不受影响」。
用户裁决取**读法 A**：只发事件。事件本来就会落 journal
（`kernel/journal.py` 的 `_to_json` 带 `data`），事后可查；
而聚合台账要维护一套状态，对「跑一次看个数字」是净成本。

## 与 D-077 的分工

估算值只用于 85% 压缩触发的**提前示警**；
「界面展示用量」必须用 **API 返回的真实值**。
用户看的是实际消耗，不是我们的预测。
"""

from __future__ import annotations

import pytest

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.domain.event import EventKind, is_user_visible
from zhengtong.domain.ids import RunId
from zhengtong.domain.tier import TierId
from zhengtong.kernel.projection import StandardProjection
from zhengtong.orchestration import 编排器
from zhengtong.ports.llm import (
    FinishReason,
    LlmResponse,
    TokenUsage,
    ToolCall,
)


def _带用量的llm(用量: TokenUsage) -> FakeLlm:
    return FakeLlm(
        scripted=[
            LlmResponse(
                text=f"第{i}轮正文",
                finish_reason=FinishReason.STOP,
                usage=用量,
            )
            for i in range(6)
        ]
    )


def _编(llm: FakeLlm) -> 编排器:
    return 编排器(
        llm=llm,
        storage=FakeStorage(),
        模型名="m",
        门禁取证=lambda 门禁: {},
        运行号=RunId("r-token"),
    )


class Test用量真的发出去了:
    def test_编排会发model_response(self) -> None:
        """**这是本条最硬的判据**：跑一次编排，事件流里必须有
        `model.response`。

        白名单改了、投影加了分支，都不代表编排真的发了——
        那是 D-118 与 D-237 反复咬人的地方。
        """
        llm = _带用量的llm(TokenUsage(prompt_tokens=100, completion_tokens=50))
        编 = _编(llm)
        编.跑到终(档位=TierId.PRO, 需求="就旱情写个通知")

        响应事件 = [e for e in 编.事件 if e.kind is EventKind.MODEL_RESPONSE]
        assert 响应事件, "编排一次都没发 model.response——用量根本没进事件流"
        assert 响应事件[0].data["prompt_tokens"] == 100
        assert 响应事件[0].data["completion_tokens"] == 50

    def test_请求事件也发(self) -> None:
        """架构 §436 列了 `model.request`，成对发才看得出「问了几次」。"""
        llm = _带用量的llm(TokenUsage(prompt_tokens=10, completion_tokens=5))
        编 = _编(llm)
        编.跑到终(档位=TierId.PRO, 需求="就旱情写个通知")

        assert [e for e in 编.事件 if e.kind is EventKind.MODEL_REQUEST], "没发 model.request"

    def test_事件载荷里没有正文(self) -> None:
        """**只放数字，不放模型回复**（D-277 的边界）。

        `MODEL_RESPONSE` 进了白名单，若载荷带 `text`，
        那就是 D-046 禁止的「模型间交互」上了标准界面。
        """
        llm = _带用量的llm(TokenUsage(prompt_tokens=7, completion_tokens=3))
        编 = _编(llm)
        编.跑到终(档位=TierId.PRO, 需求="就旱情写个通知")

        for e in 编.事件:
            if e.kind is EventKind.MODEL_RESPONSE:
                assert "text" not in e.data, "事件载荷带了模型正文"
                assert "content" not in e.data, "事件载荷带了模型正文"

    def test_工具后续轮也记用量(self) -> None:
        """一轮里多次调模型，少记一次累计值就对不上——用户拿它对账。"""
        llm = FakeLlm(
            scripted=[
                LlmResponse(
                    text="",
                    finish_reason=FinishReason.TOOL_CALLS,
                    usage=TokenUsage(prompt_tokens=11, completion_tokens=1),
                    tool_calls=(ToolCall(id="c1", name="kb.search", arguments={"query": "x"}),),
                ),
                LlmResponse(
                    text="正文",
                    finish_reason=FinishReason.STOP,
                    usage=TokenUsage(prompt_tokens=22, completion_tokens=2),
                ),
            ]
        )
        编 = _编(llm)
        编.跑到终(档位=TierId.PRO, 需求="就旱情写个通知")

        响应事件 = [e for e in 编.事件 if e.kind is EventKind.MODEL_RESPONSE]
        入 = [_取数(e.data, "prompt_tokens") for e in 响应事件]
        assert 11 in 入, "首轮用量没记"
        assert 22 in 入, "工具后续轮用量没记"
        assert sum(入) >= 33


class Test标准模式显示用量:
    def test_累计并显示(self) -> None:
        p = StandardProjection()
        for _ in range(3):
            p.accept(_用量(800, 200))
        行 = "\n".join(p.lines())
        assert "token" in 行
        assert "3000" in 行, f"累计值不对：{行}"

    def test_输入输出分开列(self) -> None:
        p = StandardProjection()
        p.accept(_用量(800, 200))
        行 = "\n".join(p.lines())
        assert "800" in 行 and "200" in 行

    def test_不用量时不显示该行(self) -> None:
        """没有用量就不该有一行 0——那会让用户以为程序坏了。"""
        p = StandardProjection()
        assert not any("token" in 行 for 行 in p.lines())

    def test_用量不改写进度(self) -> None:
        """**那行是总体进度**（D-046），被每次调用覆写会一闪一闪跳回去。"""
        p = StandardProjection()
        p.accept(_成果("正文初稿", "全文"))
        前 = p.progress
        p.accept(_用量(100, 50))
        assert p.progress == 前, "token 用量覆写了总体进度"

    def test_空载荷也不崩(self) -> None:
        """白名单里每类都必须有活分支（`test_白名单内每类都有对应处理分支`
        的延续）——发射端漏字段不能带崩界面。"""
        p = StandardProjection()
        p.accept(_ev(EventKind.MODEL_RESPONSE, {}))
        assert isinstance(p.lines(), tuple)


class Test坏数据不崩:
    """投影层跑在 TUI 主循环里，一崩整个界面就没了。"""

    @pytest.mark.parametrize("坏值", ["不是数字", None, -5, True, 3.7, [], {"a": 1}])
    def test_畸形值被收敛(self, 坏值: object) -> None:
        p = StandardProjection()
        p.accept(_ev(EventKind.MODEL_RESPONSE, {"prompt_tokens": 坏值, "completion_tokens": 坏值}))
        assert isinstance(p.lines(), tuple), f"畸形值 {坏值!r} 带崩了投影"

    def test_负数显示为0不显示负号(self) -> None:
        p = StandardProjection()
        p.accept(_ev(EventKind.MODEL_RESPONSE, {"prompt_tokens": -9, "completion_tokens": 0}))
        assert not any("-" in 行 and "token" in 行 for 行 in p.lines())


class Test可见性边界:
    def test_model_response进白名单(self) -> None:
        assert is_user_visible(EventKind.MODEL_RESPONSE)

    @pytest.mark.parametrize(
        "kind", [EventKind.MODEL_REQUEST, EventKind.MODEL_ERROR, EventKind.MODEL_RETRY]
    )
    def test_其余三个仍不可见(self, kind: EventKind) -> None:
        """只有用量这一个 kind 进白名单，其余仍是「模型间交互」。"""
        assert not is_user_visible(kind)


# ── 小工具 ──


def _ev(kind: EventKind, data: dict[str, object]):
    """造一条事件。**只有一个入口**——先前这里写了两份 `_ev`，
    后者覆盖前者，导致按 kind 传参的调用全部炸掉。"""
    from zhengtong.domain.event import Event, Level

    return Event(
        seq=1,
        kind=kind,
        run_id=RunId("r-1"),
        phase="DRAFTING",
        level=Level.INFO,
        data=data,
    )


def _用量(入: int, 出: int):
    return _ev(
        EventKind.MODEL_RESPONSE,
        {"prompt_tokens": 入, "completion_tokens": 出},
    )


def _成果(name: str, content: str):
    return _ev(EventKind.ARTIFACT_READY, {"name": name, "content": content})


def _取数(data: dict[str, object], 键: str) -> int:
    """从事件 `data` 取整数。**显式收窄，不写 `# type: ignore`**（D-018）。

    `data` 的类型是 `dict[str, object]`，直接 `int(...)` 过不了类型检查。
    取不到就返回 0——判据要的是「记没记」，缺字段本身就该让断言失败，
    而不是让测试崩在类型转换上。
    """
    值 = data.get(键)
    return 值 if isinstance(值, int) and not isinstance(值, bool) else 0


def test_展示的是真实值不是估算() -> None:
    """展示的必须是 API 返回的真实值，不是估算（D-077）。

    这里锁的是**形状**：`prompt_tokens` / `completion_tokens` 两个键
    是 `TokenUsage` 的真实字段名；估算值在 `estimate.py` 里另有其名。
    """
    assert TokenUsage(prompt_tokens=1, completion_tokens=1).total == 2
    assert not hasattr(TokenUsage(), "估算"), "usage 上不该挂着估算字段"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
