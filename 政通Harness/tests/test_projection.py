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


def test_白名单恰好九项() -> None:
    """白名单从 5 项一路增到 9 项，每次都有用户明确要求。

    5 → `RUN_STARTED`（D-150：告知扫到几个文件）→ 6
    6 → `MODEL_RESPONSE`（D-277：token 是账单）→ 7
    7 → `RUN_CANCELLED`（用户按 Esc 要有回音）→ 8
    8 → `ARTIFACT_UPDATED`（用户裁决：只显示第 2 版）→ 9

    原注释说架构 §5.4 是「5 项」，现已过时，故在此复述演进过程——
    **只写当前数字的话，下一个人会以为它是随手定的**。

    架构 §5.4 原本写「表内 **5 项**」，首次修订为 **6 项**。

    用户明确要求新增：
    > 「界面上能不能告诉用户扫描到了几个用户给 AI 的文件，
    >  相当于告诉用户它识别到的，而不是不知道。」

    故 `RUN_STARTED` 进入白名单。**架构 §5.4 的「5 项」需同步改为 6 项**
    （D-150 已记录该修订）——这里若不跟着改，数字对不上就会有人
    以为是误改而改回去。

    **不违背 D-023**：D-023 限制的是内部过程噪声（成员状态、审核意见、
    模型间交互），而"程序读到了哪些用户文件"是**系统对用户输入的确认**，
    用户有权知道。
    """
    # 用户裁决（2026-10）：门禁打回重写后**只显示第 2 版**，
    # 实现它的 `ARTIFACT_UPDATED` 必须可见——它承载的就是那份阶段成果
    # 本身，不是「模型间交互」也不是「审核意见」（D-046 不禁）。
    assert len(USER_VISIBLE_KINDS) == 9


def test_白名单恰为架构指定的九项() -> None:
    期望 = {
        EventKind.RUN_STARTED,  # 用户新增：扫描到几个文件、哪些文件名
        EventKind.ARTIFACT_READY,
        # 重写后的成果即那份阶段成果本身；留在 INTERNAL 的话，
        # 投影开头就 return，界面会停在第一版——用户明确不要那个结果
        EventKind.ARTIFACT_UPDATED,
        EventKind.EXPORT_BLOCKED,
        EventKind.EXPORT_DONE,
        EventKind.RUN_FAILED,
        EventKind.USER_QUESTION_RAISED,
        EventKind.MODEL_RESPONSE,  # 用户新增：token 用量是账单（D-277）
        EventKind.RUN_CANCELLED,  # 用户按 Esc 是用户自己的动作，界面必须有回音
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
        EventKind.MODEL_ERROR,
        EventKind.MODEL_RETRY,
    ],
)
def test_模型间交互不可见(kind: EventKind) -> None:
    """D-046：不显示模型间交互。

    **`MODEL_RESPONSE` 不在此列**（D-277）：用户明确要求
    「标准模式也显示 Token」。进白名单的是**用量这一个数字**，
    不是「谁问了什么、谁答了什么」——后者仍由 `MODEL_REQUEST` /
    `MODEL_ERROR` / `MODEL_RETRY` 挡着，且事件载荷里本就没有正文字段。
    """
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
        EventKind.RUN_STARTED: {"user_files": 2, "file_names": ("a.md", "b.docx")},
        EventKind.ARTIFACT_READY: {"name": "正文初稿", "content": "全文"},
        EventKind.ARTIFACT_UPDATED: {"name": "正文初稿", "content": "重写后的全文"},
        EventKind.EXPORT_BLOCKED: {"placeholders": ("【发文机关】",)},
        EventKind.EXPORT_DONE: {"files": ("正文.txt",)},
        EventKind.RUN_FAILED: {"detail": "调用失败"},
        EventKind.USER_QUESTION_RAISED: {"fields": "D1"},
        EventKind.MODEL_RESPONSE: {"prompt_tokens": 800, "completion_tokens": 200},
        EventKind.RUN_CANCELLED: {"已完成进度": 2, "总进度": 6},
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


# ═══ D-150：扫描结果要在标准模式显示（用户明确要求）═══


class Test扫描摘要:
    """用户原话：

    > 「界面上能不能告诉用户扫描到了几个用户给 AI 的文件，
    >  相当于告诉用户它识别到的，而不是不知道」
    > 「不要占太大空间，讲一下识别到了几个文件和文件名即可」

    故判据是三个词：**有数字、有名字、限长**。
    """

    def _投(self, 数据: dict[str, object]) -> tuple[str, ...]:
        p = StandardProjection()
        p.accept(_ev(EventKind.RUN_STARTED, 数据))
        return p.lines()

    def test_有数字有名字(self) -> None:
        行 = self._投({"user_files": 2, "file_names": ("情况说明.docx", "数据表.xlsx")})
        assert len(行) == 1, f"应只占一行，实得 {len(行)} 行：{行}"
        文 = 行[0]
        assert "2" in 文, f"没显示数量：{文}"
        assert "情况说明.docx" in 文, f"没显示文件名：{文}"
        assert "数据表.xlsx" in 文, f"没显示第二个文件名：{文}"

    def test_零份时说清楚(self) -> None:
        """**「没找到」也必须说，而不是什么都不显示。**

        用户说「不要让用户不知道」——那反过来也成立：
        不知道有没有材料，同样是「不知道」。
        """
        assert "未发现" in self._投({"user_files": 0, "file_names": ()})[0]
        assert "未发现" in self._投({})[0], "连字段都没有时也不能静默"

    def test_限长不刷屏(self) -> None:
        """**用户明确说「不要占太大空间」，故必须有上限。**"""
        十个 = tuple(f"文件{i}.docx" for i in range(10))
        文 = self._投({"user_files": 10, "file_names": 十个})[0]
        assert len(文) < 90, f"一行 {len(文)} 字，太长了：{文}"
        assert "等 10 份" in 文, f"未折成「等 N 份」：{文}"
        assert "文件0.docx" in 文, "第一个文件名应保留"

    def test_超长名单名截断(self) -> None:
        """**单个文件名也不能刷屏。**"""
        超长 = "很长的名字" * 20 + ".docx"
        文 = self._投({"user_files": 1, "file_names": (超长,)})[0]
        assert "…" in 文, f"超长名未截断：{文}"
        assert len(文) < 70, f"截断后仍过长：{文}"

    def test_数量为准名字为摘要(self) -> None:
        """**发射端截断过名字时，数量仍是事实。**

        数量 8 但只给了 2 个名字（发射端限长）时，
        显示必须是「8 份」而不是「2 份」——
        否则用户会以为程序只识别出 2 份。
        """
        文 = self._投({"user_files": 8, "file_names": ("a.md", "b.md")})[0]
        assert "8 份" in 文, f"数量应取自 user_files：{文}"

    def test_发射端与投影的折线责任分明(self) -> None:
        """**发射端只带数据，显示决策全在投影层。**

        投影层跑在 TUI 主循环里，发射端不该也不必知道屏幕有多宽。
        """
        # 用行为而非私有常量来断言——常量改名不该让测试红，
        # 而「一行有多长」才是用户真正在意的东西。
        十份 = self._投({"user_files": 10, "file_names": tuple(f"文件{i}.docx" for i in range(10))})
        超长 = self._投({"user_files": 1, "file_names": ("很长的名字" * 20 + ".docx",)})
        assert len(十份[0]) < 90 and len(超长[0]) < 70
