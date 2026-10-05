"""用户配置的容量必须真的流到压缩（D-231）。

## 这条欠账的形态

`App.编排器()` 第 143 行**读了** `self.config.context_k`，
却只拿它算装配预算（`容量 // 2`），**没有把容量传给编排器的 `容量token`**。

于是编排器一直用默认的 `128_000`。实测后果：

| 用户填 | 真实容量 | 装配预算 | 压缩按的容量 | 压缩触发线 |
|---|---|---|---|---|
| 8K | 8192 | 4096 | **128000** | **108800** |
| 128K | 131072 | 65536 | **128000** | **108800** |
| 200K | 204800 | 102400 | **128000** | **108800** |

**三种容量下压缩触发线完全相同**——填小了压缩永不触发（上下文早爆了它还在睡），
填大了提前太多（白丢本该保留的上下文）。

这是本项目第五次同型缺口（D-222 / D-223 / context 整体未接线 /
`是否真的变小了` 零引用 / 这次）。**共同特征：一个参数被读了却没往下传。**

## 判据只从外部观察

不断言源码里有没有 `容量token=`——那会被注释与措辞牵着走。
断言的是**压缩实际按哪个容量在触发**。
"""

from __future__ import annotations

from zhengtong.domain.errors import Result
from zhengtong.orchestration import 编排器
from zhengtong.ports.llm import LlmRequest, LlmResponse
from zhengtong.services.context import ContextView, 需要压缩
from zhengtong.services.context.estimate import 触发阈值


class _记容量的Llm:
    """记录收到的请求，用于回推编排器实际用的容量。"""

    def __init__(self) -> None:
        self.请求: list[LlmRequest] = []

    def complete(self, request: LlmRequest) -> Result[LlmResponse]:
        self.请求.append(request)
        return Result.ok(
            LlmResponse(text="产出", tool_calls=(), finish_reason="stop", usage=None)  # pyright: ignore[reportArgumentType]
        )


def _装(k: int) -> tuple[编排器, _记容量的Llm]:
    """按用户配置的容量 k（K）装一个编排器。"""
    from zhengtong.adapters.fakes import FakeContentPack, FakeStorage
    from zhengtong.domain.run_state import GateRecord
    from zhengtong.orchestration import 编排器

    llm = _记容量的Llm()
    编: 编排器 = 编排器(
        llm=llm,  # pyright: ignore[reportArgumentType]
        storage=FakeStorage(),
        content_pack=FakeContentPack(),  # pyright: ignore[reportArgumentType]
        模型名="m",
        容量token=k * 1024,  # 模拟 App.编排器() 的传参
        门禁取证=lambda 请求: {g.value: GateRecord(gate=g.value, passed=True) for g in 请求.门禁},
    )
    return 编, llm


class Test容量流到压缩:
    def test_编排器容量取自用户配置(self) -> None:
        """**核心判据**：`容量token` 必须等于用户配置的容量。

        判据不查源码有没有 `容量token=`，只查值——
        那才是不受措辞影响的东西。
        """
        for k in (8, 128, 200):
            编, _ = _装(k)
            assert 编.容量token == k * 1024, f"用户填 {k}K，编排器用的却是 {编.容量token} token"

    def test_不同容量得到不同触发线(self) -> None:
        """8K 与 200K 的压缩触发线必须不同——相同就说明容量没传下去。"""
        小, _ = _装(8)
        大, _ = _装(200)
        assert 触发阈值(小.容量token) != 触发阈值(大.容量token), (
            f"{小.容量token} 与 {大.容量token} 的触发线相同——容量没传下去"
        )

    def test_小容量不会被大默认值掩盖(self) -> None:
        """用户填 8K 时，触发线必须低于 8192——不能是默认 128K 的 85%。"""
        编, _ = _装(8)
        assert 触发阈值(编.容量token) < 8192 * 0.9, (
            f"8K 容量却按 {编.容量token} 算触发线——压缩永不触发"
        )


class Test装配根真的传了:
    def test_走装配根容量随配置变(self) -> None:
        """**这条才是真判据**：走 `build_app` → `App.编排器()` 全程，
        确认用户配置的容量真的流到了压缩。

        上面几条只证明「传了参数就生效」，证明不了**装配根传了**——
        而这次的缺陷恰恰出在装配根（读了容量却没用）。
        """
        from zhengtong.adapters.config import Config
        from zhengtong.app import build_app

        触发线 = []
        for k in (8, 200):
            应用 = build_app(config=Config(context_k=k))
            编 = 应用.编排器()
            触发线.append(触发阈值(编.容量token))
        assert 触发线[0] < 触发线[1], f"8K 与 200K 经装配根得到相同触发线 {触发线}——容量没往下传"

    def test_装配根的值等于配置换算(self) -> None:
        from zhengtong.adapters.config import Config
        from zhengtong.app import build_app

        编 = build_app(config=Config(context_k=64)).编排器()
        assert 编.容量token == 64 * 1024, f"配置 64K，装配根给出 {编.容量token}"


class Test压缩阈值按容量算:
    def test_触发判定用的是真实容量(self) -> None:
        """一份超 8K 上下文的视图，在 8K 档必须判定「需压缩」。"""
        长 = tuple(
            __import__("zhengtong.ports.llm", fromlist=["Message"]).Message(
                role="user", content="内容" * 4000
            )
            for _ in range(20)
        )
        视图 = ContextView(recent_window=长)
        assert 需要压缩(视图, 上下文容量=8192), "超容量却判定不需要压缩"
        assert not 需要压缩(视图, 上下文容量=204800), "远未超容量却判定需要压缩"
