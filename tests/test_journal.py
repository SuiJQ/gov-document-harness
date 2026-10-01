"""`kernel/journal.py` 测试 —— 断点续跑与日志追溯的物理基础。

## 为什么判据是「坏行不得毁掉好行」

架构 §5.2 称 journal 是「断点续跑（D-027）与日志追溯的**物理基础**」，
§8.6 承诺「内核是状态机而不是脚本，所以续跑天然是安全的」。

而**续跑只在崩溃之后才会用到**。追加写不是原子的，
进程被强杀（断电、OOM、Ctrl-C）时末行往往只写了一半——
这是崩溃日志的**常态**，不是边缘情况。

审计 D-123 查出：原实现一读到半行就抛 `JSONDecodeError`，
**前 N-1 条完好记录也一并读不出来**。
即承诺在最需要它的时刻失效。故判据定为：

> 一条坏行只丢它自己。前面每一行都必须保住。
"""

# 本文件放宽 reportAny：`json.loads` 官方返回 `Any`，这是 JSON 的固有性质
# 而非缺陷。与 `src/zhengtong/kernel/journal.py` 用同一手法（该处已有先例）。
# 收紧的做法（自写收窄助手）在下面已用 `isinstance` + `cast` 就地完成。
# pyright: reportAny=false
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

import pytest

from zhengtong.domain.event import Audience, Event, EventKind, Level, Span
from zhengtong.domain.ids import RunId
from zhengtong.domain.phase import PhaseId
from zhengtong.kernel.journal import JsonlJournal

# ═══ 往返 ═══


def test_往返一致(tmp_path: Path) -> None:
    e = _完整事件()
    j = JsonlJournal(tmp_path / "ev.jsonl")
    _ = j.append(e)
    got = j.read_all()
    assert got == (e,)


def test_多条往返保序(tmp_path: Path) -> None:
    j = JsonlJournal(tmp_path / "ev.jsonl")
    原 = [_事件(i) for i in range(1, 6)]
    for e in 原:
        _ = j.append(e)
    assert j.read_all() == tuple(原)


def test_空文件读出空(tmp_path: Path) -> None:
    p = tmp_path / "ev.jsonl"
    _ = p.write_text("", encoding="utf-8")
    assert JsonlJournal(p).read_all() == ()


def test_不存在的文件读出空(tmp_path: Path) -> None:
    assert JsonlJournal(tmp_path / "没有.jsonl").read_all() == ()


def test_自动建父目录(tmp_path: Path) -> None:
    """`system/` 可能还不存在——不该要求调用方先建好。"""
    j = JsonlJournal(tmp_path / "深" / "层" / "ev.jsonl")
    _ = j.append(_事件(1))
    assert len(j.read_all()) == 1


def test_重复回放不破坏历史(tmp_path: Path) -> None:
    """**只追加、不改写**（架构 §5.2）：重复回放同一段不会覆盖历史。"""
    j = JsonlJournal(tmp_path / "ev.jsonl")
    _ = j.append(_事件(1))
    _ = j.append(_事件(1))
    assert [e.seq for e in j.read_all()] == [1, 1]


# ═══ D-123：坏行不得毁掉好行 ═══


def test_崩溃半行不影响完好记录(tmp_path: Path) -> None:
    """**本批最严重的缺陷**。原实现读到半行即抛异常，
    前面 N-1 条完好记录也读不出来。"""
    j, p = _三条(tmp_path)
    _ = p.write_text(_读(p) + '{"seq":4,"kind":"phase.st', encoding="utf-8")
    got = j.read_all()
    assert len(got) == 3, f"半行导致历史丢失，只剩 {len(got)} 条"
    assert [e.seq for e in got] == [1, 2, 3]


def test_半行在中间也不影响其余(tmp_path: Path) -> None:
    """坏行可能在中间，不一定在末尾。"""
    j, p = _三条(tmp_path)
    坏 = json.dumps({"seq": 99, "kind": "phase.st"})
    _ = p.write_text(_读(p) + 坏 + "\n", encoding="utf-8")
    assert len(j.read_all()) == 3


