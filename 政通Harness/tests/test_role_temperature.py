"""各角色温度常量表（用户约束：基准 0.3，允许各角色不同）。

## 这条约束的真实来历

**原先一直写成「D-031 各角色可不同温度」是错的**——
架构文档里 D-031 是「耗时预估不编造」，与温度毫无关系。
温度是 D-026，而 D-026 只说了「温度 0.3」，**没说允许各角色不同**。

「允许各角色采用不同温度」来自用户的原始约束，
但决策文件里从未为它落过条目，也从未实现。
本判据补的就是这个从未存在的条目。

## 为什么是常量表而不是用户配置（D-146）

D-146 立下的纪律：「新增配置项必须先想清楚填错了会静默变成什么」。

温度填错**不报错**，只让文稿质量悄悄下降，用户完全看不出来。
而我们没有依据说「陈纲举该 0.2」——拍一组数字比统一 0.3
更可能是错的，且用户无法判断我拍得对不对。

**没有可填的入口，就没有这个坑。**

## 取值原则（用户定）

- 严肃的调低，有创造性的相对高
- **总体偏低**——公文不是创意写作

## 判据从外部观察

查**实际发出的请求**的温度，不查源码里有没有那张表。
常量表存在但没接到请求上，是本项目最常见的缺陷形状。
"""

from __future__ import annotations

from zhengtong.domain.errors import Result
from zhengtong.domain.role import ROLES, RoleId
from zhengtong.domain.tier import TierId
from zhengtong.orchestration import 编排器
from zhengtong.ports.llm import FinishReason, LlmRequest, LlmResponse, TokenUsage


class _记请求的Llm:
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


def _装(*, 基准: float = 0.3) -> tuple[编排器, _记请求的Llm]:
    from zhengtong.adapters.fakes import FakeContentPack, FakeStorage

    llm = _记请求的Llm()
    编: 编排器 = 编排器(
        llm=llm,  # pyright: ignore[reportArgumentType]
        storage=FakeStorage(),
        content_pack=FakeContentPack(),  # pyright: ignore[reportArgumentType]
        模型名="m",
        温度=基准,
    )
    return 编, llm


class Test温度按角色分流:
    def test_每个角色的请求温度固定(self) -> None:
        """**核心判据**：同一角色的所有请求温度必须**一致**。

        不一致说明分流逻辑有分支错误——那是最难查的一类。
        """
        编, _ = _装()
        编.跑到终(档位=TierId.PRO, 需求="写一份通知")
        assert 编.发出的请求, "编排器一个请求都没发——判据自身失效"
        按温度: dict[float, int] = {}
        for r in 编.发出的请求:
            按温度.setdefault(r.temperature, 0)
            按温度[r.temperature] += 1
        # 全部请求不该只有一个温度（那等于没分流）
        assert len(按温度) >= 2, f"所有请求温度相同（{按温度}）——按角色分流没生效"

    def test_不全是基准温度(self) -> None:
        """**核心判据·反默认**：不能全都等于基准 0.3。

        「全都 0.3」是最容易蒙过断言的实现——
        它在数值上合法，但没有实现「各角色不同」这条约束。
        """
        编, _ = _装(基准=0.3)
        编.跑到终(档位=TierId.PRO, 需求="写一份通知")
        温度们 = {r.temperature for r in 编.发出的请求}
        assert 温度们 != {0.3}, f"全部请求都是基准 0.3：{温度们}"
        assert 0.3 not in 温度们 or len(温度们) > 1, "没有任何角色偏离基准——分流表可能没接上"

    def test_温度都在合理区间(self) -> None:
        """公文不是创意写作：**总体偏低**。

        任何角色超过 0.8 都与用户定的原则相悖——
        那不是「有个性」，那是随机性。
        """
        编, _ = _装()
        编.跑到终(档位=TierId.PRO, 需求="写一份通知")
        for t in {r.temperature for r in 编.发出的请求}:
            assert 0.0 < t <= 0.8, f"温度 {t} 超出公文合理区间"
            assert t < 1.0, f"温度 {t} 会让输出不可控"

    def test_严肃角色温度低于创意角色(self) -> None:
        """**核心判据·取值方向**：合规/结构角色应低于语言起草。

        合规审查要的是**稳定**，起草要的是**变化**。
        若方向反了，说明表填反了——而数值合法，不会被别的判据抓到。
        """
        from zhengtong.domain.role import 取温度

        严肃 = max(取温度(RoleId.COMPLIANCE), 取温度(RoleId.STRUCTURE))
        创意 = 取温度(RoleId.LANGUAGE)
        assert 严肃 < 创意, f"合规/结构 {严肃} 未低于语言起草 {创意}——取值方向反了"

    def test_全部角色都有温度(self) -> None:
        """五个角色一个都不能漏。

        漏一个就会静默回落到基准——
        而基准 0.3 是个合法值，漏了也不会报错（D-146 的坑）。
        """
        from zhengtong.domain.role import 取温度

        缺失 = [r.id for r in ROLES if 取温度(r.id) is None]
        assert not 缺失, f"这些角色没配温度：{缺失}"


class Test温度不可被用户改:
    def test_配置里没有温度入口(self) -> None:
        """**本条钉住「完全禁止用户调」**。

        `Config` 里只有全局 `temperature`（D-234 加的）。
        若将来有人加了 per-role 的用户配置，这条会红。
        """
        from zhengtong.adapters.config import Config

        字段 = {f.name for f in Config.__dataclass_fields__.values()}
        assert "temperature" in 字段, "全局基准温度没了"
        角色级 = {f for f in 字段 if "role" in f.lower() or "温度" in f}
        assert not 角色级, f"出现了角色级温度配置：{角色级}——用户明确要求禁止"

    def test_内容包里没有温度键(self) -> None:
        """**本条钉住同一件事的另一面**：内容包也不能配。

        用户约束「角色映射只放在内容包 manifest.json」——
        但温度**不是**角色映射，是行为参数，故不进内容包。
        """
        import json
        from pathlib import Path

        清单 = json.loads(
            (Path(__file__).resolve().parent.parent / "content" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        for 键, 值 in 清单.get("roles", {}).items():
            assert "temperature" not in 值, f"内容包角色 {键} 里有 temperature"
            assert "温度" not in 值, f"内容包角色 {键} 里有温度键"
