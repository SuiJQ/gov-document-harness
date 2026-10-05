"""接线契约判据：本轮体检发现的六类缺口的**常态化守门**。

## 这组判据在防什么

体检（`docs/WIRING_AUDIT.md`）查出的缺口有一个共同形态：
**代码能跑、流程能走，但接线没接上线，且没有判据会报**。

典型样本三个：

| 缺口 | 曾经的写法 | 为何不报 |
| --- | --- | --- |
| 界面读投影字段 | `getattr(投影, "artifacts", ())` | 取不到就返回 `()`，成果卡全空 |
| 桥接调编排器 | 参数标 `object` + `.pyright: ignore` | 编排器缺方法也过检查 |
| 投影字段集分叉 | 协议只声明 `accept`/`lines` | `--debug` 少 7 个字段才崩过一次 |

三者都被 `getattr` / `object` / `ignore` 这类**「合法但失真」的写法**遮住了。
所以这里不只测行为，还测**结构**：协议声明与实现字段是否一致、
真编排器是否满足桥接协议、界面是否又退回了兜底写法。

## 为什么「不许再用 getattr 兜底」要写成判据

类型检查管不住 `getattr`：它返回 `Any`，不报错。
所以只要有人再写一次 `getattr(投影, "artifacts", ...)`，
接线断了又会回到「全空且静默」。这条判据把那条路直接封死。
"""

from __future__ import annotations

import dataclasses
import inspect
import pathlib

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.domain.event import Event, EventKind, Level
from zhengtong.domain.ids import RunId
from zhengtong.domain.phase import PhaseId
from zhengtong.domain.tier import TierId
from zhengtong.kernel.projection import DebugProjection, Projection, StandardProjection
from zhengtong.orchestration.orchestrate import 编排器
from zhengtong.presentation.tui import 事件桥接, 可跑编排

_仓库根 = pathlib.Path(__file__).resolve().parents[1]


def _产物事件(名称: str, 正文: str) -> Event:
    return Event(
        seq=1,
        kind=EventKind.ARTIFACT_READY,
        run_id=RunId("r-1"),
        phase=PhaseId.FINAL,
        level=Level.INFO,
        data={"name": 名称, "content": 正文},
    )


def _代码行(路径: pathlib.Path) -> dict[int, str]:
    """行号 → 该行**只由代码构成**的文本（注释与字符串字面量已剔除）。

    ## 为什么必须剔掉字符串

    这组判据要搜的字样（`getattr`、`pyright: ignore`）**大量出现在本仓的
    文档字符串里**——注释里正写着「原先这里挂的是 `pyright: ignore`」。
    第一版的源码判据直接扫全文，结果**把文档字符串当成了违规代码**，
    判据自己把自己判红了。

    故用 `tokenize`：注释是 `COMMENT`、字符串是 `STRING`，
    两者都不算代码。剩下的才是真正执行的东西。
    """
    import tokenize

    逐行: dict[int, list[str]] = {}
    with 路径.open(encoding="utf-8") as 句柄:
        for tok in tokenize.generate_tokens(句柄.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            逐行.setdefault(tok.start[0], []).append(tok.string)
    return {序: " ".join(片) for 序, 片 in 逐行.items()}


def _非文档行(路径: pathlib.Path) -> set[int]:
    """全部行号**减去**文档字符串所在的行。

    类型忽略（`# pyright: ignore`）本身就是注释，`tokenize` 会把它剔除——
    而它正是要查的东西。所以这条判据反过来做：**先排除文档字符串
    所在的行**，余下的行里出现的注释才算数。
    """
    import ast

    文本 = 路径.read_text(encoding="utf-8")
    树 = ast.parse(文本)
    文档行: set[int] = set()
    for 节 in ast.walk(树):
        if (
            isinstance(节, ast.Expr)
            and isinstance(节.value, ast.Constant)
            and isinstance(节.value.value, str)
        ):
            文档行.update(range(节.value.lineno, 节.value.end_lineno + 1))
    return set(range(1, 文本.count("\n") + 2)) - 文档行


# ── 1. 协议与真件 ──────────────────────────────────────────────


def test_真编排器满足桥接协议() -> None:
    """真 `编排器` 必须结构上满足 `可跑编排`。

    ## 为什么这条不能省

    `在worker里跑` 的参数从前是 `object`，靠 `.pyright: ignore` 与
    `getattr` 硬接。现改成协议，代价是**协议可能与真件脱钩**——
    真编排器改签名而没人改协议，桥接照样能过类型检查、跑起来才炸。

    这条判据把「协议 ↔ 真件」钉死：真件构造出来 isinstance 一下即可。
    """
    编 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 模型名="m")
    assert isinstance(编, 可跑编排), "真编排器必须满足桥接协议，否则 TUI→编排接线已断"


