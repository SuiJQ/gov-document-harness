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
from zhengtong.domain.tier import TIER_BY_ID, TIERS, ReasoningPolicy, TierId
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


# ═══ D-150：档位的深度思考策略（用户明确指定，不可漂移）═══


class Test深度思考由档位决定:
    """用户原话：

    > 「深度思考由档位决定，Flash 模式不深度思考，Pro 模式深度思考，
    >  但是传入的上下文不包含深度思考的内容。」

    **这三个取值是用户定的，不是推导出来的**，所以必须逐档锁死——
    反证实测：把 Flash 改成 `ON` 时，全套测试无一处变红。

    另一半「上下文不含深度思考的内容」由 D-052 管
    （适配器层丢弃 `reasoning_content`），不属本处；
    这里留一句指针，避免后人以为本枚举管了那件事。
    """

    @pytest.mark.parametrize(
        ("档位", "期望"),
        [
            pytest.param(TierId.CONSULT, ReasoningPolicy.OFF, id="咨询"),
            pytest.param(TierId.FLASH, ReasoningPolicy.OFF, id="Flash"),
            pytest.param(TierId.PRO, ReasoningPolicy.ON, id="Pro"),
        ],
    )
    def test_各档思考策略固定(self, 档位: TierId, 期望: ReasoningPolicy) -> None:
        spec = TIER_BY_ID[档位]
        assert spec.reasoning_policy is 期望, (
            f"{档位.value} 的深度思考策略被改了——"
            "这是用户明确指定的（Flash 不深度、Pro 深度），不是可推导项"
        )

    def test_策略由档位定而非用户手填(self) -> None:
        """**没有可手填的入口，故没有「填错就静默降级」的风险。**

        D-146 的教训：新增配置项必须先想清楚「填错了会静默变成什么」。
        深度思考若做成用户可填的开关，填错就是静默降级——
        用户以为在深度思考，实际没有，且界面与日志都不会提示。
        """
        import dataclasses

        from zhengtong.adapters.config import Config

        字段 = {f.name for f in dataclasses.fields(Config)}
        assert not any("reason" in n or "think" in n for n in 字段), (
            f"配置里出现了思考相关字段：{sorted(字段)}——思考策略应由档位决定，不给用户手填入口"
        )

    def test_本枚举只管要不要不管怎么开(self) -> None:
        """**职责边界**：本枚举回答「要不要」，`reasoning_probe` 回答「怎么开」。

        架构 §6.2 探测四种关闭写法（`thinking: {"type":"disabled"}` /
        `chat_template_kwargs: {"enable_thinking": false}` /
        `reasoning_effort: "none"` / 提示词前缀），
        那是第 6 步 `adapters/llm/reasoning_probe.py` 的事。

        判据：domain 层不得出现任何后端烙印。
        """
        import pathlib

        源码 = (
            pathlib.Path(__file__).resolve().parents[1] / "src" / "zhengtong" / "domain" / "tier.py"
        ).read_text(encoding="utf-8")
        for 烙印 in ("reasoning_effort", "enable_thinking", "no_think", 'thinking"'):
            assert 烙印 not in 源码, (
                f"domain 层出现了后端烙印 {烙印!r}——"
                "怎么开关是第 6 步适配器层的事，domain 只该知道要不要"
            )
