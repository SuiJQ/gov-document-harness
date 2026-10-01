"""`kernel/projection.py` 测试 —— D-023/D-046 的机械落点。

## 为什么这个文件的测试比别处更严

标准模式"不显示成员状态、不展示审核意见"是**用户可见的产品行为**，
承诺写在 D-023/D-046 里。而它的实现方式是白名单——
**靠白名单兜底，不靠"记得别显示"**（投影层文档里的反例说明）。

测试判据据此定：**不测"某类事件没显示"，而测"白名单恰好是这 5 类"**。
因为只要有人往白名单里塞进 `role.*` 或 `gate.*`，
"不显示成员状态"就失效了，而这类回归靠逐事件断言是测不出来的。
"""

from __future__ import annotations

import pytest

from zhengtong.domain.event import (
    DEBUG_ONLY_KINDS,
    USER_VISIBLE_KINDS,
    Event,
    EventKind,
    Level,
    Span,
    is_user_visible,
)
from zhengtong.domain.ids import RunId
from zhengtong.domain.phase import PhaseId
from zhengtong.domain.role import RoleId
from zhengtong.kernel.projection import DebugProjection, StandardProjection

# ═══ 白名单本身的形状 ═══


def test_白名单恰好五项() -> None:
    """架构 §5.4 明文：「用户白名单（对齐 D-023 用户只需知道四件事）：
    表内 **5 项**」。数字对不上就是有人动过它。"""
    assert len(USER_VISIBLE_KINDS) == 5


def test_白名单恰为架构指定的五项() -> None:
    期望 = {
        EventKind.ARTIFACT_READY,
        EventKind.EXPORT_BLOCKED,
        EventKind.EXPORT_DONE,
        EventKind.RUN_FAILED,
        EventKind.USER_QUESTION_RAISED,
    }
    assert set(USER_VISIBLE_KINDS) == 期望


@pytest.mark.parametrize(
    "kind", [EventKind.ROLE_STATE_CHANGED, EventKind.ROLE_TURN, EventKind.ROLE_TOOL_CALL]
)
def test_成员状态绝不可见(kind: EventKind) -> None:
    """D-046 第一条：不显示成员状态。"""
    assert not is_user_visible(kind)
    p = StandardProjection()
    p.accept(_ev(kind, {"role_id": "compliance"}))
    assert p.lines() == ()


@pytest.mark.parametrize(
    "kind",
    [
        EventKind.GATE_STARTED,
        EventKind.GATE_PASSED,
        EventKind.GATE_FAILED,
        EventKind.GATE_REJECTED,
        EventKind.GATE_REWORK,
        EventKind.GATE_ESCALATED,
    ],
)
def test_门禁明细不可见(kind: EventKind) -> None:
    """门禁判定是审核意见的载体，标准模式一律不上台面。"""
    assert not is_user_visible(kind)


@pytest.mark.parametrize(
    "kind",
    [
        EventKind.MODEL_REQUEST,
        EventKind.MODEL_RESPONSE,
        EventKind.MODEL_ERROR,
        EventKind.MODEL_RETRY,
    ],
)
def test_模型间交互不可见(kind: EventKind) -> None:
    """D-046：不显示模型间交互。"""
    assert not is_user_visible(kind)


def test_错误事件不进白名单() -> None:
    """架构 §5.4 明文：「`error.raised` 刻意不进白名单……运行真的失败时，
    由编排层升级为 `run.failed` 再告知——这与门禁"退回修正而非绕过"
    是同一个处理原则：内部细节不上台面」。"""
    assert not is_user_visible(EventKind.ERROR_RAISED)


def test_内部异常名不泄漏到标准界面() -> None:
    p = StandardProjection()
    p.accept(_ev(EventKind.ERROR_RAISED, {"detail": "ZeroDivisionError at src/x.py:42"}))
    joined = "\n".join(p.lines())
    assert "ZeroDivisionError" not in joined
    assert "src/x.py" not in joined


def test_调试专属表不含白名单项() -> None:
    """两张表若重叠，某类事件会同时是"用户可见"和"仅调试"，语义矛盾。"""
    assert not (DEBUG_ONLY_KINDS & USER_VISIBLE_KINDS)


# ═══ 死分支回归防护（D-118）═══


