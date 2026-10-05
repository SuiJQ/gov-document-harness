"""工具执行端档位检查 + 门禁账口径 + 门禁事件出口（D-390）。

## 为什么这三组必须同时存在

它们是同一次复核里查出的三个缺口，形状互不相同，但都属一类：
**「件备好了，衔接没测」**——本项目反复记录的老毛病。

1. **执行端没有档位概念**：档位过滤只作用于「发给 API 的工具声明」，
   那只是「不告诉模型有这个工具」。实测咨询档声明里没有 `validate.run`，
   主理人硬调它**照样执行成功**。而模型发来不在清单里的工具是常态
   （幻觉、上下文污染、沿用上一轮），声明过滤管不住。
2. **`走过的门禁` 报 12 道、实际取证 10 道**：差的两道是**刻意豁免**
   （G3 无编制、G9 动态），不是漏网——但「真缺席」与「本就不适用」
   被压在同一个字段里，谁拿它当「已验证门禁数」就会高报。
3. **`gate.started` / `passed` / `failed` 零生产消费**：架构 §5.4 明列，
   代码里枚举也在，实测 12 道门禁跑完**零条 gate 事件**，
   用户（含调试模式）无从得知门禁跑没跑。

## 注意：Pro 档与 Flash 档的数字**本就该不同**

Pro 有文择体 → G3 独立取证 → 已取证 11 道；
Flash 无文择体 → G3 降级给主理人 → 已取证 10 道。
判据写死某个数字会立刻把这种正确差异判成 bug。
"""

from __future__ import annotations

import pathlib

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.domain.errors import Result
from zhengtong.domain.role import RoleId
from zhengtong.domain.run_state import GateRecord
from zhengtong.domain.tier import TIER_BY_ID, TierId
from zhengtong.orchestration.orchestrate import 编排器, 编排结果


def _依赖(tmp_path: pathlib.Path):
    from test_tools import _依赖 as 建依赖

    return 建依赖(tmp_path)[0]


def _运行器(d):
    from test_tools import _运行器 as 建

    return 建(d)


# ═══ 一、执行端档位检查 ═══


class Test执行端档位检查:
    def test_咨询档拒调他档工具(self, tmp_path: pathlib.Path) -> None:
        """**核心**：声明不给 ≠ 执行不接。咨询档调 `validate.run` 必须被拒。"""
        r = _运行器(_依赖(tmp_path))
        got = r.调用(
            "validate.run",
            角色=RoleId.TEAM_LEAD,
            参数={"checker": "hard_constraint"},
            启用域=TIER_BY_ID[TierId.CONSULT].工具域,
        )
        assert got.is_error, "咨询档竟然执行了门禁校验器"
        assert got.error is not None
        assert "本档位未启用" in got.error.detail if got.error else "", (
            got.error.detail if got.error else ""
        )

    def test_成文档仍可调门禁工具(self, tmp_path: pathlib.Path) -> None:
        """**反过来也要锁死**：Flash/Pro 是成文档，不能被一起砍掉。"""
        for 档 in (TierId.FLASH, TierId.PRO):
            r = _运行器(_依赖(tmp_path))
            got = r.调用(
                "validate.run",
                角色=RoleId.TEAM_LEAD,
                参数={"checker": "hard_constraint"},
                启用域=TIER_BY_ID[档].工具域,
            )
            assert got.is_ok, f"{档.value} 被误伤：{got.error.detail if got.error else ''}"

    def test_不传启用域则不检查(self, tmp_path: pathlib.Path) -> None:
        """默认 `None` = 不按档位检查。既有直接调用行为不变（D-389 的向后兼容）。"""
        r = _运行器(_依赖(tmp_path))
        got = r.调用("validate.run", 角色=RoleId.TEAM_LEAD, 参数={"checker": "hard_constraint"})
        assert got.is_ok, "不传启用域时不该因档位被拒"

    def test_咨询档仍能用本档工具(self, tmp_path: pathlib.Path) -> None:
        """**别把档位检查做成「什么都调不了」**——咨询档有 12 个可用工具。"""
        r = _运行器(_依赖(tmp_path))
        got = r.调用(
            "shared.write",
            角色=RoleId.TEAM_LEAD,
            参数={"name": "意见", "kind": "note", "content": "内容"},
            启用域=TIER_BY_ID[TierId.CONSULT].工具域,
        )
        assert got.is_ok, got.error.detail if got.error else ""

    def test_每个工具的域都在至少一档里启用(self) -> None:
        """**没有「哪档都不启用」的工具**——那种工具等于死代码。

        而它不会报错、不会告警，只是永远调不到。
        """
        全域 = set().union(*(TIER_BY_ID[档].工具域 for 档 in TierId))
        from zhengtong.services.tools.registry import TOOL_SPECS

        死的 = [s.name for s in TOOL_SPECS if s.域 not in 全域]
        assert not 死的, f"这些工具没有任何档位启用：{死的}"


