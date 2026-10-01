"""`RunState` 的 JSON 编解码。

## 归属：为什么在适配层而不在领域层

架构要求**领域层零依赖**：只用 `abc/collections/dataclasses/enum/
hashlib/pathlib/typing` 那几样，不许 `json`。序列化本身就是
I/O 关注点，属适配层——`kernel/journal.py` 里的 json 用法同理。
故 `RunState` 这个纯领域对象由**本模块**负责与 JSON 之间转换，
领域层自己不知道 JSON 存在。

## 为什么单独一个模块

断点续跑（D-027）要求状态能存进 JSON 再读回来。
`RunState` 含 `RoleId`/`PhaseId` 等枚举，还有
`dict[RoleId, RoleState]`、`dict[DCardFieldId, DCardField]` 这样的
**带枚举键的字典**——`json.dumps` 不认枚举键，
`dataclasses.asdict` 也不会把键转成字符串。必须显式写。

## 显式枚举而非通用 encoder

与 `journal.py` 同一理由：状态文件要长期存在，字段名与结构必须
稳定可 grep、可 diff。`asdict` 的输出随字段增删自动变形，
旧状态文件会在某次改动后集体失效，而那时没人想得起来是哪个改动干的。

## `schema_version` 是迁移链的起点（D-066）

读到的版本高于当前程序时**拒绝加载**，不"尽力而为"：
把新版的字段按旧版解释出来的状态，比加载失败危险得多。
"""

# 本文件放宽 Unknown 类规则，理由同 domain/errors.py 的 D-085：
# `Result.ok(...)` 返回 `Result[Any]`（三方案实测后确认唯一可行解）。
# 只影响静态提示，不影响运行行为。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false

from __future__ import annotations

import json
from typing import TypeVar, cast

from ..domain.artifact import ArtifactKind, ArtifactRef
from ..domain.dcard import DCard, DCardField, DCardFieldId, Provenance, empty_dcard
from ..domain.errors import ErrorCode, Result
from ..domain.ids import ArtifactId, RunId
from ..domain.phase import PhaseId, PhaseState
from ..domain.role import RoleId, RoleState
from ..domain.run_state import GateRecord, RunState
from ..domain.task import TaskSpec
from ..domain.tier import TierId

#: 当前 schema 版本。**改结构就 +1**，并在 `from_json` 里加迁移分支。
SCHEMA_VERSION = 1


def dumps(state: RunState) -> str:
    """序列化成文本。`ensure_ascii=False` —— 状态里有中文，
    转义成 \\uXXXX 会让用户用记事本打开时完全不可读。"""
    return json.dumps(_to(state), ensure_ascii=False, indent=2)


def loads(text: str) -> Result[RunState]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return Result.fail(ErrorCode.RUN_RESUMED, f"状态文件不是合法 JSON：{exc}")
    if not isinstance(raw, dict):
        return Result.fail(ErrorCode.RUN_RESUMED, "状态文件顶层不是对象")
    return from_json(cast("dict[str, object]", raw))


def _to(state: RunState) -> dict[str, object]:
    """状态 → 可序列化结构。**字段顺序即写入顺序**，便于 diff。"""
    return {
        "schema_version": state.schema_version,
        "run_id": state.run_id,
        "task": {
            "tier": state.task.tier.value,
            "requirement": state.task.requirement,
            "user_files": [
                {"name": f.name, "size": f.size, "suffix": f.suffix} for f in state.task.user_files
            ],
        },
        "phase": state.phase.value,
        "phase_state": state.phase_state.value,
        "dcard": {
            "by_id": {
                k.value: {
                    "label": f.label,
                    "value": f.value,
                    "provenance": f.provenance.value,
                }
                for k, f in state.dcard.by_id.items()
            },
        },
        "role_states": {k.value: v.value for k, v in state.role_states.items()},
        "artifacts": {
            k: {
                "id": v.id,
                "kind": v.kind.value,
                "path": v.path,
                "version": v.version,
            }
            for k, v in state.artifacts.items()
        },
        "gates": {
            k: {
                "gate": v.gate,
                "passed": v.passed,
                "attempt": v.attempt,
                "evidence": list(v.evidence),
                "rejection_reason": v.rejection_reason,
            }
            for k, v in state.gates.items()
        },
        "completed_call_ids": list(state.completed_call_ids),
        "last_seq": state.last_seq,
        "last_error": state.last_error,
    }


