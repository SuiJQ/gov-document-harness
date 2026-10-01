# 本文件放宽 reportAny：往返测试必须处理 `json.loads` 返回的 `Any`，
# 那是 JSON 的本质而非缺陷。已在读取侧用 `_str` / `_int` / `_field` 逐值收窄，
# 收窄失配会断言失败，不会静默通过。
# 之所以用文件级注释而非 pyproject 的 overrides：实测 overrides 里写
# reportAny = "none" 对该规则无效，只有行内/文件级抑制有效。
# pyright: reportAny=false
"""JSON 往返测试（第 1b 步判据）。

**为什么这是硬判据而不是可选**：断点续跑就是把 `RunState`
序列化成 JSON 存盘、重启后再读回来（D-064）。
"能存能读"听起来是显然的，实际上是所有值对象里最容易出问题的地方——
因为 `StrEnum`、嵌套 `dataclass`、`dict` 键类型、`tuple` vs `list`
每一项都会在某一版悄悄变形。

**最危险的不是报错，是不报错地变形**：
`tuple` 读回来变 `list`，代码照跑，只是"不可变"悄悄没了；
`GateId` 读回来变裸字符串，比对时 `==` 恒为 False，门禁静默失效。
所以往返后必须做**结构等价断言**，而不是只断言"不抛异常"。
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, cast

from zhengtong.domain.artifact import ArtifactKind, ArtifactRef
from zhengtong.domain.dcard import DCardFieldId, Provenance
from zhengtong.domain.errors import ErrorCode
from zhengtong.domain.event import Audience, Event, EventKind, Level, Span, make_call_id
from zhengtong.domain.ids import ArtifactId, RunId, make_run_id
from zhengtong.domain.phase import PhaseId, PhaseState
from zhengtong.domain.role import RoleId, RoleState
from zhengtong.domain.run_state import GateRecord, RunState, new_run_state
from zhengtong.domain.task import TaskSpec, UserFile
from zhengtong.domain.tier import TierId


def _to_jsonable(obj: object) -> object:
    """把领域对象转成可 `json.dumps` 的结构。

    显式手写而非用通用 encoder：通用方案要么依赖第三方库，
    要么得给每个类注册钩子——都会在 L0 引入不该有的依赖。
    手写的另一个好处是**转换规则看得见**，
    哪一处的键是枚举、哪一处转成字符串，一眼可知。
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            _str(f.name): _to_jsonable(getattr(obj, _str(f.name))) for f in dataclasses.fields(obj)
        }
    # 注意：**不能**写 `isinstance(obj, dict[object, object])` 来收窄——
    # Python 运行时禁止 isinstance 传参数化泛型（3.11 抛 TypeError
    # "isinstance() argument 2 cannot be a parameterized generic"），
    # 与 D-085 是同一族限制。故先裸判类型，再经中间变量显式标注。
    if isinstance(obj, dict):
        mapping = cast("dict[object, object]", obj)
        return {_str(k): _to_jsonable(v) for k, v in mapping.items()}
    if isinstance(obj, tuple | list):
        seq = cast("tuple[object, ...] | list[object]", obj)
        return [_to_jsonable(v) for v in seq]
    if isinstance(
        obj,
        RoleId
        | TierId
        | PhaseId
        | PhaseState
        | RoleState
        | EventKind
        | Audience
        | Level
        | ArtifactKind
        | DCardFieldId
        | Provenance,
    ):
        return obj.value
    return obj


#: JSON 解出来的对象类型。`json.loads` 官方返回 `Any`，
#: 不收窄的话整条读取链的类型信息全断——检查器会在此之后
#: 一路报 `reportAny`，等于把测试的类型安全全部放弃。
#: 这里显式承认"这是 JSON 对象"并集中在一处做转型，
#: 好过让 `Any` 传染到每个断言。
# JSON 解出来的值本质是 `Any`（`json.loads` 的官方返回类型）。
# 这里集中标注一次：链上每一处取值都过 `_str` / `_int` / `_field`
# 收窄，所以 `Any` 不会传染到断言里——定点豁免是有依据的，不是图省事。
_Json = dict[str, Any]  # pyright: ignore[reportExplicitAny]