# ═══ 二、门禁账口径 ═══


def _事件(种类, 门禁号: str):
    """按 `tests/test_projection.py` 的形状造一条事件。"""
    from zhengtong.domain.event import Event, Level
    from zhengtong.domain.ids import RunId
    from zhengtong.domain.phase import PhaseId

    return Event(
        seq=1,
        kind=种类,
        run_id=RunId("r-门禁投影"),
        phase=PhaseId.GATING,
        level=Level.INFO,
        data={"gate_id": 门禁号},
    )


def _全过():
    return lambda 请求: {
        g.value: GateRecord(gate=g.value, passed=True, evidence=("实测依据",)) for g in 请求.门禁
    }


def _跑(档: TierId) -> Result[编排结果]:
    return 编排器(llm=FakeLlm(), storage=FakeStorage(), 门禁取证=_全过()).跑到终(
        档位=档, 需求="关于防汛工作的通知"
    )


class Test门禁账:
    def test_三档都能跑通(self) -> None:
        for 档 in TierId:
            got = _跑(档)
            assert got.is_ok, f"{档.value} 跑不通：{got.error.detail if got.error else ''}"

    def test_已取证加不适用覆盖本档全部门禁(self) -> None:
        """**核心**：三者合起来 == 「本档该跑」。

        拆字段的意义就在于**不丢信息**——拆完还能拼回原来那个集合。
        """
        for 档 in (TierId.FLASH, TierId.PRO):
            账 = _跑(档).unwrap().门禁账
            assert 账.缺席 == (), f"{档.value} 不该有缺席"
            合计 = {g.value for g in 账.已取证} | {g.value for g in 账.不适用}
            assert len(合计) == 12, f"{档.value} 合起来只有 {len(合计)} 道：{sorted(合计)}"

    def test_豁免的两道被如实标为不适用(self) -> None:
        """G3 无编制、G9 动态——**是豁免不是缺失**，两者必须能分开。"""
        闪 = _跑(TierId.FLASH).unwrap().门禁账
        普 = _跑(TierId.PRO).unwrap().门禁账
        assert {g.value for g in 闪.不适用} == {"G3", "G9"}, sorted(g.value for g in 闪.不适用)
        # **Pro 有文择体，G3 该独立取证**——这个差异正是拆字段才能表达的
        assert "G3" in {g.value for g in 普.已取证}, "Pro 有文择体却没取证 G3"
        assert {g.value for g in 普.不适用} == {"G9"}, sorted(g.value for g in 普.不适用)

    def test_咨询档三字段皆空(self) -> None:
        账 = _跑(TierId.CONSULT).unwrap().门禁账
        assert not (账.已取证 or 账.缺席 or 账.不适用), "咨询档门禁不适用，不该有三类中的任何一项"

    def test_全过以已取证为分母(self) -> None:
        """G3/G9 没跑**不是缺陷**，故不能进分母。"""
        assert _跑(TierId.FLASH).unwrap().门禁账.全过
        assert _跑(TierId.PRO).unwrap().门禁账.全过

    def test_报告区分不适用与无结论(self) -> None:
        """**报告是标准模式下唯一的台账**（D-236），把豁免印成「无结论」
        会让用户以为漏查了门禁，排查顺着错方向走。"""
        结果 = _跑(TierId.FLASH).unwrap()
        正文 = next(件.内容 for 件 in 结果.产物 if 件.名称 == "gate-report.md")
        assert "G3" in 正文 and "本档不适用" in 正文, 正文
        assert "**无结论**" not in 正文, f"全过档不该出现「无结论」：{正文}"


# ═══ 三、门禁事件出口 ═══