def test_非对象JSON行被跳过(tmp_path: Path) -> None:
    j, p = _三条(tmp_path)
    _ = p.write_text(_读(p) + "[1,2,3]\n", encoding="utf-8")
    assert len(j.read_all()) == 3


def test_缺字段行被跳过(tmp_path: Path) -> None:
    """缺 `run_id` 的行。严格解析仍在（不因容错而放过人为坏数据）。"""
    j, p = _三条(tmp_path)
    _ = p.write_text(
        _读(p) + json.dumps({"seq": 9, "kind": "phase.started"}) + "\n",
        encoding="utf-8",
    )
    assert len(j.read_all()) == 3


def test_空行被跳过(tmp_path: Path) -> None:
    j, p = _三条(tmp_path)
    _ = p.write_text(_读(p) + "\n\n\n", encoding="utf-8")
    assert len(j.read_all()) == 3


def test_损坏必须留痕(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """**静默跳过等于把数据损坏藏起来**——用户以为续跑正常，实际丢了事件。"""
    j, p = _三条(tmp_path)
    _ = p.write_text(_读(p) + '{"seq":4,', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        _ = j.read_all()
    assert any("损坏" in r.message for r in caplog.records), "损坏行没有留痕"


def test_未知kind被跳过(tmp_path: Path) -> None:
    """内容包升级后，旧版本的事件 kind 可能已不存在。

    这类行**不是崩溃半行**，但同样不该让整份日志读不出来——
    否则一次内容包升级就抹掉全部历史。
    """
    j, p = _三条(tmp_path)
    _ = p.write_text(
        _读(p)
        + json.dumps({"seq": 4, "kind": "role.telepathy", "run_id": "r", "phase": "intake"})
        + "\n",
        encoding="utf-8",
    )
    assert len(j.read_all()) == 3


def test_seq有缺口但事件仍可用(tmp_path: Path) -> None:
    """坏行跳过后 seq 不连续——这**无害**：续跑走 `RunState` 不靠 seq，
    seq 只用于持久化与调试显示。此条锁住该前提，防止将来有人靠 seq 判进度。"""
    j, p = _三条(tmp_path)
    _ = p.write_text(
        _读(p) + json.dumps({"seq": 4, "kind": "坏"}) + "\n",
        encoding="utf-8",
    )
    assert [e.seq for e in j.read_all()] == [1, 2, 3]


# ═══ 中文与可 grep 稳定性 ═══


def test_中文按原样落盘不转义(tmp_path: Path) -> None:
    """`ensure_ascii=False`：日志要能直接 grep 中文，
    转义成 `\\uXXXX` 就没法肉眼排查了。"""
    p = tmp_path / "ev.jsonl"
    e = _事件(1)
    e = Event(
        seq=e.seq,
        kind=e.kind,
        run_id=e.run_id,
        phase=e.phase,
        level=e.level,
        audience=e.audience,
        call_id=e.call_id,
        span=e.span,
        data={"名称": "关于做好旱情应对工作的通知"},
    )
    _ = JsonlJournal(p).append(e)
    assert "旱情" in _读(p)


def test_每行一个JSON对象(tmp_path: Path) -> None:
    """JSONL 格式本身：行可独立解析，故能容忍半行。"""
    p = tmp_path / "ev.jsonl"
    j = JsonlJournal(p)
    for i in range(1, 4):
        _ = j.append(_事件(i))
    for line in _读(p).splitlines():
        if line.strip():
            json.loads(line)


def test_字段命名稳定可grep(tmp_path: Path) -> None:
    """显式枚举而非通用 encoder：字段名必须稳定，否则按文档 grep 排障会空手而归。"""
    p = tmp_path / "ev.jsonl"
    _ = JsonlJournal(p).append(_完整事件())
    原始 = json.loads(_读(p).strip())
    assert isinstance(原始, dict), "前提：单行应是 JSON 对象"
    assert set(cast("dict[str, object]", 原始)) == {
        "seq",
        "kind",
        "run_id",
        "phase",
        "level",
        "audience",
        "call_id",
        "span",
        "data",
    }


# ═══ 字段还原的严格性 ═══


def test_span完整往返(tmp_path: Path) -> None:
    e = _完整事件()
    j = JsonlJournal(tmp_path / "ev.jsonl")
    _ = j.append(e)
    got = j.read_all()[0]
    源, 回 = e.span, got.span
    assert 回 is not None and 源 is not None
    assert 回.role == 源.role
    assert 回.gate == 源.gate
    assert 回.round_no == 源.round_no


def test_无span时往返(tmp_path: Path) -> None:
    e = Event(
        seq=1,
        kind=EventKind.PHASE_STARTED,
        run_id=RunId("r"),
        phase=PhaseId.INTAKE,
        span=None,
    )
    j = JsonlJournal(tmp_path / "ev.jsonl")
    _ = j.append(e)
    assert j.read_all()[0].span is None


def test_嵌套data往返(tmp_path: Path) -> None:
    e = _事件(1)
    e = Event(
        seq=e.seq,
        kind=e.kind,
        run_id=e.run_id,
        phase=e.phase,
        level=e.level,
        audience=e.audience,
        call_id=e.call_id,
        span=e.span,
        data={"请求体": {"messages": [{"role": "user", "content": "写个通知"}]}},
    )
    j = JsonlJournal(tmp_path / "ev.jsonl")
    _ = j.append(e)
    assert j.read_all()[0].data == e.data


def test_可选字段缺省时仍可还原(tmp_path: Path) -> None:
    """`level` / `audience` / `call_id` 是可选的，缺失时给默认值而非报错。"""
    p = tmp_path / "ev.jsonl"
    _ = p.write_text(
        json.dumps({"seq": 1, "kind": "phase.started", "run_id": "r", "phase": "intake"}) + "\n",
        encoding="utf-8",
    )
    got = JsonlJournal(p).read_all()[0]
    assert got.level is Level.INFO
    assert got.audience is Audience.USER
    assert got.call_id == ""


# ═══ 夹具 ═══


def _读(p: Path) -> str:
    """统一读法。刻意不让 `Path.read_text` 的返回值直接进断言——
    既避免未用结果警告，也让"读到的到底是什么"在测试里显式。"""
    return p.read_text(encoding="utf-8")


def _事件(seq: int) -> Event:
    return Event(seq=seq, kind=EventKind.PHASE_STARTED, run_id=RunId("r-1"), phase=PhaseId.INTAKE)


def _完整事件() -> Event:
    return Event(
        seq=7,
        kind=EventKind.ARTIFACT_READY,
        run_id=RunId("r-完整"),
        phase=PhaseId.DRAFTING,
        level=Level.WARN,
        audience=Audience.USER,
        call_id="call-abc",
        span=Span(role="compliance", gate="G2", round_no=3),
        data={"名称": "正文初稿", "字数": 2408},
    )


def _三条(tmp_path: Path) -> tuple[JsonlJournal, Path]:
    p = tmp_path / "ev.jsonl"
    j = JsonlJournal(p)
    for i in (1, 2, 3):
        _ = j.append(_事件(i))
    return j, p


def test_span字段为null时还原为None(tmp_path: Path) -> None:
    """`span` 在 JSON 里显式为 `null` 时应还原为 `None`，
    而不是把 `None` 当字符串 `"None"` 塞进去。"""
    p = tmp_path / "ev.jsonl"
    _ = p.write_text(
        json.dumps(
            {
                "seq": 1,
                "kind": "phase.started",
                "run_id": "r",
                "phase": "intake",
                "span": {"role": None, "gate": None, "round_no": None},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    got = JsonlJournal(p).read_all()[0]
    assert got.span is not None
    assert got.span.role is None
    assert got.span.gate is None
    assert got.span.round_no is None


def test_seq类型不对的行被跳过而非崩(tmp_path: Path) -> None:
    """`seq` 不是整数（人为坏数据，非崩溃半行）时，该行被跳过而非崩掉整个读取。

    这就是"容错不放松严格"的落地形态：**坏行只丢它自己**。
    """
    j, p = _三条(tmp_path)
    _ = p.write_text(
        _读(p)
        + json.dumps({"seq": "不是数字", "kind": "phase.started", "run_id": "r", "phase": "intake"})
        + "\n",
        encoding="utf-8",
    )
    assert [e.seq for e in j.read_all()] == [1, 2, 3]
