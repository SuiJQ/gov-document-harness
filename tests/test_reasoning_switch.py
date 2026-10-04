"""推理开关真的接上了（D-380）。

## 修的是什么

`reasoning_probe` 能实测出「这个后端用第几种写法关推理」，
但结论此前只变成一句中文显示给用户就丢了；
`TierSpec.reasoning_policy` 三档取值写对了（OFF/OFF/ON），
**全仓无一处读它**。于是 `写法表` 里那四道关闭写法
**从未用于关闭任何一次请求**：测了、报告了、用户看得懂，然后丢掉。

## 判据怎么设计的

对着**真实编排器**跑三档，看**真实发出去的请求**带没带关闭字段。

不这么测就会重蹈 `走过的门禁` 与 `SearchHit.score` 的覆辙：
判据只验零件（「这个字段存在」「这个函数被调用」），
不验衔接（「用户发出去的那条请求里到底有没有」）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zhengtong.adapters.reasoning_cache import 关闭方案, 写推理结论, 读推理结论
from zhengtong.domain.tier import ReasoningPolicy, TierId
from zhengtong.ports.llm import LlmRequest


def test_三档策略取值未变() -> None:
    """**先锁住取值**：Pro 档是 ON 这一点本轮之前就对，
    别在接线过程中被顺手改掉。"""
    from zhengtong.domain.tier import TIER_BY_ID

    assert TIER_BY_ID[TierId.CONSULT].reasoning_policy is ReasoningPolicy.OFF
    assert TIER_BY_ID[TierId.FLASH].reasoning_policy is ReasoningPolicy.OFF
    assert TIER_BY_ID[TierId.PRO].reasoning_policy is ReasoningPolicy.ON


def test_关闭方案翻译正确() -> None:
    """写法序号 → 怎么关。第 1–3 种是请求体字段，第 4 种是提示词前缀。"""
    assert 关闭方案(1) == ((("thinking", {"type": "disabled"}),), "")
    assert 关闭方案(3) == ((("reasoning_effort", "none"),), "")
    字段, 前缀 = 关闭方案(4)
    assert 字段 == (), "第 4 种不是请求体字段，硬塞会被静默忽略"
    assert 前缀 == "/no_think"


@pytest.mark.parametrize("序号", [0, -1, 5, 99])
def test_越界序号返回空(序号: int) -> None:
    """越界 → 不做任何控制。**不能抛**——
    缓存坏掉不该让程序起不来。"""
    assert 关闭方案(序号) == ((), "")


def test_落盘与读回() -> None:
    根 = Path("/tmp/_d380_用例")
    写推理结论(根, 模型="模型甲", 写法=2)
    assert 读推理结论(根, 模型="模型甲") == 2


def test_模型对不上当没有结论(tmp_path: Path) -> None:
    """**换模型必须重测**——写法是后端烙印。

    若不校验模型名，换模型后程序会拿**上一个模型的写法**去请求，
    而服务端对不认识的字段通常**静默忽略**：
    用户以为推理关掉了，实际还在推理并计费。
    """
    写推理结论(tmp_path, 模型="模型甲", 写法=2)
    assert 读推理结论(tmp_path, 模型="模型乙") == 0


def test_文件缺失读出零(tmp_path: Path) -> None:
    """没探测过 → 0 → 不附加任何字段，行为与改之前逐字节相同。"""
    assert 读推理结论(tmp_path, 模型="模型甲") == 0


def test_坏文件不抛(tmp_path: Path) -> None:
    """这是提速用的缓存，不是配置。读不到就退化，**
    不该把一个可选缓存变成启动失败点。"""
    (tmp_path / "reasoning.json").write_text("{ 不是 json", encoding="utf-8")
    assert 读推理结论(tmp_path, 模型="模型甲") == 0


def _跑一档(档: TierId, 关闭: tuple[tuple[tuple[str, object], ...], str]) -> list[LlmRequest]:
    """真跑一档，返回**发出去的所有请求**。"""
    from tests.test_seams_round1 import _全过编排器

    器 = _全过编排器()
    器.推理关闭 = 关闭
    got = 器.跑到终(档位=档, 需求="写一份关于防汛工作的通知")
    assert got.is_ok, got.error
    return list(器.发出的请求)


def test_咨询档真的带上关闭字段() -> None:
    """**核心判据**：OFF 档 + 已知写法 → 请求里必须真的有那个字段。"""
    请求 = _跑一档(TierId.CONSULT, ((("reasoning_effort", "none"),), ""))
    assert 请求, "编排器一个请求都没发，判据无效"
    for r in 请求:
        assert ("reasoning_effort", "none") in r.额外字段, (
            f"咨询档是 OFF 档，发出去的请求里却没有关闭字段：{r.额外字段}"
        )


def test_flash档真的带上关闭字段() -> None:
    请求 = _跑一档(TierId.FLASH, ((("reasoning_effort", "none"),), ""))
    for r in 请求:
        assert ("reasoning_effort", "none") in r.额外字段


def test_pro档不附加关闭字段() -> None:
    """**Pro 档是深度思考档**：加了关闭字段等于把它关掉。

    这是最容易搞反的一处——「把探测到的开关用上」听起来总该是好事，
    但用错档位就是**反着来**。
    """
    请求 = _跑一档(TierId.PRO, ((("reasoning_effort", "none"),), ""))
    for r in 请求:
        assert r.额外字段 == (), f"Pro 档不该带关闭字段，实得 {r.额外字段}"


def test_没探测过时全部档位都不附加() -> None:
    """**零风险保证**：没有结论 → 请求体与改之前逐字节相同。

    这条是整个功能的底线：最坏情况下等于「没做」，
    不会把任何一个能用的端点弄坏。
    """
    for 档 in (TierId.CONSULT, TierId.FLASH, TierId.PRO):
        for r in _跑一档(档, ((), "")):
            assert r.额外字段 == (), f"{档.value} 在无结论时不该带任何字段"


def test_前缀写法只加在诉求上() -> None:
    """第 4 种是提示词前缀。加在每条消息上会污染公文正文，
    而模型看到 `/no_think` 开头的公文会当成用户真写的字。"""
    from tests.test_seams_round1 import _全过编排器

    器 = _全过编排器()
    器.推理关闭 = ((), "/no_think")
    got = 器.跑到终(档位=TierId.CONSULT, 需求="写一份关于防汛工作的通知")
    assert got.is_ok
    带前缀 = [r for r in 器.发出的请求 if any("/no_think" in str(m.content) for m in r.messages)]
    assert 带前缀, "前缀写法必须真的加到消息上——否则它就是测了不用"
    for r in 带前缀:
        首条 = str(r.messages[0].content)
        assert 首条.startswith("/no_think"), f"前缀必须加在第一条诉求上，实得 {首条[:40]!r}"


def test_编排器不import适配层() -> None:
    """**守住 D-070**：编排层不许 import 适配层。

    翻译成值这一步必须在装配根/适配层做完，
    否则换实现就会破掉唯一装配点。
    """
    import ast

    源 = Path("src/zhengtong/orchestration/orchestrate.py").read_text(encoding="utf-8")
    导入: list[str] = []
    for 节 in ast.walk(ast.parse(源)):
        if isinstance(节, ast.ImportFrom) and 节.module:
            导入.append(节.module)
        elif isinstance(节, ast.Import):
            导入 += [a.name for a in 节.names]
    越界 = [m for m in 导入 if m.startswith("..adapters") or ".adapters." in m]
    assert not 越界, f"编排层 import 了适配层——D-070 被破坏：{越界}"


def test_编排器确实存了档位() -> None:
    """档位此前只逐层传参、编排器自己不存，
    于是构造请求时无从判断该档该不该关推理。"""
    from tests.test_seams_round1 import _全过编排器

    器 = _全过编排器()
    器.跑到终(档位=TierId.FLASH, 需求="写一份关于防汛工作的通知")
    assert 器.档位 is TierId.FLASH
