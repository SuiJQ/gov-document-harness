"""L1 判据测试（第 3 步）：伪造密钥不入日志；`phase.artifact_ready` 载全文。

第 3 步的两条完成判据各对应一组测试：

- **伪造密钥断言不入日志**：断言的是 `JournalPort` 真正**落到磁盘的**
  文本，而不只是内存里收到的事件对象。因为密钥泄漏的真实路径是日志文件
  被贴给别人看，只检查内存等于没查。
- **`phase.artifact_ready` 载全文**：断言 `data["content"]` 是成果原文
  而非摘要，且它在标准模式下**可见**（D-072/D-030）。
"""

# 本文件放宽 Unknown 类规则，理由同 tests/test_domain_behavior.py：
# `Result.ok(...)` 的返回类型是 `Result[Any]`（D-085：三方案均已实测，这是唯一可行解），
# 故拿它做 lambda 返回值或成员访问时类型为 Unknown。
# 未知只影响静态提示，不影响运行行为，也不影响断言有效性。
# 另外 `tmp_path` 是 pytest 注入的 `Path`，类型信息由 pytest 插件提供，
# 静态检查看不到，故标注为 Path。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportUnusedCallResult=false
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import cast

from zhengtong.domain.errors import Result
from zhengtong.domain.event import Event, EventKind, is_user_visible
from zhengtong.domain.ids import RunId
from zhengtong.domain.role import RoleId
from zhengtong.kernel.bus import EventBus
from zhengtong.kernel.journal import JsonlJournal
from zhengtong.kernel.projection import DebugProjection, StandardProjection
from zhengtong.kernel.redactor import REDACTED, Redactor
from zhengtong.kernel.scheduler import GateGroup, GroupResult, run_groups

# 伪造的密钥。刻意用三种形态：OpenAI 风格、Bearer、裸十六进制。
FAKE_SK = "sk-proj-000111222333444555666777888999AAA"
FAKE_BEARER = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
FAKE_HEX = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def _ev(kind: EventKind, data: dict[str, object] | None = None) -> Event:
    return Event(
        seq=0,
        kind=kind,
        run_id=RunId("r-test"),
        phase="gating",
        data=data or {},
    )


# ── 判据一：伪造密钥不入日志 ──


def test_伪造密钥在落盘文件中查不到(tmp_path: Path) -> None:
    """最强判据：读完整个日志文件，搜不到任何一个伪造密钥。

    刻意覆盖三种形态与三种藏身处（顶层、嵌套 dict、list 元素），
    因为密钥藏在请求体里是常态而非特例。
    """
    journal = JsonlJournal(tmp_path / "events.jsonl")
    secrets = ("my-secret-key-abc123",)
    bus = EventBus(Redactor(extra_patterns=secrets), journal)

    payloads: list[dict[str, object]] = [
        {"note": f"用的是 {FAKE_SK}"},
        {"note": f"Authorization: {FAKE_BEARER}"},
        {"hash": FAKE_HEX},
        {"body": {"headers": {"Authorization": f"Bearer {FAKE_HEX}"}}},
        {"messages": [{"content": "sk-abcdefghijklmnopqrstuvwx"}]},
        {"key": "my-secret-key-abc123"},
    ]
    for p in payloads:
        bus.emit(_ev(EventKind.ROLE_TOOL_CALL, p))

    assert bus.flush(5.0), "泵线程未在超时内排空队列"
    bus.stop()

    raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    for secret in (FAKE_SK, FAKE_HEX, FAKE_BEARER, *secrets, "sk-abcdefghijklmnopqrstuvwx"):
        assert secret not in raw, f"密钥 {secret!r} 泄漏进了日志文件"
    # 脱敏痕迹应当存在——否则"没搜到"可能只是因为日志根本没写成
    assert REDACTED in raw


def test_伪造密钥不进投影() -> None:
    """投影（含调试模式）拿到的也必须是脱敏后的。

    与上一条互补：那条查磁盘文件，这条查内存里的两个投影，
    确保"脱敏在发射处"对所有出口一致，而不是只对 journal 生效。
    """
    bus = EventBus(Redactor())
    std, dbg = StandardProjection(), DebugProjection()
    bus.subscribe(std.accept)
    bus.subscribe(dbg.accept)

    bus.emit(_ev(EventKind.ROLE_TOOL_CALL, {"body": f"key={FAKE_SK}"}))
    assert bus.flush(5.0)
    bus.stop()

    for line in (*std.lines(), *dbg.lines()):
        assert FAKE_SK not in line


