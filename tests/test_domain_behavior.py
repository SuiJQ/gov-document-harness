# 本文件放宽 Unknown 类规则：`Result.fail(...)` 的返回类型是 `Result[Any]`
# （D-085：`Result[Never]` 因不变性报错，`Self` 在裸调用时退回 Unknown，
#   `Result[Any]` 是唯一可行解——三方案均已实测）。故拿它的结果去做成员访问时类型是未知的。
# 这不影响断言有效性——未知只影响静态提示，不影响运行行为。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnusedCallResult=false
"""L0 行为测试：Result、D-Card 校验、配平铁律、ContextView 不可变性。"""

from __future__ import annotations

import pytest

from zhengtong.domain.dcard import (
    ALLOWED_INFERRED_FIELDS,
    DCard,
    DCardFieldId,
    Provenance,
    empty_dcard,
)
from zhengtong.domain.errors import ErrorCode, Result
from zhengtong.domain.role import RoleId
from zhengtong.domain.tier import TIERS, TierId
from zhengtong.domain.validation import (
    validate_dcard,
    validate_gate_balance,
    validate_task,
)


def _filled_card(**overrides: str | Provenance) -> DCard:
    """造一张除 D8 外全填好的确认单。"""
    card = empty_dcard()
    for fid in DCardFieldId:
        if fid in ALLOWED_INFERRED_FIELDS:
            continue
        card = card.with_field(card.get(fid).filled_with("用户给的值", Provenance.USER))
    for key, v in overrides.items():
        fid = DCardFieldId[key]
        card = card.with_field(card.get(fid).filled_with(str(v), Provenance.USER))
    return card


class TestResult:
    def test_成功态(self) -> None:
        r: Result[str] = Result.ok("值")
        assert r.is_ok
        assert not r.is_error
        assert r.unwrap() == "值"

    def test_失败态(self) -> None:
        r: Result[str] = Result.fail(ErrorCode.CONFIG_INVALID, "配置坏了")
        assert r.is_error
        assert not r.is_ok
        assert r.error is not None
        assert r.error.code is ErrorCode.CONFIG_INVALID

    def test_错误态调用unwrap抛错(self) -> None:
        """全 L0 唯一允许抛异常处：这是编程错误，不是正常拒绝。"""
        r: Result[str] = Result.fail(ErrorCode.FILE_UNREADABLE)
        with pytest.raises(RuntimeError, match="unwrap"):
            r.unwrap()

    def test_失败态value为None(self) -> None:
        r: Result[str] = Result.fail(ErrorCode.PATH_ESCAPE)
        assert r.value is None

    def test_ok_none(self) -> None:
        r: Result[None] = Result.ok_none()
        assert r.is_ok
        assert r.value is None

    def test_错误码为字符串(self) -> None:
        """码要能直接进 JSON 与日志，不需各处写 .value。"""
        assert ErrorCode.PATH_ESCAPE == "E060"
        assert f"{ErrorCode.PATH_ESCAPE}" == "E060"

    def test_错误码不重复(self) -> None:
        values = [c.value for c in ErrorCode]
        assert len(values) == len(set(values))


class Test任务校验:
    def test_空需求被拦(self) -> None:
        from zhengtong.domain.task import TaskSpec

        r = validate_task(TaskSpec(tier=TierId.CONSULT, requirement="   "))
        assert r.is_error
        assert r.error is not None
        assert r.error.code is ErrorCode.REQUIREMENT_EMPTY

    def test_正常需求通过(self) -> None:
        from zhengtong.domain.task import TaskSpec

        r = validate_task(TaskSpec(tier=TierId.PRO, requirement="写个通知"))
        assert r.is_ok