def test_白名单内每类都有对应处理分支() -> None:
    """**这是本文件最重要的一条**。

    D-118 的成因：`StandardProjection.accept` 里挂着 `DIRECTION_CONFIRMED`
    与 `ERROR_RAISED` 两个分支，而它们都不在白名单里——
    方法第一行就 `return`，**这两个分支永远进不去**。
    后果是死代码误导后来者，且那两行永远无法被测试覆盖。

    判据：白名单里每一类事件，喂进去都必须产生可见输出。
    若某类喂进去毫无反应，说明它的处理分支被删了或白名单被改了，
    两种情况都要立刻暴露。
    """
    样例: dict[EventKind, dict[str, object]] = {
        EventKind.ARTIFACT_READY: {"name": "正文初稿", "content": "全文"},
        EventKind.EXPORT_BLOCKED: {"placeholders": ("【发文机关】",)},
        EventKind.EXPORT_DONE: {"files": ("正文.txt",)},
        EventKind.RUN_FAILED: {"detail": "调用失败"},
        EventKind.USER_QUESTION_RAISED: {"fields": "D1"},
    }
    for kind in USER_VISIBLE_KINDS:
        p = StandardProjection()
        p.accept(_ev(kind, 样例[kind]))
        assert p.lines() != (), f"{kind.value} 在白名单里却没有任何显示效果——处理分支死了"


def test_白名单外事件不产生任何效果() -> None:
    """与上一条互为反面：白名单外的一律不得显示。"""
    for kind in EventKind:
        if kind in USER_VISIBLE_KINDS:
            continue
        p = StandardProjection()
        p.accept(_ev(kind, {"name": "x", "content": "y", "detail": "z", "fields": "D1"}))
        assert p.lines() == (), f"{kind.value} 不在白名单却显示了：{p.lines()}"


def test_进度不被内部事件改写() -> None:
    """成果卡显示"已完成"后，一条内部事件不该把进度清掉或篡改。"""
    p = StandardProjection()
    p.accept(_ev(EventKind.ARTIFACT_READY, {"name": "文章大纲", "content": "大纲"}))
    之后 = p.progress
    for kind in EventKind:
        if kind not in USER_VISIBLE_KINDS:
            p.accept(_ev(kind, {}))
    assert p.progress == 之后


# ═══ 标准模式的具体行为 ═══


def test_成果卡记标题与长度() -> None:
    p = StandardProjection()
    p.accept(_ev(EventKind.ARTIFACT_READY, {"name": "写作方向确认单", "content": "共十二字"}))
    assert p.artifacts == (("写作方向确认单", "共十二字"),)
    assert "写作方向确认单" in p.lines()[1]
    assert "4 字" in p.lines()[1]  # 「共十二字」共 4 字


def test_三份阶段成果依次累积() -> None:
    """D-030：标准模式只展示方向确认单、大纲、正文初稿三个阶段成果。"""
    p = StandardProjection()
    for 名 in ("写作方向确认单", "文章大纲", "正文初稿"):
        p.accept(_ev(EventKind.ARTIFACT_READY, {"name": 名, "content": 名 + "内容"}))
    assert [n for n, _ in p.artifacts] == ["写作方向确认单", "文章大纲", "正文初稿"]


def test_提问逐条累积() -> None:
    p = StandardProjection()
    p.accept(_ev(EventKind.USER_QUESTION_RAISED, {"fields": "D1,D2"}))
    p.accept(_ev(EventKind.USER_QUESTION_RAISED, {"fields": "D8"}))
    assert len(p.questions) == 2


def test_运行失败填入失败原因() -> None:
    """D-118 修复点：原先挂的是 `ERROR_RAISED`（永不可达），
    导致 `errors` 永远是空的——用户看不到失败原因。"""
    p = StandardProjection()
    p.accept(_ev(EventKind.RUN_FAILED, {"detail": "模型连续 3 次调用失败"}))
    assert p.errors == ("模型连续 3 次调用失败",)
    assert "运行失败" in p.lines()
    assert p.progress == "运行失败"


def test_失败后进度不谎报已完成() -> None:
    p = StandardProjection()
    p.accept(_ev(EventKind.ARTIFACT_READY, {"name": "正文初稿", "content": "文"}))
    p.accept(_ev(EventKind.RUN_FAILED, {"detail": "导出前扫描发现残留占位符"}))
    assert p.progress != "已完成：正文初稿"


def test_输出顺序稳定() -> None:
    """进度 → 提问 → 成果 → 错误。TUI 靠这个顺序渲染。"""
    p = StandardProjection()
    p.accept(_ev(EventKind.ARTIFACT_READY, {"name": "正文初稿", "content": "文"}))
    p.accept(_ev(EventKind.USER_QUESTION_RAISED, {"fields": "D1"}))
    p.accept(_ev(EventKind.RUN_FAILED, {"detail": "原因"}))
    lines = p.lines()
    # progress 只保留**最新**结论（失败优先），其余按固定段序排列
    assert lines[0] == "运行失败"
    assert any("需要确认" in x for x in lines)
    assert any("▣" in x for x in lines)
    提问行 = next(x for x in lines if "需要确认" in x)
    成果行 = next(x for x in lines if "▣" in x)
    assert lines.index(提问行) < lines.index(成果行)