def test_普通公文内容不被脱敏误伤() -> None:
    """脱敏必须只打密钥，不打正文——误伤会让日志失去可用性。"""
    text = "关于开展二〇二六年度安全生产大检查的通知，各单位遵照执行。"
    assert Redactor().scrub_text(text) == text


def test_嵌套结构递归脱敏() -> None:
    scrubbed = Redactor().scrub_value({"a": [{"b": f"sk-{FAKE_HEX}"}]})
    assert FAKE_HEX not in json.dumps(scrubbed, ensure_ascii=False)


# ── 判据二：phase.artifact_ready 载全文 ──


def test_阶段成果载全文而非摘要(tmp_path: Path) -> None:
    """D-072：content 装的是成果全文，界面直接渲染。"""
    full_text = "写作方向确认单\n" + ("一、背景与依据。" * 200)
    journal = JsonlJournal(tmp_path / "events.jsonl")
    bus = EventBus(Redactor(), journal)
    std = StandardProjection()
    bus.subscribe(std.accept)

    bus.emit(
        _ev(
            EventKind.ARTIFACT_READY,
            {
                "name": "写作方向确认单",
                "kind": "direction_sheet",
                "content": full_text,
                "is_gate": True,
            },
        )
    )
    assert bus.flush(5.0)
    bus.stop()

    # 落盘的是全文
    stored = journal.read_all()
    assert len(stored) == 1
    assert stored[0].data["content"] == full_text
    # 从磁盘原始文本反解，确认 JSON 序列化没有截断长文本
    # json.loads 返回 Any（JSON 边界固有性质），故显式 cast 后再取字段
    raw_obj = cast(
        "dict[str, object]",
        json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8").strip()),
    )
    assert cast("dict[str, object]", raw_obj["data"])["content"] == full_text
    assert len(full_text) > 1000, "测试样本要够长，才能验证全文而非摘要"

    # 标准模式拿到的也是全文，且成果可见
    assert std.artifacts[0][1] == full_text


def test_阶段成果事件名与架构逐字一致() -> None:
    """事件名是日志检索锚点，与架构不一致会让排障 grep 落空。"""
    assert EventKind.ARTIFACT_READY.value == "phase.artifact_ready"


# ── 白名单：D-046 的机械落点 ──


def test_标准模式只放行六项四类事() -> None:
    """D-023「用户只需知道四件事」+ D-046。

    表内 5 项是因为 `export.blocked` / `export.done` 属同一族
    （导出结果）分开计；语义上仍是四件事。合并裁决见 EVENT_MERGE.md。
    """
    # 用户新增：「告诉用户它识别到了几个文件、哪些文件名」（D-150）
    assert is_user_visible(EventKind.RUN_STARTED)
    assert is_user_visible(EventKind.ARTIFACT_READY)
    assert is_user_visible(EventKind.EXPORT_BLOCKED)
    assert is_user_visible(EventKind.EXPORT_DONE)
    assert is_user_visible(EventKind.RUN_FAILED)
    assert is_user_visible(EventKind.USER_QUESTION_RAISED)
    # 用户新增第 7 项：「标准模式也显示 Token」（D-277）——
    # 用量是账单不是模型间交互，故进白名单
    assert is_user_visible(EventKind.MODEL_RESPONSE)
    # 事件共 38 项，标准界面只放行 7 项——这正是 D-046 的意义
    # （第 6 项 `RUN_STARTED` 见 D-150，第 7 项 `MODEL_RESPONSE` 见 D-277）
    assert len({k for k in EventKind if is_user_visible(k)}) == 7
    assert len(EventKind) == 38, "事件总数变了，架构 §5.4 与 EVENT_MERGE.md 需同步"


def test_错误事件不直接进标准模式() -> None:
    """`error.raised` 是实现细节，运行失败才升级为 `run.failed`。

    直接把 error.raised 放行会让用户看到内部异常名（D-046 反例）。
    """
    assert not is_user_visible(EventKind.ERROR_RAISED)


def test_成员状态与门禁明细不进标准模式() -> None:
    """D-046：不显示成员状态、不展示审核意见。"""
    for kind in (
        EventKind.ROLE_STATE_CHANGED,
        EventKind.ROLE_TOOL_CALL,
        EventKind.GATE_REJECTED,
        EventKind.GATE_PASSED,
        EventKind.PHASE_STARTED,
        EventKind.RETRIEVAL_DONE,
    ):
        assert not is_user_visible(kind), f"{kind.value} 不该进标准模式"