def from_json(raw: dict[str, object]) -> Result[RunState]:
    """可序列化结构 → 状态。**任何一步不对都返回 fail，不抛异常**——
    状态损坏不该把程序带崩，用户需要看到"这个 run 读不了"而不是栈回溯。"""
    version = raw.get("schema_version")
    if not isinstance(version, int):
        return Result.fail(ErrorCode.RUN_RESUMED, "状态文件缺 schema_version")
    if version > SCHEMA_VERSION:
        return Result.fail(
            ErrorCode.RUN_RESUMED,
            f"状态文件版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}，拒绝加载",
        )
    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return Result.fail(ErrorCode.RUN_RESUMED, "状态文件缺 run_id")

    return Result.ok(
        RunState(
            schema_version=version,
            run_id=RunId(run_id),
            task=_task_from(raw.get("task")),
            phase=_enum(PhaseId, raw.get("phase"), PhaseId.INTAKE),
            phase_state=_enum(PhaseState, raw.get("phase_state"), PhaseState.PENDING),
            dcard=_dcard_from(raw.get("dcard")),
            role_states={
                _enum(RoleId, k, RoleId.TEAM_LEAD): _enum(RoleState, v, RoleState.IDLE)
                for k, v in _as_dict(raw.get("role_states")).items()
            },
            artifacts={k: _ref_from(v) for k, v in _as_dict(raw.get("artifacts")).items()},
            gates={k: _gate_from(v) for k, v in _as_dict(raw.get("gates")).items()},
            completed_call_ids=tuple(
                str(x) for x in cast("list[object]", raw.get("completed_call_ids") or [])
            ),
            last_seq=int(cast("int", raw.get("last_seq") or 0)),
            last_error=str(raw.get("last_error") or ""),
        )
    )


# ── 子结构 ──


def _as_dict(value: object) -> dict[str, object]:
    """只接受 dict，其余一律当空。**不抛异常**：
    状态文件是外部输入，畸形数据不该中断恢复。"""
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in cast("dict[object, object]", value).items()}


_E = TypeVar("_E")


def _enum(enum_cls: type[_E], value: object, default: _E) -> _E:
    """枚举反序列化。**认不出的值回落默认**而不是抛异常。

    回落而非报错是有意的取舍：状态文件里一个陌生枚举值不该让整个
    run 读不出来——那会让用户丢掉已完成的工作。真正危险的
    「版本比我新」已由 `from_json` 按 `schema_version` 挡住，不靠这里兜。
    """
    if value is None:
        return default
    try:
        # `type[_E]` 只保证是个类，不保证构造签名是单参数——
        # 实际传入的六个枚举都是 StrEnum，故此处安全。
        return enum_cls(value)  # pyright: ignore[reportCallIssue]
    except ValueError:
        return default


def _task_from(raw: object) -> TaskSpec:
    from ..domain.task import UserFile

    d = _as_dict(raw)
    files = cast("list[object]", d.get("user_files") or [])
    return TaskSpec(
        tier=_enum(TierId, d.get("tier"), TierId.CONSULT),
        requirement=str(d.get("requirement") or ""),
        user_files=tuple(
            UserFile(
                name=str(_as_dict(f).get("name") or ""),
                size=int(cast("int", _as_dict(f).get("size") or 0)),
                suffix=str(_as_dict(f).get("suffix") or ""),
            )
            for f in files
        ),
    )


def _dcard_from(raw: object) -> DCard:
    d = _as_dict(raw)
    base = empty_dcard()
    fields = _as_dict(d.get("by_id"))
    if not fields:
        return base
    # DCard 没有独立的 id 字段：它的身份就是这 9 个字段的键集合
    return DCard(
        by_id={DCardFieldId(k): _field_from(v, DCardFieldId(k)) for k, v in fields.items()}
    )


def _field_from(raw: object, fid: DCardFieldId) -> DCardField:
    d = _as_dict(raw)
    value = d.get("value")
    return DCardField(
        id=fid,
        label=str(d.get("label") or ""),
        value=None if value is None else str(value),
        provenance=_enum(Provenance, d.get("provenance"), Provenance.USER),
    )


def _ref_from(raw: object) -> ArtifactRef:
    d = _as_dict(raw)
    return ArtifactRef(
        id=ArtifactId(str(d.get("id") or "")),
        kind=_enum(ArtifactKind, d.get("kind"), ArtifactKind.NOTE),
        path=str(d.get("path") or ""),
        version=int(cast("int", d.get("version") or 1)),
    )


def _gate_from(raw: object) -> GateRecord:
    d = _as_dict(raw)
    return GateRecord(
        gate=str(d.get("gate") or ""),
        passed=bool(d.get("passed")),
        attempt=int(cast("int", d.get("attempt") or 0)),
        evidence=tuple(str(x) for x in cast("list[object]", d.get("evidence") or [])),
        rejection_reason=str(d.get("rejection_reason") or ""),
    )
