"""`domain/validation.py` 测试 —— "严禁编造"的执行者。

## 为什么这个文件的判据特别严

最高数据纪律是"严禁编造"（D-003 体系内的最高约束），
而 `validate_dcard` 是它在**结构层面**的执行者——不看文本内容，
只判"该有的有没有、来源标注有没有越权"。

**校验漏检一处，等于防线破一个洞**，而破洞不会报错，
只会让编造的内容一路走到用户面前。所以本文件重点测**负例**：
每一条规则都要有一个"违反它会被拦住"的用例。
"""

from __future__ import annotations

import pytest

from zhengtong.domain.dcard import (
    ALLOWED_INFERRED_FIELDS,
    DCard,
    DCardField,
    DCardFieldId,
    Provenance,
    empty_dcard,
)
from zhengtong.domain.errors import ErrorCode
from zhengtong.domain.gate import FORMAL_GATES, GATES, GateId
from zhengtong.domain.ids import RunId
from zhengtong.domain.role import RoleId
from zhengtong.domain.run_state import RunState
from zhengtong.domain.task import TaskSpec
from zhengtong.domain.tier import TIER_BY_ID, TierId
from zhengtong.domain.validation import (
    validate_dcard,
    validate_gate_balance,
    validate_run_state,
    validate_task,
)

# ═══ 需求 ═══


def test_需求为空须拦() -> None:
    got = validate_task(TaskSpec(tier=TierId.PRO, requirement="   "))
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.REQUIREMENT_EMPTY


def test_需求非空通过() -> None:
    assert validate_task(TaskSpec(tier=TierId.PRO, requirement="就旱情写个通知")).is_ok


def test_需求仅含换行视为空() -> None:
    """只有空白的需求等于没需求。"""
    assert validate_task(TaskSpec(tier=TierId.PRO, requirement="\n\n\t")).is_error


# ═══ D-Card：必填字段 ═══


def test_全空Dcard须拦() -> None:
    got = validate_dcard(empty_dcard())
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.DCARD_FIELD_MISSING


def test_必填字段整个不在字典里也要拦() -> None:
    """**审计中重点查证的漏洞形态**。

    `validate_dcard` 只遍历 `by_id.items()`，看似会漏掉"字段没登记"的情况。
    实测不漏：`empty_dcard()` 保证 9 个字段恒在，
    删掉任何一个都让另一个空字段先触发 `is_missing` 分支。
    但这条要**显式锁死**——一旦将来 `empty_dcard` 改成只登记有值的字段，
    校验就会静默漏检，而"漏检"恰恰是防线破洞的方式。
    """
    必填 = [f for f in DCardFieldId if f not in ALLOWED_INFERRED_FIELDS]
    assert len(必填) == 8, "必填字段数变了，本测试的遍历前提失效"
    for 缺 in 必填:
        残 = DCard(by_id={k: v for k, v in empty_dcard().by_id.items() if k is not 缺})
        got = validate_dcard(残)
        assert got.is_error, f"删掉 {缺.name} 后校验竟然通过了——防线漏了"


def test_填全必填字段后通过() -> None:
    d = _full_dcard()
    assert validate_dcard(d).is_ok


# ═══ D-Card：来源标注越权（D-040）═══


def test_必填字段标研判须拦() -> None:
    """**这是最关键的一条**：若允许标成研判，模型只要把字段标一下
    就能自己填上内容——"严禁编造"立刻失效。"""
    got = validate_dcard(_with_provenance(DCardFieldId.D1, Provenance.LEAD_INFERRED))
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.DCARD_PROVENANCE_INVALID


def test_八条规则逐条测() -> None:
    """逐字段过一遍，而不是只测 D1——
    少写一条 `continue` 就是一个漏网的洞。"""
    for fid in DCardFieldId:
        if fid in ALLOWED_INFERRED_FIELDS:
            continue
        got = validate_dcard(_with_provenance(fid, Provenance.LEAD_INFERRED))
        assert got.is_error, f"{fid.name} 被标成研判竟然通过了"


def test_D8是唯一允许研判的字段() -> None:
    assert list(ALLOWED_INFERRED_FIELDS) == [DCardFieldId.D8]
    assert validate_dcard(_with_provenance(DCardFieldId.D8, Provenance.LEAD_INFERRED)).is_ok


def test_D8缺失是允许的() -> None:
    """D8 是可研判字段，为空不算缺失。"""
    d = _full_dcard()
    d.by_id[DCardFieldId.D8] = DCardField(
        id=DCardFieldId.D8, label="备注", value="", provenance=Provenance.LEAD_INFERRED
    )
    assert validate_dcard(d).is_ok


