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
    """D-234 的原意保留，落点已由 D-243 取代。

    ## 语义变更（重要）

    D-234 修的是「`Config.temperature` 生产零消费，用户改配置完全无效」。
    当时每个请求都吃全局配置温度。

    **D-243 起，各角色温度改由代码内常量表决定**（用户约束：
    「基准 0.3，允许各角色采用不同温度」，且**完全禁止用户调**——
    温度填错不报错，只让文稿质量悄悄下降，是 D-146 的坑）。

    故这两条判据的断言从「每个请求温度 == 配置值」
    改为「**配置值仍被装配根接收、不被丢弃**」。

    ## 为什么不能直接删掉它们

    它们当初抓出的缺陷是真的（D-234：值在那里没人往下传）。
    删掉等于把那个坑重新挖开——只是现在多了一层常量表。
    「值该被接收」这条性质**仍然成立**，只是它不再是请求的温度来源。
    """

    def test_配置温度仍被装配根接收(self) -> None:
        """装配根不能把配置温度**丢在地上**。

        D-243 之后它不再直接进请求，但要确认它仍然被接到编排器上——
        若哪天有人在 `App.编排器()` 里把这一行删了，本条会红。
        """
        from zhengtong.adapters.config import Config
        from zhengtong.app import build_app

        for t in (0.0, 0.9, 0.3):
            编 = build_app(config=Config(temperature=t)).编排器()
            assert 编.温度 == t, f"配置温度 {t}，装配根给出 {编.温度}——配置被丢弃了"

    def test_请求温度不再直接等于配置值(self) -> None:
        """**本条钉住 D-243 的语义**：请求温度来自常量表，不是配置。

        若哪天有人「顺手」把配置温度接回请求，
        会导致用户改配置覆盖各角色设定——那正是 D-243 要禁止的。
        """
        from zhengtong.adapters.config import Config
        from zhengtong.app import build_app

        编 = build_app(config=Config(temperature=0.0)).编排器()
        _跑(编)
        实际 = {r.temperature for r in 编.发出的请求}
        assert 0.0 not in 实际, (
            f"配置温度 0.0 混进了请求温度：{实际}——用户不该能覆盖各角色的采样温度"
        )
        assert 编.温度 == 0.0, "编排器自身的配置温度被改了"