class Test门禁事件:
    def test_单轮时逐道发started且与实际取证数一致(self) -> None:
        """**单轮**下事件数 = 真取证的数（10），不是「本档该跑」的数（12）。

        只在单轮成立——退回重跑会逐轮累加，见下面那条判据。
        """
        器 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 门禁取证=_全过())
        器.跑到终(档位=TierId.FLASH, 需求="通知")
        started = [e for e in 器.事件 if e.kind.value == "gate.started"]
        passed = [e for e in 器.事件 if e.kind.value == "gate.passed"]
        assert len(started) == 10, f"started 应为实际取证的 10 道，实得 {len(started)}"
        assert len(passed) == 10, f"passed 应为 10 道，实得 {len(passed)}"

    def test_退回重跑时事件逐轮累加_这不是失真(self) -> None:
        """**事件逐轮、账最终**——两者本就不该相等（D-390 的口径更正）。

        G2 第一次不过、退回重写后第二轮过：`started` 发了两轮共 20 条，
        而 `门禁账.已取证` 仍是 10 道（结论去重后的集合）。

        这不是口径失真——要求「事件数恒等于已取证数」等于要求退回不重跑。
        本条锁住这个事实，防止将来有人把 `started=20` 当成 bug 去「修」。
        """
        次数: dict[str, int] = {}

        def 取证(请求):
            out = {}
            for g in 请求.门禁:
                n = 次数.get(g.value, 0) + 1
                次数[g.value] = n
                过 = (g.value != "G2") or n >= 2
                out[g.value] = GateRecord(
                    gate=g.value, passed=过, attempt=n - 1, evidence=("依据",)
                )
            return out

        器 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 门禁取证=取证)
        got = 器.跑到终(档位=TierId.FLASH, 需求="通知")
        assert got.is_ok, got.error.detail if got.error else ""
        started = [e for e in 器.事件 if e.kind.value == "gate.started"]
        assert len(started) == 20, f"退回一轮应发 20 条 started，实得 {len(started)}"
        assert len(got.unwrap().门禁账.已取证) == 10, "账仍应是 10 道"
        assert [e for e in 器.事件 if e.kind.value == "gate.rework"], "退回应有 rework 事件"

    def test_每条事件带门禁号(self) -> None:
        器 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 门禁取证=_全过())
        器.跑到终(档位=TierId.FLASH, 需求="通知")
        for e in 器.事件:
            if e.kind.value.startswith("gate."):
                assert e.data.get("gate_id"), f"{e.kind.value} 缺 gate_id：{e.data}"

    def test_不通过发failed而非passed(self) -> None:
        def 取证(请求):
            return {
                g.value: GateRecord(gate=g.value, passed=(g.value != "G2"), evidence=("依据",))
                for g in 请求.门禁
            }

        器 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 门禁取证=取证)
        器.跑到终(档位=TierId.FLASH, 需求="通知")
        failed = [e for e in 器.事件 if e.kind.value == "gate.failed"]
        assert len(failed) >= 1, "G2 未通过却没有 gate.failed"
        assert any(e.data.get("gate_id") == "G2" for e in failed), failed

    def test_全过时零failed(self) -> None:
        器 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 门禁取证=_全过())
        器.跑到终(档位=TierId.FLASH, 需求="通知")
        assert not [e for e in 器.事件 if e.kind.value == "gate.failed"]

    def test_失败路径也留下门禁事件(self) -> None:
        """失败路径同样要能排查——它没有结果对象可读，事件流是唯一痕迹。"""
        from test_step14_orchestrate_gate import _编排

        编 = _编排(驳回={"G2": 99})
        结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
        assert 结果.is_error
        assert [e for e in 编.事件 if e.kind.value == "gate.started"], "失败路径零门禁事件"


class Test调试模式可见门禁:
    """**标准模式仍不显示门禁**——D-046 只让展示进度/三份成果/最终成果。

    所以这次补事件**不是**为了让标准模式多显示什么，
    而是让 `DebugProjection`（全量透传）终于**有东西可透传**：
    此前事件本身不存在，调试模式也看不到门禁跑没跑。

    反过来说：若哪天给 `StandardProjection` 加门禁分支，
    那就违反 D-046 了——本组判据挡住它。
    """

    def test_调试模式记录门禁事件(self) -> None:
        from zhengtong.domain.event import EventKind
        from zhengtong.kernel.projection import DebugProjection

        投影 = DebugProjection()
        投影.accept(_事件(EventKind.GATE_PASSED, "G2"))
        assert any("gate.passed" in r and "G2" in r for r in 投影.records), 投影.records

    def test_标准模式不显示门禁(self) -> None:
        """D-046：标准模式**不展示审核意见**。门禁明细不得进标准输出。"""
        from zhengtong.domain.event import EventKind
        from zhengtong.kernel.projection import StandardProjection

        投影 = StandardProjection()
        投影.accept(_事件(EventKind.GATE_FAILED, "G2"))
        assert not any("G2" in 行 for 行 in 投影.lines()), 投影.lines()
        assert not 投影.errors, "门禁未通过不属于「错误」，走门禁报告与退回流程"