def _obj(value: object) -> _Json:
    """断言并收窄为一个 JSON 对象。

    值仍是 `Any`（JSON 本就异构），但**键**被标为 `str`，
    于是 `for k in data` 这类遍历不再丢类型。
    下游取值统一过 `_str` / `_int` 收窄，不直接用裸值。
    """
    assert isinstance(value, dict), f"期望 JSON 对象，实际 {type(value).__name__}"
    return cast("_Json", value)


def _field(obj: _Json, key: str) -> object:
    """取一个子对象并收窄。键名写错时立刻报错，而不是静默拿到 None。"""
    assert key in obj, f"缺字段：{key}"
    return obj[key]


def _str(value: object) -> str:
    """把 JSON 值收窄为字符串。类型不符即断言失败，不静默通过。"""
    assert isinstance(value, str), f"期望字符串，实际 {type(value).__name__}"
    return value


def _int(value: object) -> int:
    assert isinstance(value, int), f"期望整数，实际 {type(value).__name__}"
    return value


def _roundtrip(state: RunState) -> RunState:
    """序列化再反序列化。"""
    raw = json.dumps(_to_jsonable(state), ensure_ascii=False)
    data = _obj(cast("object", json.loads(raw)))
    roles = {RoleId(_str(k)): RoleState(_str(v)) for k, v in _obj(data["role_states"]).items()}
    gates = {
        k: GateRecord(
            gate=_str(_field(v, "gate")),
            passed=bool(_field(v, "passed")),
            attempt=_int(_field(v, "attempt")),
            evidence=tuple(_str(x) for x in cast("list[object]", _field(v, "evidence"))),
            rejection_reason=_str(_field(v, "rejection_reason")),
        )
        for k, v in _obj(data["gates"]).items()
    }
    arts = {
        k: ArtifactRef(
            id=ArtifactId(v["id"]),
            kind=ArtifactKind(v["kind"]),
            path=v["path"],
            version=v["version"],
        )
        for k, v in _obj(data["artifacts"]).items()
    }
    task_data = _obj(data["task"])
    task = TaskSpec(
        tier=TierId(_str(_field(task_data, "tier"))),
        requirement=_str(_field(task_data, "requirement")),
        user_files=tuple(
            UserFile(
                name=_str(f["name"]),
                size=_int(f["size"]),
                suffix=_str(f["suffix"]),
            )
            for f in map(_obj, cast("list[object]", task_data["user_files"]))
        ),
    )
    return RunState(
        schema_version=_int(_field(data, "schema_version")),
        run_id=RunId(_str(_field(data, "run_id"))),
        task=task,
        phase=PhaseId(_str(_field(data, "phase"))),
        phase_state=PhaseState(_str(_field(data, "phase_state"))),
        role_states=roles,
        artifacts=arts,
        gates=gates,
        completed_call_ids=tuple(
            _str(v) for v in cast("list[object]", _field(data, "completed_call_ids"))
        ),
        last_seq=_int(_field(data, "last_seq")),
        last_error=_str(_field(data, "last_error")),
    )


def _sample_state() -> RunState:
    task = TaskSpec(
        tier=TierId.PRO,
        requirement="就 droughts 写个通知\n第二行",
        user_files=(UserFile(name="材料.txt", size=1234, suffix=".txt"),),
    )
    state = new_run_state(make_run_id("20261001-143000", "ab12"), task)
    state = state.with_phase(PhaseId.GATING, PhaseState.GATED)
    state = state.set_role_state(RoleId.COMPLIANCE, RoleState.RUNNING)
    state = state.mark_completed("call-0001")
    state = state.mark_completed("call-0002")
    state = state.put_gate(
        GateRecord(
            gate="G2",
            passed=False,
            attempt=1,
            evidence=("第一章第3段",),
            rejection_reason="事实三态缺失",
        )
    )
    state = state.put_artifact(
        ArtifactRef(
            id=ArtifactId("draft-v1"), kind=ArtifactKind.DRAFT, path="drafts/main-v1.txt", version=2
        )
    )
    return state.with_error(ErrorCode.GATE_FAILED, "G2 退回")