def test_桥接协议的跑到终签名与真件一致() -> None:
    """协议方法名与关键字参数名必须与真件逐字一致。

    结构化子类型只查「有没有这个方法」，**参数名对不上照样过**——
    而参数名错了，调用处会在运行时炸。
    """
    真签 = inspect.signature(编排器.跑到终)
    协议签 = inspect.signature(可跑编排.跑到终)
    assert set(真签.parameters) == set(协议签.parameters), (
        f"桥接协议与真编排器的参数名不一致：真件 {list(真签.parameters)} vs 协议 {list(协议签.parameters)}"
    )


# ── 2. 投影协议与实现不许分叉 ──────────────────────────────────


def test_投影协议字段与实现一致() -> None:
    """协议声明的结构化字段，两个投影类**都必须真的有**。

    ## 这条判据守着一次真实崩溃

    `DebugProjection` 早期是独立类，缺 `artifacts` `errors` `progress`
    等 7 个字段，`--debug` 一跑就 `AttributeError`——而当时
    1905 条判据全绿。根因不是「忘了继承」，
    而是**协议只声明了两个方法，字段集没有任何机制约束**。

    当时的修法（改继承）只堵了这一个洞。故这里从结构上封死：
    协议加了字段，实现就必须有；实现加了字段，协议也该跟上。
    """
    协议字段 = set(Projection.__annotations__)
    assert 协议字段, "Projection 必须声明结构化字段，否则界面读字段只能退回 getattr"

    for 实现 in (StandardProjection, DebugProjection):
        实际字段 = {f.name for f in dataclasses.fields(实现)}
        缺 = 协议字段 - 实际字段
        assert not 缺, f"{实现.__name__} 缺协议声明的字段：{sorted(缺)}（协议与实现已分叉）"


def test_标准投影字段不得多于协议() -> None:
    """基类的字段必须与协议**完全一致**（不多不少）。

    「多」出来的字段意味着：界面读不到它，而它又是每事件都要维护的状态——
    要么它该进协议被界面读到，要么它根本不该存在。中间态最坏：
    有人以为界面显示了它，其实没有。

    `DebugProjection` **不在此列**：它是子类，`prefix` / `records`
    是调试专用扩展，且只经 `lines()` 暴露，刻意不给界面读。
    子类扩展是正当的，基类与协议分叉不是。
    """
    协议字段 = set(Projection.__annotations__)
    多 = {f.name for f in dataclasses.fields(StandardProjection)} - 协议字段
    assert not 多, f"标准投影有协议未声明的字段 {sorted(多)}：界面读不到它们"


# ── 3. 最终成果真的拿得到 ──────────────────────────────────────


def test_最终正文取到的是最后一份成果全文() -> None:
    """**「最终成果」这条取值路径此前零判据**（体检发现）。

    它原先写 `getattr(投影, "artifacts", ())`。这段代码「看起来」是对的，
    但兜底意味着：字段名写错、投影实现改了、接线断了——
    三种情况都只会让成果卡变空，**不报任何错**。

    D-046 要求标准模式展示最终成果，所以这条路径必须有判据守着。
    """
    import asyncio

    from zhengtong.adapters.tui import ZhengTongApp

    桥 = 事件桥接()
    应用 = ZhengTongApp(桥=桥, 根=None, 取编排器=lambda: None, 闸=None)

    桥.喂(_产物事件("文章大纲", "一、总体要求"))
    桥.喂(_产物事件("正文初稿", "初稿正文……"))
    最终 = "**这是最终成果全文**，不该为空。"

    async def 跑() -> str:
        async with 应用.run_test():
            桥.喂(_产物事件("最终成果", 最终))
            return 应用._最终正文()

    取到 = asyncio.run(跑())
    assert 取到 == 最终, f"最终成果取值路径失真：期望 {最终!r}，实得 {取到!r}"


