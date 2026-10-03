"""`state_codec` 测试 —— 断点续跑（D-027）的生死线。

## 为什么这个文件要单独测、且测得这么细

覆盖率基线显示本模块只有 36%，且**未覆盖的恰是主入口**
（`dumps` / `loads` / `from_json` 主体）。

而它承载断点续跑：`RunState` 含 `dict[RoleId, RoleState]`、
`dict[DCardFieldId, DCardField]` 这类**带枚举键的字典**，
`json.dumps` 不认、`dataclasses.asdict` 也不转键，必须显式手写。

**一个畸形的状态文件就能让程序崩在启动**——而"崩"恰恰是
断点续跑最不能接受的结果。故本文件对每一处外部输入转换都设了用例。
"""

from __future__ import annotations

import json

from zhengtong.adapters import state_codec as sc

#: 程序支持的版本号——**唯一来源是 `domain`**，state_codec 不再另定义（D-252）
from zhengtong.domain import SCHEMA_VERSION as _当前版本
from zhengtong.domain.artifact import ArtifactKind, ArtifactRef
from zhengtong.domain.dcard import DCard, DCardField, DCardFieldId, Provenance, empty_dcard
from zhengtong.domain.errors import ErrorCode
from zhengtong.domain.ids import ArtifactId, RunId
from zhengtong.domain.phase import PhaseId, PhaseState
from zhengtong.domain.role import RoleId, RoleState
from zhengtong.domain.run_state import GateRecord, RunState
from zhengtong.domain.task import TaskSpec, UserFile
from zhengtong.domain.tier import TierId

# ═══ 往返：正常路径必须无损 ═══


def test_往返一致() -> None:
    """完整状态存下来再读回来，必须完全相同。"""
    st = _state()
    back = sc.loads(sc.dumps(st))
    assert back.is_ok, f"往返失败：{back.error}"
    got = back.unwrap()
    assert got.run_id == st.run_id
    assert got.phase == st.phase
    assert got.phase_state == st.phase_state
    assert got.task.tier == st.task.tier
    assert got.task.requirement == st.task.requirement
    assert got.artifacts == st.artifacts
    assert got.gates == st.gates
    assert got.completed_call_ids == st.completed_call_ids
    assert got.last_seq == st.last_seq
    assert got.last_error == st.last_error


def test_往返保留角色状态() -> None:
    """角色状态是**带枚举键的字典**，必须按 key 复原——
    丢了这个，续跑后所有角色都变 IDLE，会重跑已完成的阶段。"""
    st = _state()
    back = sc.loads(sc.dumps(st))
    assert back.is_ok
    assert back.unwrap().role_states == st.role_states


def test_往返保留双轴台账() -> None:
    """D-Card 是"严禁编造"的载体，丢失等于编造内容无人可查。"""
    st = _state()
    back = sc.loads(sc.dumps(st))
    assert back.is_ok
    assert back.unwrap().dcard == st.dcard


def test_中文不转义() -> None:
    """用户用记事本打开状态文件，中文被转成 \\uXXXX 就完全不可读。"""
    st = _state()
    assert "就旱情写个通知" in sc.dumps(st)
    assert "\\u" not in sc.dumps(st)


def test_输出是合法JSON() -> None:
    assert json.loads(sc.dumps(_state()))["schema_version"] == _当前版本


def test_字段顺序稳定便于diff() -> None:
    """状态文件长期存在，字段顺序变了会让每次提交都产生假 diff。"""
    first = sc.dumps(_state())
    second = sc.dumps(_state())
    assert first == second


# ═══ 缺陷 1：错误码语义说反了 ═══


def test_非法JSON用文件不可读而非已续跑() -> None:
    """**已证实的缺陷**。

    `RUN_RESUMED` 在 errors.py 的注释是「非错误，仅记录」，
    语义是"成功恢复了"。用它报解析失败，等于告诉用户
    "你的 run 已恢复"，而实际是**恢复失败并终止**——状态完全说反。
    """
    got = sc.loads("这不是 JSON")
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.FILE_UNREADABLE


def test_顶层非对象同样用文件不可读() -> None:
    got = sc.loads("[1, 2, 3]")
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.FILE_UNREADABLE


