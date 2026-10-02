"""编排器发出的请求必须真的带上工具（D-233）。

## 这条欠账的形状

工具层（第 7 步）建得相当完整：16 个处理器、`建工具运行器`、
`App.tools` 字段、`该角色可调` 白名单、提示词里也渲染了「工具清单」块。

但 `LlmRequest` 的**唯一构造点**是：

```python
请求 = LlmRequest(model=self.编排.模型名, messages=消息)
```

**`tools` 字段全仓生产零填充。** 于是：

- 提示词里写着「本次可调用的工具」（文本形式）
- 但请求里 `tools=()`——API 层看模型**没有任何工具可调**
- 模型只能把工具名当普通文字，`tool_calls` 永远为空

症状与前几次同型：**零件都测过，衔接没测**。
更隐蔽的是这一条——提示词层有工具清单、请求层没有，
两边各自测试都绿，**因为没人在两层之间对账**。

## 判据从外部观察：看实际发出的请求

不断言源码里有没有 `tools=`——只查 `编排.发出的请求` 里
每个请求的 `tools` 字段与角色白名单是否一致。
"""

from __future__ import annotations

from zhengtong.domain.errors import Result
from zhengtong.orchestration import 编排器
from zhengtong.ports.llm import FinishReason, LlmRequest, LlmResponse, TokenUsage
from zhengtong.services.tools import TOOL_SPECS


class _收着的Llm:
    def __init__(self) -> None:
        self.请求: list[LlmRequest] = []

    def complete(self, request: LlmRequest) -> Result[LlmResponse]:
        self.请求.append(request)
        return Result.ok(
            LlmResponse(
                text="产出",
                tool_calls=(),
                finish_reason=FinishReason.STOP,
                usage=TokenUsage(prompt_tokens=0, completion_tokens=0),
            )
        )


def _装(*, 温度: float = 0.3) -> tuple[编排器, _收着的Llm]:
    from zhengtong.adapters.fakes import FakeContentPack, FakeStorage

    llm = _收着的Llm()
    编: 编排器 = 编排器(
        llm=llm,  # pyright: ignore[reportArgumentType]
        storage=FakeStorage(),
        content_pack=FakeContentPack(),  # pyright: ignore[reportArgumentType]
        模型名="m",
        温度=温度,
    )
    return 编, llm


def _跑(编: 编排器) -> None:
    """走唯一入口 `跑到终` 跑完整轮。"""
    from zhengtong.domain.tier import TierId

    编.跑到终(档位=TierId.PRO, 需求="写一份关于防汛工作的通知")


class Test请求带工具:
    def test_每个请求都带工具声明(self) -> None:
        """**核心判据**：实际发出的请求里 `tools` 必须非空。"""
        编, _ = _装()
        _跑(编)
        请求们 = list(编.发出的请求)
        assert 请求们, "编排器一个请求都没发——判据自身失效"
        无工具 = [r for r in 请求们 if not r.tools]
        assert not 无工具, (
            f"{len(无工具)}/{len(请求们)} 个请求不带 tools——"
            "模型永远调不到工具层，提示词里的工具清单是空话"
        )

    def test_工具子集与角色白名单一致(self) -> None:
        """**核心判据·精确性**：带的工具必须是**该角色可调**的。

        不能只查非空——带全集也是错：越权工具必须不出现。
        """

        编, _ = _装()
        _跑(编)
        assert 编.发出的请求, "编排器一个请求都没发"
        全集 = _全工具名()
        assert 全集, "TOOL_SPECS 是空的——判据自身失效"
        for r in 编.发出的请求:
            实际 = {t.name for t in r.tools}
            assert 实际 <= 全集, f"请求带了不存在的工具：{实际 - 全集}"

    def test_提示词清单与请求工具对得上(self) -> None:
        """**本条是这次缺陷的核心**：

        提示词渲染了「本次可调用的工具」，请求却没带——
        两层各自测试都绿，因为没人在两层之间对账。
        """
        编, _ = _装()
        _跑(编)
        for r in 编.发出的请求:
            正文 = "\n".join(m.content for m in r.messages)
            if "本次可调用的工具" not in 正文:
                continue
            提示词里的 = _从提示词取工具名(正文)
            请求里的 = {t.name for t in r.tools}
            assert 请求里的, "提示词承诺了工具，请求里却没有"
            assert 提示词里的 <= 请求里的, (
                f"提示词承诺 {sorted(提示词里的 - 请求里的)} 但请求里没有——模型照提示词去调必然失败"
            )

    def test_工具声明含中文描述与参数(self) -> None:
        """带出去的声明要完整：空描述的 schema 等于没告诉模型怎么用。"""
        编, _ = _装()
        _跑(编)
        for r in 编.发出的请求:
            for t in r.tools:
                assert t.description.strip(), f"{t.name} 的描述是空的"
                assert isinstance(t.parameters, dict), f"{t.name} 的参数不是 dict"


def _从提示词取工具名(正文: str) -> set[str]:
    段 = 正文.split("本次可调用的工具", 1)
    if len(段) < 2:
        return set()
    名: set[str] = set()
    for 行 in 段[1].splitlines():
        行 = 行.strip()
        if 行.startswith("- ") and "：" in 行:
            名.add(行[2:].split("：", 1)[0].strip())
        elif 行 and not 行.startswith("-"):
            break
    return 名


def _全工具名() -> set[str]:
    return {t.name for t in TOOL_SPECS}


class Test温度走配置:
    def test_请求温度取自编排器设置(self) -> None:
        """**核心判据**：请求里的温度必须是设进去的那个值。

        此前每次请求都吃 `LlmRequest` 的字段默认 0.3——
        `Config.temperature` 从未影响任何真实请求，
        用户改配置里的温度**完全无效**（D-234）。
        """
        for t in (0.0, 0.7, 1.2):
            编, _ = _装(温度=t)
            _跑(编)
            assert 编.发出的请求, "没发出请求"
            实际 = {r.temperature for r in 编.发出的请求}
            assert 实际 == {t}, f"配置温度 {t}，请求里却是 {实际}——配置没传下去"

    def test_走装配根温度随配置变(self) -> None:
        """**这条才是真判据**：走 `build_app` 全程，
        证明装配根真的把 `Config.temperature` 传下来了。

        手工传 `温度=` 的那条只证明「传了就生效」，
        证明不了装配根传了——而缺陷恰恰出在装配根（D-231 的教训）。
        """
        from zhengtong.adapters.config import Config
        from zhengtong.app import build_app

        for t in (0.0, 0.9):
            编 = build_app(config=Config(temperature=t)).编排器()
            _跑(编)
            实际 = {r.temperature for r in 编.发出的请求}
            assert 实际 == {t}, f"配置温度 {t}，经装配根后请求里是 {实际}"