def test_空投影无输出() -> None:
    assert StandardProjection().lines() == ()


def test_lines为纯读不消费() -> None:
    """连续调两次，第二次必须还在。TUI 每帧都要重画。"""
    p = StandardProjection()
    p.accept(_ev(EventKind.ARTIFACT_READY, {"name": "正文初稿", "content": "文"}))
    assert p.lines() == p.lines()


# ═══ 调试模式 ═══


def test_调试模式全量透传() -> None:
    """D-023「有啥输出啥」：一条都不过滤。"""
    p = DebugProjection()
    for kind in EventKind:
        p.accept(_ev(kind, {"x": 1}))
    assert len(p.records) == len(list(EventKind))


def test_调试模式带序号与阶段() -> None:
    p = DebugProjection()
    p.accept(_ev(EventKind.ARTIFACT_READY, {"name": "正文初稿"}))
    assert "#1" in p.records[0]
    assert "phase=intake" in p.records[0]


def test_调试模式带角色中文名与内部标识() -> None:
    """D-080：调试模式附内部标识便于定位文件。"""
    from zhengtong.domain.event import Span

    p = DebugProjection()
    p.accept(_ev(EventKind.ROLE_TURN, {}, span=Span(role=RoleId.COMPLIANCE.value)))
    assert "骆准绳" in p.records[0]
    assert "compliance" in p.records[0]


def test_调试模式遇未知角色不崩() -> None:
    """角色标识来自事件流，可能是旧值或非法值——不能因此炸掉整个调试输出。"""
    from zhengtong.domain.event import Span

    p = DebugProjection()
    p.accept(_ev(EventKind.ROLE_TURN, {}, span=Span(role="某个未来角色")))
    assert "某个未来角色" in p.records[0]


def test_调试模式带门禁与call_id() -> None:
    import dataclasses

    from zhengtong.domain.event import Span

    p = DebugProjection()
    p.accept(
        dataclasses.replace(_ev(EventKind.GATE_PASSED, {}), span=Span(gate="G2"), call_id="abc123")
    )
    assert "门禁=G2" in p.records[0]
    assert "call=abc123" in p.records[0]


def test_调试模式无数据时不拖尾() -> None:
    p = DebugProjection()
    p.accept(_ev(EventKind.PHASE_STARTED, {}))
    assert "data=" not in p.records[0]


# ═══ 夹具 ═══


def _ev(kind: EventKind, data: dict[str, object], span: Span | None = None) -> Event:
    return Event(
        seq=1,
        kind=kind,
        run_id=RunId("r-投影测试"),
        phase=PhaseId.INTAKE,
        level=Level.INFO,
        span=span,
        data=data,
    )


# ═══ 导出两类事件（D-118 新补的分支）═══


def test_导出被拦须列出待补信息() -> None:
    """D-038：存在未处理占位符时禁止导出，**并列出待补信息**。

    审计 D-118 查出：`export.blocked` 一直在白名单里，却**没有任何分支处理它**。
    后果是导出被拦时用户看不到任何提示——门禁变成静默的，
    比没有门禁更危险。
    """
    p = StandardProjection()
    p.accept(_ev(EventKind.EXPORT_BLOCKED, {"placeholders": ("【发文机关】", "【联系人】")}))
    joined = "\n".join(p.lines())
    assert "【发文机关】" in joined
    assert "【联系人】" in joined
    assert "导出被拦" in p.progress


def test_导出完成须点名文件() -> None:
    p = StandardProjection()
    p.accept(_ev(EventKind.EXPORT_DONE, {"files": ("正文.txt", "附件.docx")}))
    assert "正文.txt" in p.progress
    assert "附件.docx" in p.progress


def test_导出完成但文件名为空也不崩() -> None:
    p = StandardProjection()
    p.accept(_ev(EventKind.EXPORT_DONE, {}))
    assert p.progress == "已导出"


@pytest.mark.parametrize("坏值", [None, "单个字符串", ["a", ""], ("a", None), 42, {"k": "v"}])
def test_清单字段类型异常不崩(坏值: object) -> None:
    """投影层跑在 TUI 主循环里——**不能因发射端写错类型就崩掉整个界面**。"""
    p = StandardProjection()
    p.accept(_ev(EventKind.EXPORT_DONE, {"files": 坏值}))
    p.accept(_ev(EventKind.EXPORT_BLOCKED, {"placeholders": 坏值}))
    assert isinstance(p.lines(), tuple)
