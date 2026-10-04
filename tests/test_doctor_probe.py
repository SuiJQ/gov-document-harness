"""doctor 五项真探测（D-295）。

## 这批判据为什么现在才写

`探测五项` 从第 6 步就写好了，`探测推理` 也有独立判据，
但 **`probe()` 全仓零调用方**——它们一行没跑过。

零调用的实现是最危险的一种「已完成」：
它在提交信息里、在 docstring 里、在函数名里都显得完备，
**只有真正接上电线才知道对不对**。

## 判据盯三件事

1. **逐项，不笼统**——中途失败后续项标「未测」，不谎报未通过
2. **已确认的项不倒退**——工具探测失败时不能把网络与鉴权说成未通过
3. **真假通过分开**——`全通过` 不能把「压根没跑」算成过

## 为什么用假实现而不是真端点

真端点在 `tests/test_real_endpoint.py` 测。
这里要穷举各种失败形态（401、空正文、工具失败、连不上），
真端点造不出这些情形，逐个搭服务器既慢又不稳。
"""

from __future__ import annotations

from collections.abc import Callable

from zhengtong.adapters.llm_probe import 探测五项
from zhengtong.domain.errors import ErrorCode, Result
from zhengtong.ports.llm import FinishReason, LlmRequest, LlmResponse, TokenUsage, 探测结论
from zhengtong.presentation.doctor import 自检结果, 自检项


def _响应(正文: str = "pong") -> LlmResponse:
    return LlmResponse(
        text=正文,
        finish_reason=FinishReason.STOP,
        tool_calls=(),
        usage=TokenUsage(),
    )


class 假可探测:
    """按脚本回应的假端点。**记下收到几次请求**，供判据数往返。"""

    def __init__(self, 回: Callable[[LlmRequest, int], Result[LlmResponse]]) -> None:
        self._回 = 回
        self.次数 = 0

    @property
    def model(self) -> str:
        return "假模型"

    def complete(self, request: LlmRequest) -> Result[LlmResponse]:
        第 = self.次数
        self.次数 += 1
        return self._回(request, 第)


def _全过(_r: LlmRequest, _i: int) -> Result[LlmResponse]:
    return Result.ok(_响应())


def _探(假: 假可探测) -> 探测结论:
    got = 探测五项(假)
    assert got.is_ok, "探测本身不该失败"
    结论 = got.unwrap()
    assert 结论 is not None
    return 结论


# ═══ 一、逐项，不笼统 ═══


class Test逐项结论:
    def test_五项各自都有结论(self) -> None:
        结论 = _探(假可探测(_全过))
        assert 结论.取("网络连通") is True
        assert 结论.取("鉴权") is True
        assert 结论.取("模型调用") is True
        assert 结论.取("工具调用") is True
        assert 结论.取("推理开关") is True
        assert 结论.未测 == (), "全过时不该有未测项"

    def test_每项都有详情可看(self) -> None:
        """详情为空用户就只能看到「通过」两个字，不知道测了什么。"""
        结论 = _探(假可探测(_全过))
        for 名 in 探测结论.项序:
            assert 结论.详情.get(名), f"「{名}」没有详情"


class Test中途失败后续标未测:
    def test_连不上则鉴权未测(self) -> None:
        """**没送达就没有鉴权可言**——报「鉴权未通过」是撒谎。"""
        结论 = _探(
            假可探测(
                lambda _r, _i: Result.fail(
                    ErrorCode.LLM_REQUEST_FAILED,
                    "连不上端点：请求没能送达服务器（地址不通、网络不可达、或服务未启动）。",
                )
            )
        )
        assert 结论.取("网络连通") is False
        assert 结论.取("鉴权") is None, "没送达却判了鉴权"
        assert 结论.取("模型调用") is None

    def test_连不上则后两项未测(self) -> None:
        结论 = _探(
            假可探测(
                lambda _r, _i: Result.fail(
                    ErrorCode.LLM_REQUEST_FAILED,
                    "连不上端点：请求没能送达服务器",
                )
            )
        )
        assert 结论.取("工具调用") is None
        assert 结论.取("推理开关") is None

    def test_鉴权失败不牵连网络连通(self) -> None:
        """**已确认过的项不倒退**——否则用户会去查错方向。"""
        结论 = _探(
            假可探测(lambda _r, _i: Result.fail(ErrorCode.LLM_AUTH_FAILED, "鉴权被拒（401）"))
        )
        assert 结论.取("网络连通") is True, "收到 401 说明网络是通的"
        assert 结论.取("鉴权") is False
        assert 结论.取("工具调用") is None

    def test_未测项不再发请求(self) -> None:
        """鉴权没过还硬发工具请求，只会再得到同一个 401，
        多花一次往返还多一条误导性错误。"""
        假 = 假可探测(lambda _r, _i: Result.fail(ErrorCode.LLM_AUTH_FAILED, "鉴权被拒（401）"))
        _探(假)
        assert 假.次数 == 1, f"鉴权失败后又发了 {假.次数 - 1} 次请求"

    def test_空正文时后两项未测(self) -> None:
        结论 = _探(假可探测(lambda _r, _i: Result.ok(_响应(""))))
        assert 结论.取("模型调用") is False
        assert 结论.取("工具调用") is None