def test_版本过新时拒绝并说清原因() -> None:
    """把新版的字段按旧版解释出来，比加载失败危险得多（D-066）。"""
    got = sc.from_json({"schema_version": _当前版本 + 1, "run_id": "r"})
    assert got.is_error
    assert got.error is not None
    assert "高于当前程序支持" in got.error.detail


def test_缺schema_version拒绝() -> None:
    got = sc.from_json({"run_id": "r"})
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.FILE_UNREADABLE


def test_缺run_id拒绝() -> None:
    got = sc.from_json({"schema_version": 1})
    assert got.is_error


def test_缺run_id且为空串也拒绝() -> None:
    """空串 run_id 会让状态落到一个无名目录，用户根本找不到。"""
    assert sc.from_json({"schema_version": 1, "run_id": ""}).is_error


# ═══ 缺陷 2：未知 DCard 字段 id 会抛异常 ═══


def test_未知DCard字段不抛异常() -> None:
    """**已证实的缺陷**：`DCardFieldId(k)` 遇未知键抛 ValueError，
    调用链上无 try，直接冒泡出 `from_json`，
    违背了它自己承诺的「任何一步不对都返回 fail，不抛异常」。"""
    got = sc.from_json(
        {"schema_version": 1, "run_id": "r", "dcard": {"by_id": {"不存在的字段": "x"}}}
    )
    assert got.is_ok, f"仍抛异常：{got.error}"


def test_未知字段被丢弃而非整个run读不出() -> None:
    """不认识的键丢弃即可——让它把整个 run 读不出来，
    等于让用户丢掉已完成的工作。"""
    got = sc.from_json(
        {
            "schema_version": 1,
            "run_id": "r",
            "dcard": {"by_id": {"将来版本才有的字段": "x", DCardFieldId.D1.value: "旱情"}},
        }
    )
    assert got.is_ok
    assert got.unwrap().dcard.by_id.get(DCardFieldId.D1) is not None


# ═══ 缺陷 3：畸形数字抛异常 ═══


def test_文件大小非数字不抛() -> None:
    """**已证实的缺陷**：`int("abc")` 抛 ValueError，
    一个畸形状态文件就能让程序崩在启动。"""
    got = sc.from_json(
        {"schema_version": 1, "run_id": "r", "task": {"user_files": [{"name": "a", "size": "abc"}]}}
    )
    assert got.is_ok
    assert got.unwrap().task.user_files[0].size == 0


def test_last_seq非数字不抛() -> None:
    got = sc.from_json({"schema_version": 1, "run_id": "r", "last_seq": "abc"})
    assert got.is_ok
    assert got.unwrap().last_seq == 0


def test_产物版本非数字不抛() -> None:
    got = sc.from_json({"schema_version": 1, "run_id": "r", "artifacts": {"a": {"version": "abc"}}})
    assert got.is_ok
    assert got.unwrap().artifacts["a"].version == 1


def test_门禁次数非数字不抛() -> None:
    """`attempt` 关系到退回上限（D-050），读错会让门禁永不触发。"""
    got = sc.from_json({"schema_version": 1, "run_id": "r", "gates": {"G0": {"attempt": "abc"}}})
    assert got.is_ok
    assert got.unwrap().gates["G0"].attempt == 0


def test_数字字符串能救回() -> None:
    """`"128"` 与 `128` 等价，救回来比丢弃好。"""
    got = sc.from_json({"schema_version": 1, "run_id": "r", "last_seq": "42"})
    assert got.is_ok
    assert got.unwrap().last_seq == 42


def test_布尔值不被当成整数() -> None:
    """`isinstance(True, int)` 为真，不排除会得到看似合法的 seq=1。"""
    got = sc.from_json({"schema_version": 1, "run_id": "r", "last_seq": True})
    assert got.is_ok
    assert got.unwrap().last_seq == 0


# ═══ 其他畸形输入 ═══


def test_各分区不是字典时当空() -> None:
    got = sc.from_json(
        {
            "schema_version": 1,
            "run_id": "r",
            "task": "不是对象",
            "dcard": 5,
            "role_states": "也不是",
            "artifacts": [],
            "gates": None,
        }
    )
    assert got.is_ok