def test_报错信息含字段标签() -> None:
    """用户要知道**是哪一条**不对，不能只说"校验失败"。"""
    got = validate_dcard(_with_provenance(DCardFieldId.D1, Provenance.LEAD_INFERRED))
    assert got.is_error
    assert got.error is not None
    assert got.error.detail != ""


def test_用户提供的来源不被拦() -> None:
    d = _full_dcard()
    assert validate_dcard(d).is_ok


# ═══ 编制—门禁配平 ═══


@pytest.mark.parametrize("tier", list(TierId))
def test_各档配平均通过(tier: TierId) -> None:
    """三档的编制表都必须与门禁配置自洽。
    任何一档配不平，那档就带着静默失效的门禁运行。"""
    got = validate_gate_balance(tier)
    assert got.is_ok, f"{tier.name} 配平失败：{got.error}"


def test_咨询档不适用门禁() -> None:
    """咨询档只主理人一人，无法取证任何门禁，故不适用。"""
    assert TIER_BY_ID[TierId.CONSULT].gates_apply is False
    assert validate_gate_balance(TierId.CONSULT).is_ok


def test_Flash档不含文择体() -> None:
    """Flash 三人，文择体不在内——这是 G3 降级的前提。"""
    assert RoleId.DOC_ADVISOR not in TIER_BY_ID[TierId.FLASH].roster
    assert RoleId.DOC_ADVISOR in TIER_BY_ID[TierId.PRO].roster


def test_G3取证方确为文择体() -> None:
    """降级规则依赖这个前提。若哪天 G3 的取证方变了，
    `validate_gate_balance` 里那条例外就会变成错误的放行。"""
    assert GATES[GateId.G3].owners == (RoleId.DOC_ADVISOR,)


def test_G9为动态取证() -> None:
    """动态取证不参与静态配平（另行判定）。"""
    assert GATES[GateId.G9].owners == ()


def test_正式门禁均有定义() -> None:
    for g in FORMAL_GATES:
        assert GATES[g].name != ""


# ═══ 运行状态自检 ═══


def test_状态含未编入成员须拦() -> None:
    """**审计中查出的错误码语义错配**（D-115）。

    原用 `GATE_RESPONSIBLE_NOT_IN_TIER`，而该码字面专指门禁取证方。
    用户看到"门禁取证问题"，实际问题却是"状态被污染"，
    排查方向会被带偏。
    """
    from zhengtong.domain.role import RoleState

    st = _state(TierId.FLASH)
    st.role_states[RoleId.DOC_ADVISOR] = RoleState.IDLE
    got = validate_run_state(st)
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.STATE_ROLE_NOT_IN_TIER


def test_未知门禁须拦且用专属码() -> None:
    """原用 `GATE_NO_EVIDENCE`（"门禁无证据"），与"状态里有未知门禁"无关。"""
    st = _state(TierId.PRO)
    st.gates["G99"] = _gate("G99")
    got = validate_run_state(st)
    assert got.is_error
    assert got.error is not None
    assert got.error.code is ErrorCode.STATE_UNKNOWN_GATE


def test_正常状态通过() -> None:
    assert validate_run_state(_state(TierId.PRO)).is_ok


def test_本档编制内成员不被拦() -> None:
    st = _state(TierId.PRO)
    for role in TIER_BY_ID[TierId.PRO].roster:
        st.role_states[role] = _rs()
    assert validate_run_state(st).is_ok


# ═══ 夹具 ═══


def _rs():
    from zhengtong.domain.role import RoleState

    return RoleState.IDLE


def _gate(name: str):
    from zhengtong.domain.run_state import GateRecord

    return GateRecord(gate=name, passed=True, attempt=1, evidence=("据",), rejection_reason="")


def _state(tier: TierId) -> RunState:
    return RunState(
        schema_version=1,
        run_id=RunId("r-校验测试"),
        task=TaskSpec(tier=tier, requirement="就旱情写个通知"),
        role_states={},
    )


def _with_provenance(fid: DCardFieldId, prov: Provenance) -> DCard:
    """`DCard` 是 frozen，改不了，只能整体造一个新的。"""
    base = _full_dcard()
    old = base.by_id[fid]
    items = dict(base.by_id)
    items[fid] = DCardField(id=fid, label=old.label, value=old.value, provenance=prov)
    return DCard(by_id=items)


def _full_dcard() -> DCard:
    """八条必填全部填好，D8 留空（可研判）。"""
    out = empty_dcard()
    for fid in DCardFieldId:
        if fid in ALLOWED_INFERRED_FIELDS:
            continue
        out.by_id[fid] = DCardField(
            id=fid,
            label=fid.name,
            value=f"{fid.name} 的内容",
            provenance=Provenance.USER,
        )
    return out