def test_没有任何成果时最终正文为空串而不是崩() -> None:
    """没成果时返回 `""`，不得抛异常——空界面是合法状态。"""
    import asyncio

    from zhengtong.adapters.tui import ZhengTongApp

    桥 = 事件桥接()
    应用 = ZhengTongApp(桥=桥, 根=None, 取编排器=lambda: None, 闸=None)

    async def 跑() -> str:
        async with 应用.run_test():
            return 应用._最终正文()

    assert asyncio.run(跑()) == "", "无成果时必须返回空串而不是抛异常"


# ── 4. 不许再退回 getattr 兜底 ─────────────────────────────────


def test_界面读投影字段不许用getattr兜底() -> None:
    """**封死那条退化路径**。

    `getattr(投影, "artifacts", ())` 这类写法类型检查抓不到
    （返回 `Any`），而它的失效形态正是本项目反复出现的那个：
    **界面全静默、无异常、无日志**。协议已经补齐字段，
    直接属性访问既可读又受类型检查保护，没有理由再用兜底。
    """
    offenders: list[str] = []
    for 路径 in (_仓库根 / "src").rglob("*.py"):
        for 序, 片 in _代码行(路径).items():
            if "getattr" in 片 and "投影" in 片:
                offenders.append(f"{路径.relative_to(_仓库根)}:{序}: {片}")
    assert not offenders, "界面读投影字段不得用 getattr 兜底：\n" + "\n".join(offenders)


def test_桥接层不许出现类型忽略() -> None:
    """桥接层曾有三处 `pyright: ignore` 与一处 `getattr` 兜底。

    `ignore` 与 `object` 一样：**合法但失真**——它把编译期能抓到的
    问题推迟到运行时，而运行时那正是「不报错」的形态。
    这里直接禁止该层再出现 `type: ignore` / `pyright: ignore`。
    """
    路径 = _仓库根 / "src" / "zhengtong" / "presentation" / "tui.py"
    可查行 = _非文档行(路径)
    违规 = [
        f"{序}: {行.strip()}"
        for 序, 行 in enumerate(路径.read_text(encoding="utf-8").splitlines(), start=1)
        if 序 in 可查行 and ("pyright: ignore" in 行 or "type: ignore" in 行)
    ]
    assert not 违规, "桥接层不许类型忽略：\n" + "\n".join(违规)


# ── 5. 真件与替身的对照 ────────────────────────────────────────


def test_档位类型不是object() -> None:
    """档位参数必须是 `TierId`。

    原先 `档位: object`，于是**非法档位要到 `TIER_BY_ID[...]` 才炸**——
    而那是模型交互循环内部，炸起来是一屏 Traceback 而不是一句提示。
    类型标对之后，传错在检查期就暴露。
    """
    签 = inspect.signature(事件桥接.在worker里跑)
    标注 = 签.parameters["档位"].annotation
    assert 标注 == "TierId", f"档位应标 TierId，实标 {标注!r}"


def test_真编排器的档位注解是TierId() -> None:
    """协议侧与真件侧的档位注解必须都是 `TierId`，不能一侧松一侧紧。"""
    真 = inspect.signature(编排器.跑到终).parameters["档位"].annotation
    协议 = inspect.signature(可跑编排.跑到终).parameters["档位"].annotation
    assert 真 == 协议 == "TierId", f"档位注解不一致：真件 {真!r} vs 协议 {协议!r}"
    assert TierId.PRO  # 确认真类型可导入且有成员