class TestRunState往返:
    def test_结构等价(self) -> None:
        s = _sample_state()
        back = _roundtrip(s)
        assert back == s, "往返后结构不等价"

    def test_中文不转义(self) -> None:
        """中文要原样保留——转义了虽然能读回来，但日志里全是 \\uXXXX。"""
        raw = json.dumps(_to_jsonable(_sample_state()), ensure_ascii=False)
        assert "就 droughts 写个通知" in raw
        assert "\\u" not in raw

    def test_枚举读回后仍是枚举(self) -> None:
        """最危险的静默变形：读回裸字符串后 `==` 恒 False，门禁静默失效。"""
        back = _roundtrip(_sample_state())
        assert back.phase is PhaseId.GATING
        assert back.task.tier is TierId.PRO
        assert isinstance(back.role_states[RoleId.COMPLIANCE], RoleState)

    def test_元组读回后需重新转元组(self) -> None:
        """JSON 没有元组。读取侧负责还原——此处确认这是读取侧的职责。"""
        raw = _obj(
            cast(
                "object", json.loads(json.dumps(_to_jsonable(_sample_state()), ensure_ascii=False))
            )
        )
        assert isinstance(_field(raw, "completed_call_ids"), list)
        assert isinstance(_roundtrip(_sample_state()).completed_call_ids, tuple)

    def test_已完成调用集保持不变(self) -> None:
        s = _sample_state()
        back = _roundtrip(s)
        assert back.is_call_completed("call-0001")
        assert not back.is_call_completed("call-9999")
        assert back.completed_call_ids == s.completed_call_ids

    def test_Dcard往返后字段齐(self) -> None:
        s = _sample_state()
        back = _roundtrip(s)
        assert set(back.dcard.by_id) == set(s.dcard.by_id)
        assert back.dcard.missing_fields == s.dcard.missing_fields


class Test事件序列化:
    def test_事件可JSON化且读回等价(self) -> None:
        ev = Event(
            seq=7,
            kind=EventKind.GATE_FAILED,
            run_id=make_run_id("20261001-143000", "ab12"),
            phase="gating",
            level=Level.WARN,
            audience=Audience.USER,
            call_id="abc123",
            span=Span(role="compliance", gate="G2", round_no=1),
            data={"gate": "G2", "attempt": 1},
        )
        raw = json.dumps(_to_jsonable(ev), ensure_ascii=False)
        back = json.loads(raw)
        assert back["kind"] == "gate.failed"
        assert back["audience"] == "user"
        assert back["span"]["role"] == "compliance"
        assert back["data"]["gate"] == "G2"

    def test_默认载荷为空字典(self) -> None:
        ev = Event(seq=1, kind=EventKind.RUN_STARTED, run_id=RunId("r"), phase="intake")
        assert ev.data == {}

    def test_标准模式不见DEBUG事件(self) -> None:
        """D-046：标准模式只显示进度与成果，不显示成员状态。"""
        ev = Event(
            seq=1,
            kind=EventKind.ROLE_STATE_CHANGED,
            run_id=RunId("r"),
            phase="intake",
            audience=Audience.DEBUG,
        )
        assert not ev.is_user_visible


class Testcall_id语义:
    def test_同一件事得到同一id(self) -> None:
        """D-076：call_id 是副作用的语义身份，重试后必须不变。"""
        a = make_call_id("gate.check", "run-1", "gating", gate="G2")
        b = make_call_id("gate.check", "run-1", "gating", gate="G2")
        assert a == b

    def test_不同门禁不同id(self) -> None:
        a = make_call_id("gate.check", "run-1", "gating", gate="G2")
        b = make_call_id("gate.check", "run-1", "gating", gate="G7")
        assert a != b

    def test_不同运行不同id(self) -> None:
        a = make_call_id("gate.check", "run-1", "gating", gate="G2")
        b = make_call_id("gate.check", "run-2", "gating", gate="G2")
        assert a != b

    def test_参数顺序不影响结果(self) -> None:
        a = make_call_id("x", "r", "p", alpha="1", beta="2")
        b = make_call_id("x", "r", "p", beta="2", alpha="1")
        assert a == b

    def test_id不含轮次(self) -> None:
        """call_id 与轮次无关——否则续跑时轮次变了就认不出同一件事。"""
        a = make_call_id("llm.call", "run-1", "drafting", prompt="p")
        b = make_call_id("llm.call", "run-1", "drafting", prompt="p")
        assert a == b
