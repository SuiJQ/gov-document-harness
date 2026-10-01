"""架构文档与代码的一致性校验。

**为什么需要这一组测试**：第 1 步重设计事件体系后没有回写架构文档，
导致架构 §5.4 有 29 项、代码 18 项且仅 5 项交集——分叉了整整两个开发步骤
都没人发现，因为两边的测试各自都能过。

教训是：**两侧各自正确不等于两侧一致**。故把一致性本身做成断言，
只要有人改了任一边而忘了另一边，测试立刻失败。

它读的是 `docs/ARCHITECTURE.md` 原文，不是某个转述——
转述会与原文一起腐化，那就白测了。
"""

from __future__ import annotations

import re
from pathlib import Path

from zhengtong.domain.event import EventKind

ARCH = Path(__file__).resolve().parent.parent / "docs" / "ARCHITECTURE.md"

# §5.4 里"而非 X"式对照说明用的旧名。这些是**有意保留**的对比文本，
# 不是事件定义，故校验时排除——但列出清单，好让新增的误留无处可藏。
_DELIBERATELY_OLD = frozenset({"artifact.ready", "tool.called"})


def _arch_event_names() -> set[str]:
    """从架构 §5.4 表格里抽出事件名。"""
    text = ARCH.read_text(encoding="utf-8")
    start = text.index("### 5.4 事件目录")
    end = text.index("### 5.5 追溯能力清单")
    section = text[start:end]
    # `re.findall` 的返回标注是 `list[Any]`；此处用带类型的别名收窄，
    # 否则后续 `startswith` 等调用会一路传染 Unknown（D-086 同族问题）。
    matches: list[str] = re.findall(r"`([a-z]+\.[a-z_]+)`", section)
    found = set(matches)
    # `data.content` 是字段名不是事件名
    return {n for n in found if not n.startswith("data.")} - _DELIBERATELY_OLD


def _code_event_names() -> set[str]:
    return {k.value for k in EventKind}


def test_架构事件表与代码逐字一致() -> None:
    """两边事件名必须完全相同——不多不少。

    断言双向差集都是空，比只断言"包含"严格：
    只查包含会漏掉"代码里多了一个没人知道的事件"。
    """
    arch = _arch_event_names()
    code = _code_event_names()

    only_arch = arch - code
    only_code = code - arch
    assert not only_arch, f"架构有而代码没有：{sorted(only_arch)}"
    assert not only_code, f"代码有而架构没有：{sorted(only_code)}（须先改架构 §5.4）"


def test_事件总数为37() -> None:
    """锁定事件总数。改动它就得同步改架构、EVENT_MERGE.md 与本断言。"""
    assert len(EventKind) == 37, "事件数变了：架构 §5.4、EVENT_MERGE.md、本行都要改"


#: 合法领域前缀。加新领域时须同时更新本表与架构 §5.4。
VALID_DOMAINS = frozenset(
    {
        "run",
        "phase",
        "role",
        "tool",
        "model",
        "retrieval",
        "gate",
        "context",
        "user",
        "artifact",
        "export",
        "error",
        "direction",
    }
)

#: 不遵循"过去式"的动作词——它们描述的是**发生的瞬间**而非结果，
#: 故用原形：`role.turn` 是一次轮次（不是"已轮过"），
#: `gate.started` 门禁的起点事件；`model.request` 同理。
#:
#: 曾按"全部必须过去式"写这条规则，结果被 `role.turn` 判失败。
#: 教训：规则要按**语义**定，不能按形式一刀切——
#: 一条误杀的规则会逼人用 noqa 绕过，那才是真损失。
NON_PASTTENSE_OK = frozenset(
    {
        "role.turn",
        "gate.started",
        "model.request",
        "tool.denied",
        "user.answered",
        "phase.artifact_ready",
        "phase.artifact_updated",
    }
)


def test_事件名符合命名规则() -> None:
    """`领域.动作`。防止有人随手加个 `ArtifactReady` 或 `model_call` 之类。

    只校验结构，不强制动作词为过去式——理由见 `NON_PASTTENSE_OK`。
    """
    for kind in EventKind:
        domain, sep, action = kind.value.partition(".")
        assert sep and domain and action, f"{kind.value} 不符合 `领域.动作`"
        assert domain in VALID_DOMAINS, f"{kind.value} 的领域前缀不在规范内"
        assert "_" not in domain, f"{kind.value} 的领域部分不应含下划线"


def test_过去式豁免表没有多余条目() -> None:
    """豁免表里的每个条目都必须是真事件。

    否则删掉某个事件后，豁免表会留下幽灵条目，
    让人以为"还有这个事件不需要过去式"，实际已无此事件。
    """
    values = {k.value for k in EventKind}
    stale = NON_PASTTENSE_OK - values
    assert not stale, f"豁免表里有已不存在的事件：{sorted(stale)}"
    assert values >= NON_PASTTENSE_OK, "豁免表须全部为真事件"


def test_合并裁决文档存在() -> None:
    """`EVENT_MERGE.md` 记录了为什么两边要合、按什么规则合。

    它是这次分叉的处置记录。删掉它，后来人只会看到"两边曾经不一致"，
    看不到**为什么选了这个折中**，于是下次又各改各的。
    """
    assert (ARCH.parent.parent / "EVENT_MERGE.md").exists(), "合并裁决记录缺失"


def test_架构文档声明了两表一致() -> None:
    """架构 §5.4 应当显式声明已与代码对齐，避免读者以为仍是旧表。"""
    text = ARCH.read_text(encoding="utf-8")
    section = text[text.index("### 5.4") : text.index("### 5.5")]
    assert "EVENT_MERGE.md" in section, "架构 §5.4 未指向合并裁决文档"
    assert "逐字一致" in section, "架构 §5.4 未声明与代码一致"