def test_成员状态只在调试模式出现() -> None:
    bus = EventBus(Redactor())
    std, dbg = StandardProjection(), DebugProjection()
    bus.subscribe(std.accept)
    bus.subscribe(dbg.accept)
    bus.emit(_ev(EventKind.ROLE_STATE_CHANGED, {"state": "运行中"}))
    assert bus.flush(5.0)
    bus.stop()

    assert not std.lines()
    assert len(dbg.lines()) == 1
    assert "role.state_changed" in dbg.lines()[0]


# ── seq 单点分配（D-075）──


def test_并发发射seq唯一且无空洞() -> None:
    """架构 §5.3 的核心：4 线程 × 2000 次无锁分配曾得 7997 唯一 + 3 重复。

    队列 + 泵线程方案下必须做到零重复、零空洞、零丢失。
    """
    bus = EventBus(Redactor())
    seqs: list[int] = []
    lock = threading.Lock()

    def sink(e: Event) -> None:
        with lock:
            seqs.append(e.seq)

    bus.subscribe(sink)
    n_threads, per_thread = 8, 500
    total = n_threads * per_thread

    def work(t: int) -> None:
        for i in range(per_thread):
            bus.emit(_ev(EventKind.PHASE_ADVANCED, {"t": t, "i": i}))

    threads = [threading.Thread(target=work, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bus.flush(10.0), "泵线程未排空"
    bus.stop()

    assert len(seqs) == total, "有事件丢失"
    assert len(set(seqs)) == total, "seq 有重复——回放出来的时间线会是假的"
    assert sorted(seqs) == list(range(1, total + 1)), "seq 有空洞"


def test_停止后发射被拒绝() -> None:
    """总线停了还有线程在发，必须显式报错而非静默丢弃。"""
    bus = EventBus(Redactor())
    bus.stop()
    try:
        bus.emit(_ev(EventKind.PHASE_ADVANCED))
    except RuntimeError:
        return
    raise AssertionError("总线停止后仍应拒绝发射")


def test_停止时排空已入队事件(tmp_path: Path) -> None:
    journal = JsonlJournal(tmp_path / "e.jsonl")
    bus = EventBus(Redactor(), journal)
    for _ in range(50):
        bus.emit(_ev(EventKind.PHASE_ADVANCED))
    bus.stop()  # 默认 drain=True
    assert len(journal.read_all()) == 50


# ── 并行门禁调度（D-049 / D-075）──


def test_并行组返回顺序恒等于入参顺序() -> None:
    """编排层据此做确定性归并，不受线程调度抖动影响。"""
    roles = (RoleId.STRUCTURE, RoleId.LANGUAGE, RoleId.COMPLIANCE, RoleId.DOC_ADVISOR)
    groups = tuple(GateGroup(r, (f"G{i}",)) for i, r in enumerate(roles))

    def work(g: GateGroup) -> Result[str]:
        # 让最后一组先完成，制造与入参顺序相反的完成顺序
        if g.owner is RoleId.DOC_ADVISOR:
            threading.Event().wait(0.05)
        return Result.ok(g.owner.value)

    out = run_groups(groups, work)
    assert [r.owner.value for r in out] == [r.value for r in roles]
    assert all(isinstance(r, GroupResult) and r.ok for r in out)


def test_空组表直接返回() -> None:
    """咨询档（D-049）不启门禁并行，此时不应建线程池。"""
    assert run_groups((), lambda g: Result.ok(0)) == ()


def test_门禁组保留owner与门禁编号() -> None:
    """组定义本身要能表达"这组门禁归谁负责"（D-049 责任映射）。"""
    g = GateGroup(RoleId.COMPLIANCE, ("G3", "G4", "G7"))
    assert g.owner is RoleId.COMPLIANCE
    assert len(g.gate_ids) == 3


def test_并行门禁真并行() -> None:
    """4 组各睡 0.2s，串行需 0.8s，并行应约 0.2s。"""
    import time

    groups = tuple(
        GateGroup(r, ("G1",))
        for r in (RoleId.STRUCTURE, RoleId.LANGUAGE, RoleId.COMPLIANCE, RoleId.DOC_ADVISOR)
    )
    started = time.monotonic()
    run_groups(groups, lambda g: _sleep_result(0.2))
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, f"未并行，耗时 {elapsed:.2f}s"


def _sleep_result(sec: float) -> Result[int]:
    threading.Event().wait(sec)
    return Result.ok(1)