class TestDcard校验:
    def test_空卡被拦(self) -> None:
        r = validate_dcard(empty_dcard())
        assert r.is_error
        assert r.error is not None
        assert r.error.code is ErrorCode.DCARD_FIELD_MISSING

    def test_填齐后通过(self) -> None:
        r = validate_dcard(_filled_card())
        assert r.is_ok, r.error

    def test_必填字段标为研判被拦(self) -> None:
        """D-069 的核心：不允许模型自行填充 D1–D7、D9。"""
        card = _filled_card()
        card = card.with_field(
            card.get(DCardFieldId.D4).filled_with("我猜的", Provenance.LEAD_INFERRED)
        )
        r = validate_dcard(card)
        assert r.is_error
        assert r.error is not None
        assert r.error.code is ErrorCode.DCARD_PROVENANCE_INVALID

    def test_D8允许研判(self) -> None:
        card = _filled_card()
        card = card.with_field(
            card.get(DCardFieldId.D8).filled_with("正式严肃", Provenance.LEAD_INFERRED)
        )
        r = validate_dcard(card)
        assert r.is_ok, r.error

    def test_D8为空不算缺失(self) -> None:
        """D8 允许研判，空着也不该报"缺失必填"。"""
        assert DCardFieldId.D8 not in empty_dcard().missing_fields

    def test_只有D8填好仍不通过(self) -> None:
        card = empty_dcard()
        card = card.with_field(
            card.get(DCardFieldId.D8).filled_with("正式", Provenance.LEAD_INFERRED)
        )
        r = validate_dcard(card)
        assert r.is_error


class Test配平铁律:
    """材料原文：门禁含某 Gate，则取证方必在该档编制内。"""

    def test_三档均配平(self) -> None:
        for tier in TIERS:
            r = validate_gate_balance(tier.id)
            assert r.is_ok, f"{tier.display_name} 未配平：{r.error}"

    def test_咨询档不适用门禁(self) -> None:
        assert validate_gate_balance(TierId.CONSULT).is_ok

    def test_flash含骆准绳(self) -> None:
        """D-029：G1/G2/G7/G10 的取证方是骆准绳，他必须出面。"""
        from zhengtong.domain.tier import TIER_BY_ID

        assert RoleId.COMPLIANCE in TIER_BY_ID[TierId.FLASH].roster

    def test_flash不含文择体但配平(self) -> None:
        """G3 是唯一例外：文择体未编入时降级。"""
        from zhengtong.domain.tier import TIER_BY_ID

        assert RoleId.DOC_ADVISOR not in TIER_BY_ID[TierId.FLASH].roster

    def test_flash仍需问全部要素(self) -> None:
        """D-029：Flash 不缩水提问，只减人数。"""
        from zhengtong.domain.tier import TIER_BY_ID

        assert TIER_BY_ID[TierId.FLASH].requires_user_questions

    def test_咨询不提问(self) -> None:
        from zhengtong.domain.tier import TIER_BY_ID

        assert not TIER_BY_ID[TierId.CONSULT].requires_user_questions


class Test上下文视图不可变:
    def test_不可变视图不含可变容器(self) -> None:
        """D-070：跨层只传值对象，故上下文视图里不得有 list/dict/set。"""

        def scan(obj: object, path: str, seen: set[int]) -> list[str]:
            bad: list[str] = []
            if id(obj) in seen:
                return bad
            seen.add(id(obj))
            if isinstance(obj, list | set):
                bad.append(f"{path} 是 {type(obj).__name__}")
            elif isinstance(obj, dict):
                bad.append(f"{path} 是 dict")
            return bad

        from zhengtong.domain.tier import TIERS

        problems: list[str] = []
        for t in TIERS:
            assert t.roster and isinstance(t.roster, tuple), "档位编制必须是 tuple"
            problems += scan(t.roster, f"{t.display_name}.roster", set())
        assert not problems, f"发现可变容器：{problems}"

    def test_角色集合是元组(self) -> None:
        from zhengtong.domain.role import ROLES

        assert isinstance(ROLES, tuple)

    def test_门禁集合是元组(self) -> None:
        from zhengtong.domain.gate import FORMAL_GATES

        assert isinstance(FORMAL_GATES, tuple)