class Test工具探测:
    def test_工具失败不影响前四项(self) -> None:
        def 回(r: LlmRequest, i: int) -> Result[LlmResponse]:
            if i == 0:
                return Result.ok(_响应())
            return Result.fail(ErrorCode.LLM_REQUEST_FAILED, "工具调用探测失败：不支持")

        结论 = _探(假可探测(回))
        assert 结论.取("网络连通") is True
        assert 结论.取("鉴权") is True
        assert 结论.取("模型调用") is True
        assert 结论.取("工具调用") is False
        assert "不支持" in 结论.详情["工具调用"]

    def test_关不掉推理不算失败(self) -> None:
        """**「关不掉」是探测成功了**，不是失败——
        服务端认了参数但推理仍开着，这是有效结论。

        把它算成未通过会让体检整体挂掉，而机器其实是能写公文的。"""
        结论 = _探(
            假可探测(
                lambda r, i: (
                    Result.ok(_响应())
                    if not r.额外字段
                    else Result.fail(ErrorCode.LLM_REQUEST_FAILED, "此写法不生效")
                )
            )
        )
        assert 结论.取("推理开关") is True
        assert "关不掉" in 结论.详情["推理开关"], 结论.详情["推理开关"]

    def test_推理探测本身崩了才算未通过(self) -> None:
        """连基准请求都失败才是探测失败（不是「关不掉」）。"""

        def 回(r: LlmRequest, i: int) -> Result[LlmResponse]:
            if not r.额外字段:
                return Result.ok(_响应())
            return Result.fail(ErrorCode.LLM_AUTH_FAILED, "推理探测失败：鉴权被拒")

        结论 = _探(假可探测(回))
        # 基准请求能通、四种写法全部被拒 → 结论是「关不掉」，仍算探测成功
        assert 结论.取("推理开关") is True


# ═══ 二、显示层：三态不能混淆 ═══


class Test未测不等于未通过:
    def test_未测项不进失败清单(self) -> None:
        """未测项没有处置可写，混进处置清单只会多出无意义条目。"""
        from zhengtong.presentation.doctor import 跑自检

        结果 = 跑自检(
            探测=(
                自检项("网络连通", False, "连不上"),
                自检项("鉴权", False, "前项失败", 未测=True),
            )
        )
        assert [项.名称 for 项 in 结果.失败项] == ["网络连通"]
        assert [项.名称 for 项 in 结果.未测项] == ["鉴权"]

    def test_未测项使全通过为假(self) -> None:
        """**这条最关键**：未测项若不算失败，体检就会因为
        「有一项压根没跑」而返回退出码 0。"""
        from zhengtong.presentation.doctor import 跑自检

        结果 = 跑自检(探测=(自检项("鉴权", False, "前项失败", 未测=True),))
        assert not 结果.全通过, "未测项被算成了通过"

    def test_不能既通过又未测(self) -> None:
        """不变式由 `__post_init__` 兜底，不靠调用方自觉。"""
        import pytest

        with pytest.raises(ValueError, match="未测"):
            自检项("鉴权", True, "既通过又未测", 未测=True)

    def test_未测项报告里单独说明(self) -> None:
        from zhengtong.presentation.doctor import 跑自检

        报告 = 跑自检(
            探测=(
                自检项("网络连通", False, "连不上端点"),
                自检项("工具调用", False, "前项失败，未能跑到这一项", 未测=True),
            )
        ).报告()
        assert "未测" in 报告, 报告
        assert "1 项因前项失败未测" in 报告, 报告


class Test项序是唯一事实源:
    def test_五项顺序固定(self) -> None:
        """展示层与判据都按它遍历——**不许各写一份顺序**。"""
        assert 探测结论.项序 == ("网络连通", "鉴权", "模型调用", "工具调用", "推理开关")

    def test_自检结果按项序展开(self) -> None:
        from zhengtong.cli.main import _探测转自检项

        结论 = 探测结论(
            通过项=探测结论.项序,
            详情=dict.fromkeys(探测结论.项序, "好"),
        )
        项 = _探测转自检项(结论)
        assert tuple(x.名称 for x in 项) == 探测结论.项序
        assert all(x.通过 for x in 项)
        assert 自检结果(项=tuple(项)).全通过