def test_未知枚举值回落默认而非报错() -> None:
    """状态里一个陌生枚举值不该让整个 run 读不出来。
    真正危险的「版本比我新」已由 schema_version 挡住。"""
    got = sc.from_json({"schema_version": 1, "run_id": "r", "phase": "将来才有的阶段"})
    assert got.is_ok
    assert got.unwrap().phase is PhaseId.INTAKE


def test_未知角色键回落默认() -> None:
    got = sc.from_json({"schema_version": 1, "run_id": "r", "role_states": {"未来角色": "运行中"}})
    assert got.is_ok
    # **已证实的缺陷**：原实现对未知键回落 TEAM_LEAD，
    # 于是 {"未来角色": "运行中"} 把主理人凭空置为运行中——
    # 续跑后系统在等一个永远不会来的角色，整个 run 卡死
    assert RoleId.TEAM_LEAD not in got.unwrap().role_states


def test_空文件返回错误而非崩() -> None:
    assert sc.loads("").is_error


def test_schema_version为当前值时正常加载() -> None:
    got = sc.from_json({"schema_version": _当前版本, "run_id": "r"})
    assert got.is_ok
    assert got.unwrap().schema_version == _当前版本


def test_空DCard加载() -> None:
    """`empty_dcard()` 的 9 个字段都不在时不该崩。"""
    got = sc.from_json({"schema_version": 1, "run_id": "r", "dcard": {"by_id": {}}})
    assert got.is_ok
    assert got.unwrap().dcard == empty_dcard()


def test_provenance保真() -> None:
    """provenance 是"这条信息哪来的"——编造内容靠它区分。"""
    st = _state()
    back = sc.loads(sc.dumps(st))
    assert back.is_ok
    got_fields = back.unwrap().dcard.by_id
    assert any(f.provenance is Provenance.LEAD_INFERRED for f in got_fields.values())


def test_门禁证据保真() -> None:
    """退回时的证据链，丢了就无法追溯是谁的锅（D-049）。"""
    st = _state()
    back = sc.loads(sc.dumps(st))
    assert back.is_ok
    for gate in back.unwrap().gates.values():
        assert gate.evidence


# ═══ 测试夹具 ═══


def _state() -> RunState:
    """一个字段全满的状态。**每个字段都非空**——
    空字段的往返不能证明序列化是对的。"""
    return RunState(
        schema_version=_当前版本,
        run_id=RunId("run-测试-001"),
        task=TaskSpec(
            tier=TierId.PRO,
            requirement="就旱情写个通知\n第二行",
            user_files=(UserFile(name="上级文件.docx", size=1234, suffix=".docx"),),
        ),
        phase=PhaseId.DRAFTING,
        phase_state=PhaseState.ACTIVE,
        dcard=DCard(
            by_id={
                DCardFieldId.D1: DCardField(
                    id=DCardFieldId.D1,
                    label="主题",
                    value="防旱",
                    provenance=Provenance.USER,
                ),
                DCardFieldId.D2: DCardField(
                    id=DCardFieldId.D2,
                    label="场景",
                    value="县抗旱应急",
                    provenance=Provenance.LEAD_INFERRED,
                ),
            }
        ),
        role_states={RoleId.TEAM_LEAD: RoleState.RUNNING, RoleId.LANGUAGE: RoleState.DONE},
        artifacts={
            "outline": ArtifactRef(
                id=ArtifactId("a-1"),
                kind=ArtifactKind.OUTLINE,
                path="data/run-测试-001/大纲.md",
                version=2,
            )
        },
        gates={
            "G0": GateRecord(
                gate="G0",
                passed=True,
                attempt=1,
                evidence=("事实三态检查通过",),
                rejection_reason="",
            ),
            "G3": GateRecord(
                gate="G3",
                passed=False,
                attempt=2,
                evidence=("第2次仍缺主送机关",),
                rejection_reason="缺少主送机关",
            ),
        },
        completed_call_ids=("abc123", "def456"),
        last_seq=17,
        last_error="",
    )
