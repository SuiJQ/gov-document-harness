"""16 个工具的**成功路径**逐个走通（第 10 步判据的第一条）。

## 为什么必须有这个文件

`test_tools.py` 主要测**拒绝与失败**（白名单、审计、参数校验）。
但那只能证明「不该做的做不了」，**证明不了「该做的做得了」**。

实测发现：只跑 `test_tools.py` 时 `handlers.py` 覆盖率只有 **54%**——
大量 handler 的成功分支一行没被执行。而一个从未成功执行过的 handler
和一段死代码在覆盖率报告里长得一模一样。

这正是 D-165 的形态的前一格：**零件都测过（失败路径），衔接没测（成功路径）**。

## 怎么写才算数

每个工具至少一条**成功断言**，且断言的是**可观察的结果**
（文件真的落盘、检索真的返回命中、状态真的改了），不是「没报错」。
「没报错」这种断言会把空实现判成通过。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from zhengtong.adapters import paths
from zhengtong.adapters.fakes import (
    FakeContentPack,
    FakeStorage,
    FakeUserFiles,
)
from zhengtong.adapters.fs_storage import FsStorage
from zhengtong.domain.errors import Result
from zhengtong.domain.ids import RunId
from zhengtong.domain.role import RoleId
from zhengtong.ports.retriever import SearchHit
from zhengtong.ports.validators import ValidationIssue, ValidationResult
from zhengtong.services.tools import TOOL_SPECS, 审计器, 工具依赖, 工具运行器
from zhengtong.services.tools.calllog import 调用存档

_RUN = RunId("r-ok")


class _可编排检索:
    """**按查询词返回**——第一版不管查什么都返回同一批命中，
    于是「零命中」这条判据永远测不到（查询换了、结果没变）。

    **真正实现 `RetrieverPort`**（不靠 cast 硬转）：
    Protocol 的方法签名要写准，否则类型检查器会正确地拒绝
    「用 cast 掩盖签名不符」——那只会把问题推到运行时。
    """

    def __init__(self, 库: dict[str, tuple[SearchHit, ...]] | None = None) -> None:
        self.库 = 库 or {}
        self.查询过: list[str] = []

    def search(self, query: str, top_k: int = 8) -> Result[tuple[SearchHit, ...]]:
        self.查询过.append(query)
        return Result.ok(self.库.get(query, ())[:top_k])

    def build(self, docs: tuple[tuple[str, str], ...]) -> Result[None]:
        return Result.ok_none()


class _可编排校验:
    """真正实现 `ValidatorPort`。"""

    def __init__(self, *, 通过: bool = True) -> None:
        self.通过 = 通过
        self.跑过: list[str] = []

    def run_hard_constraint(self, payload: dict[str, object]) -> Result[ValidationResult]:
        self.跑过.append("hard")
        if self.通过:
            return Result.ok(ValidationResult(script="hard", passed=True))
        # `blocking` 是**无锚点**的问题（`not i.has_anchor`）——
        # 有锚点表示可定位、可核实，故不算阻断。
        return Result.ok(
            ValidationResult(
                script="hard",
                passed=False,
                issues=(ValidationIssue(rule="D7", message="不得新增收费"),),
            )
        )

    def run_structure(self, payload: dict[str, object]) -> Result[ValidationResult]:
        self.跑过.append("structure")
        return Result.ok(ValidationResult(script="structure", passed=True))


_存档 = 调用存档()
_存档.记("call-demo", "材料正文：关于做好稳增长工作的通知……")


def _依赖(tmp_path: Path, *, 真存储: bool = False) -> tuple[工具依赖, _可编排检索, _可编排校验]:
    存储: FakeStorage | FsStorage
    if 真存储:
        建 = paths.ensure_dirs(tmp_path)
        assert 建.is_ok
        存储 = FsStorage(ws=建.unwrap())
    else:
        存储 = FakeStorage()
    检索 = _可编排检索(
        库={"稳增长": (SearchHit(ref="门禁/G3", snippet="G3 降级须标注", score=1.0),)}
    )
    校验 = _可编排校验()
    d = 工具依赖(
        storage=存储,
        user_files=FakeUserFiles(contents={"材料.txt": "关于做好稳增长工作的通知"}),
        retriever=检索,
        content_pack=FakeContentPack(),
        validators=校验,
        run_id=_RUN,
        调用存档=_存档,
    )
    return d, 检索, 校验


def _跑(d: 工具依赖):
    return 工具运行器(依赖=d, 审计=审计器(时钟=lambda: 1.0))


# ═══ 公共区 ═══


def test_shared_write与read与list(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path, 真存储=True)
    跑 = _跑(d)
    写 = 跑.调用(
        "shared.write",
        角色=RoleId.STRUCTURE,
        参数={
            "name": "大纲",
            "kind": "outline",
            "content": "一、背景\n二、举措",
        },
    )
    assert 写.is_ok, 写.error
    assert "大纲" in 写.unwrap()
    列 = 跑.调用("shared.list", 角色=RoleId.LANGUAGE, 参数={"kind": "outline"})
    assert 列.is_ok
    assert "大纲" in 列.unwrap()
    读 = 跑.调用("shared.read", 角色=RoleId.LANGUAGE, 参数={"name": "大纲"})
    assert 读.is_ok
    assert "稳增长" not in 读.unwrap()  # 读的是别的东西，说明串了
    assert "一、背景" in 读.unwrap()


def test_shared_read读不到时报错(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path)
    跑 = _跑(d)
    r = 跑.调用("shared.read", 角色=RoleId.STRUCTURE, 参数={"name": "不存在"})
    assert r.is_error


# ═══ 知识库 ═══


def test_kb_search返回命中(tmp_path: Path) -> None:
    d, 检索, _ = _依赖(tmp_path)
    跑 = _跑(d)
    r = 跑.调用("kb.search", 角色=RoleId.STRUCTURE, 参数={"query": "稳增长", "scope": "kb"})
    assert r.is_ok, r.error
    assert "G3" in r.unwrap()
    assert 检索.查询过 == ["稳增长"], "检索器没被真正调用"


def test_kb_search无命中时不编造(tmp_path: Path) -> None:
    """零命中要明说「没找到」，不能返回一段编造的候选（D-007）。"""
    d, _, _ = _依赖(tmp_path)
    跑 = _跑(d)
    r = 跑.调用("kb.search", 角色=RoleId.STRUCTURE, 参数={"query": "无关", "scope": "kb"})
    assert r.is_ok
    assert "未找到" in r.unwrap()


def test_kb_read_pack(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path)
    跑 = _跑(d)
    r = 跑.调用("kb.read_pack", 角色=RoleId.STRUCTURE, 参数={"item": "红线E1"})
    assert r.is_ok
    assert "红线E1" in r.unwrap()


def test_kb_read_user_file(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path)
    跑 = _跑(d)
    r = 跑.调用("kb.read_user_file", 角色=RoleId.STRUCTURE, 参数={"filename": "材料.txt"})
    assert r.is_ok
    assert "稳增长" in r.unwrap()


# ═══ 汇报 ═══


def test_msg_report_to_lead真的送出(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path)
    收到: list[tuple[str, dict[str, object]]] = []
    d.汇报 = lambda s, p: (收到.append((s, p)), "已收")[1]
    跑 = _跑(d)
    r = 跑.调用(
        "msg.report_to_lead",
        角色=RoleId.LANGUAGE,
        参数={
            "summary": "第三章语气偏口语",
            "payload": {"段落": 3},
        },
    )
    assert r.is_ok
    assert 收到 == [("第三章语气偏口语", {"段落": 3})], "汇报没真的送到主理人"


def test_msg_request_from_lead真的送出(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path)
    收到: list[tuple[str, bool]] = []
    d.请示 = lambda q, b: (收到.append((q, b)), "去问用户")[1]
    跑 = _跑(d)
    r = 跑.调用(
        "msg.request_from_lead",
        角色=RoleId.COMPLIANCE,
        参数={
            "question": "这句需要出处吗",
            "blocking": True,
        },
    )
    assert r.is_ok
    assert 收到 == [("这句需要出处吗", True)]


# ═══ 草稿 ═══


def test_draft_submit落盘且计数递增(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path, 真存储=True)
    跑 = _跑(d)
    角色 = RoleId.COMPLIANCE
    r = 跑.调用("draft.submit", 角色=角色, 参数={"content": "正文", "version": 1})
    assert r.is_ok, r.error
    assert d.草稿计数[角色] == 1
    assert list((tmp_path / "data").rglob("v1")), "草稿没落盘"


# ═══ 校验 ═══


def test_validate_run通过(tmp_path: Path) -> None:
    d, _, 校验 = _依赖(tmp_path)
    跑 = _跑(d)
    r = 跑.调用("validate.run", 角色=RoleId.TEAM_LEAD, 参数={"checker": "hard_constraint"})
    assert r.is_ok
    assert "通过" in r.unwrap()
    assert 校验.跑过 == ["hard"], "校验器没被调用"


def test_validate_run不通过时列出问题(tmp_path: Path) -> None:
    """不通过必须**说清哪条不过**，不能只回一句「失败」——
    模型据此才知道要改什么。"""
    d, _, _ = _依赖(tmp_path)
    d.validators = _可编排校验(通过=False)
    跑 = _跑(d)
    r = 跑.调用("validate.run", 角色=RoleId.TEAM_LEAD, 参数={"checker": "hard_constraint"})
    assert r.is_ok, "校验不通过不是工具错误，而是校验结论"
    assert "不得新增收费" in r.unwrap()


def test_validate_run走structure(tmp_path: Path) -> None:
    d, _, 校验 = _依赖(tmp_path)
    跑 = _跑(d)
    _ = 跑.调用("validate.run", 角色=RoleId.TEAM_LEAD, 参数={"checker": "structure"})
    assert 校验.跑过 == ["structure"]


# ═══ 流程工具 ═══


def test_flow_status返回成员状态(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path)
    d.状态 = lambda: {"structure": "运行中", "language": "空闲"}
    跑 = _跑(d)
    r = 跑.调用("flow.status", 角色=RoleId.TEAM_LEAD, 参数={})
    assert r.is_ok
    assert "structure=运行中" in r.unwrap()


def test_flow_sleep真的挂起(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path)
    事件: list[str] = []
    d.挂起 = 事件.append
    跑 = _跑(d)
    assert 跑.调用("flow.sleep", 角色=RoleId.STRUCTURE, 参数={"reason": "等大纲"}).is_ok
    assert 事件 == ["等大纲"]


def test_flow_wake_on真的设定条件(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path)
    条件: list[tuple[str, str]] = []
    d.唤醒 = lambda e, r: 条件.append((e, r))
    跑 = _跑(d)
    assert 跑.调用(
        "flow.wake_on",
        角色=RoleId.STRUCTURE,
        参数={
            "event": "gate.passed",
            "role": "team-lead",
        },
    ).is_ok
    assert 条件 == [("gate.passed", "team-lead")]


def test_flow_ask_user真的问出去(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path)
    收到: list[tuple[object, ...]] = []
    d.问用户 = lambda f: (收到.append(f), "用户答：已确认")[1]
    跑 = _跑(d)
    字段 = ({"字段": "时限", "必填": True},)
    r = 跑.调用("flow.ask_user", 角色=RoleId.TEAM_LEAD, 参数={"fields": 字段})
    assert r.is_ok
    assert 收到 == [字段]
    assert "已确认" in r.unwrap()


def test_flow_rollback真的回滚(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path)
    动作: list[tuple[str, bool]] = []
    d.回滚 = lambda s, k: 动作.append((s, k))
    跑 = _跑(d)
    assert 跑.调用(
        "flow.rollback",
        角色=RoleId.TEAM_LEAD,
        参数={
            "step": "第三章",
            "keep": True,
        },
    ).is_ok
    assert 动作 == [("第三章", True)]


def test_flow_inject_directive真的转达(tmp_path: Path) -> None:
    d, _, _ = _依赖(tmp_path)
    转达: list[str] = []
    d.转达 = 转达.append
    跑 = _跑(d)
    assert 跑.调用(
        "flow.inject_directive",
        角色=RoleId.TEAM_LEAD,
        参数={
            "text": "别写得太长",
        },
    ).is_ok
    assert 转达 == ["别写得太长"]


# ═══ 覆盖闭合 ═══


def test_每个工具都至少被本文件走到一次(tmp_path: Path) -> None:
    """闭合判据：本文件真的覆盖了 16 个工具的**成功路径**。

    没有这条，前面的测试可能因为参数写错而根本没调到目标 handler，
    而测试照样全绿。
    """
    d, _, _ = _依赖(tmp_path, 真存储=True)
    d.状态 = lambda: {}
    d.挂起 = lambda _r: None
    d.唤醒 = lambda _e, _r: None
    d.问用户 = lambda _f: "好"
    d.回滚 = lambda _s, _k: None
    d.转达 = lambda _t: None
    d.汇报 = lambda _s, _p: "收到"
    d.请示 = lambda _q, _b: "照办"
    跑 = _跑(d)
    走到的: set[str] = set()
    参数表 = {
        "shared.write": {"name": "n", "kind": "note", "content": "c"},
        "shared.read": {"name": "n"},
        "shared.list": {},
        "kb.search": {"query": "通知", "scope": "kb"},
        "kb.read_pack": {"item": "红线E1"},
        "kb.read_user_file": {"filename": "材料.txt"},
        "msg.report_to_lead": {"summary": "s"},
        "msg.request_from_lead": {"question": "q"},
        "draft.submit": {"content": "c", "version": 1},
        "validate.run": {"checker": "hard_constraint"},
        "flow.status": {},
        "flow.sleep": {"reason": "r"},
        "flow.wake_on": {"event": "e"},
        "flow.ask_user": {"fields": []},
        "flow.rollback": {"step": "s"},
        "flow.inject_directive": {"text": "t"},
        # tool.result 查回 call-demo（见上方 _存档）
        "tool.result": {"call_id": "call-demo"},
        "gate.report": {"gate_id": "G1", "conclusion": "通过", "evidence": "x"},
    }
    for s in TOOL_SPECS:
        # **按工具自己的白名单挑一个合法调用者**（D-384）。
        # 早先一律用 TEAM_LEAD，于是「主理人向主理人汇报」这种调用也被当成
        # 合法路径走了一遍——判据绿着，而那条路径在语义上根本不存在。
        角色 = min(s.allowed_roles, key=str) if s.allowed_roles else RoleId.TEAM_LEAD
        assert s.allowed_roles, f"{s.name} 没有任何合法调用者——白名单成了空集"
        r = 跑.调用(s.name, 角色=角色, 参数=cast(dict[str, object], 参数表[s.name]))
        assert r.is_ok, f"{s.name} 成功路径未走通（角色={角色}）：{r.error}"
        走到的.add(s.name)
    assert 走到的 == {s.name for s in TOOL_SPECS}, "有工具的成功路径没被覆盖"
