"""编排级门禁：GATING 段真跑判定 + 退回重写（D-050）。

前一组（`test_step14_judge`）锁的是纯判定函数；本组锁的是**编排真的调用它**——
若编排压根不调 `门禁判定`，纯函数再对也没用（本步历史缺陷正是这个形态：
零件都测过、衔接没测）。

## 编排器如何拿到门禁结论

门禁结论**注入**，不从模型自由文本里解析——那既脆弱又不可测。
生产装配接真实取证实现；测试接脚本。故判据用脚本裁定驱动编排。

## 退回怎么观测

`完整阶段序列` 取自**构造时那条静态线**，退回重跑会插新阶段，
所以判据不能只看它——要看**真正跑过的阶段**（`该段产物` 按阶段分组）。
"""

from __future__ import annotations

from threading import Lock

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.domain.errors import ErrorCode
from zhengtong.domain.event import EventKind
from zhengtong.domain.phase import PhaseId
from zhengtong.domain.run_state import GateRecord
from zhengtong.domain.tier import TierId
from zhengtong.orchestration import 编排器
from zhengtong.orchestration.gates_forging import 门禁取证请求
from zhengtong.ports.content_pack import ContentPackPort
from zhengtong.ports.llm import LlmPort
from zhengtong.ports.storage import StoragePort


def _编排(
    *,
    驳回: dict[str, int] | None = None,
    缺席: bool = False,
    storage: StoragePort | None = None,
    llm: LlmPort | None = None,
    容量token: int | None = None,
    内容包: ContentPackPort | None = None,
) -> 编排器:
    """装一个编排器。

    ## 为什么按**门禁计次**而不是按**调用次序**消费脚本

    并行化之后 `门禁取证` 每组被调一次（4 个并行组 + 1 次终闸），
    且**并发**调用。若脚本靠 `next(队列)` 顺序取裁定，
    某组的裁定会被别的组领走——而那组并不负责这些门禁，
    驳回就被静默丢弃，退回永远不触发（实测：GATE_REWORK 一条都没有）。

    故改为**按门禁号计次**：某门禁前 N 次调用判驳回、之后放行。
    与调用顺序无关，线程安全（用锁计数）。

    `驳回={"G2": 1}` = G2 第一次驳回、第二次放行。
    `缺席=True` = 一条结论都不给（验 D-221 无证据不放行）。
    """
    次数: dict[str, int] = {}
    锁 = Lock()

    def 门禁取证(请求: 门禁取证请求) -> dict[str, GateRecord]:
        门禁 = 请求.门禁
        if 缺席:
            return {}
        出: dict[str, GateRecord] = {}
        with 锁:
            for g in 门禁:
                n = 次数.get(g.value, 0)
                次数[g.value] = n + 1
                限 = (驳回 or {}).get(g.value, 0)
                出[g.value] = GateRecord(
                    gate=g.value,
                    passed=n >= 限,
                    rejection_reason="" if n >= 限 else "编造",
                )
        return 出

    # **显式传，不走 `**dict`**：`dict[str, object]` 会把参数类型
    # 全抹成 object，一次带出 11 条类型错误（D-159 那个坑的变体）。
    return 编排器(
        llm=llm if llm is not None else FakeLlm(),
        storage=storage if storage is not None else FakeStorage(),
        门禁取证=门禁取证,
        容量token=容量token if 容量token is not None else 128_000,
        content_pack=内容包,
    )


class TestGATING真跑判定:
    def test_门禁结论进产物(self) -> None:
        """通过的门禁须出现在 gate-report 里——「走过」不等于「取证过」。"""
        编 = _编排()
        结果 = 编.跑到终(档位=TierId.PRO, 需求="通知").unwrap()
        报告 = "".join(p.内容 for p in 结果.产物 if p.名称 == "gate-report.md")
        assert 报告.strip(), "GATING 段没产出任何报告——门禁静默"

    def test_门禁结论落进状态(self) -> None:
        """门禁结论必须写进 RunState.gates，不能只在报告里。"""
        编 = _编排()
        编.跑到终(档位=TierId.PRO, 需求="通知")
        终态 = 编.终态
        assert 终态 is not None
        assert 终态.gates, "门禁结论没落进 RunState——续跑与取证台账都拿不到"


class Test退回重写:
    def test_门禁不通过则退回重起草(self) -> None:
        """第一次不通过（attempt=0）判退回，须真的重起草一遍。

        观测点是 **REVISION 段的产物**——重起草走 REVISION phase 而非
        复用 DRAFTING（架构 D-065：退回靠 phase 不同自然不命中幂等）。
        """
        编 = _编排(驳回={"G2": 1})
        结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
        assert 结果.is_ok, f"首次退回不该直接失败：{结果.error}"
        重写 = [e for e in 编.事件 if e.kind is EventKind.GATE_REWORK]
        assert 重写, "没发 GATE_REWORK 事件——退回没触发重起草"

    def test_退回时进REVISION阶段(self) -> None:
        """退回必须显式经过 REVISION 阶段，不能悄悄跳回 DRAFTING。"""
        编 = _编排(驳回={"G2": 1})
        结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
        assert 结果.is_ok
        # 退回后的重起草事件 phase 必须是 REVISION——复用 DRAFTING 会让
        # call_id 与首轮相同、被幂等跳过，拿到旧正文（架构 D-065）
        重写 = [e for e in 编.事件 if e.kind is EventKind.GATE_REWORK]
        assert 重写, "没发 GATE_REWORK——退回未发生"
        起草事件 = [
            e
            for e in 编.事件
            if e.kind is EventKind.PHASE_STARTED and e.phase == PhaseId.REVISION.value
        ]
        assert 起草事件, "重起草没走 REVISION 阶段——call_id 会与首轮相同"

    def test_第三次判失败转用户裁决(self) -> None:
        """同一门禁连续退回，第 3 次**不再自动重写**，转用户裁决（D-050）。

        编排须发 GATE_ESCALATED 并以 GATE_REWRITE_EXHAUSTED 结束，
        且**不得**有第 3 次 REVISION——再自动重写就是死循环。
        """
        编 = _编排(驳回={"G2": 99})  # 永远驳回
        结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
        assert 结果.is_error, "退满上限仍自动放行——违反 D-050"
        错 = 结果.error
        assert 错 is not None
        assert 错.code is ErrorCode.GATE_REWRITE_EXHAUSTED, f"错误码错：{错.code}"
        升级 = [e for e in 编.事件 if e.kind is EventKind.GATE_ESCALATED]
        assert 升级, "未升级用户裁决——上限没生效"
        重写轮次 = [e for e in 编.事件 if e.kind is EventKind.GATE_REWORK]
        assert len(重写轮次) == 2, f"重写了 {len(重写轮次)} 轮，上限 2 次应恰好 2 轮"


class Test无证据不放行:
    def test_缺席的门禁不算通过(self) -> None:
        """裁定里**没提到**的门禁不得被当作通过（E041 无证据不通过）。

        这是本轮查出的真缺陷：原先 `驳回 = [记 for 记 in 裁定.values() if not 记.passed]`
        只看「裁定说了什么」，对**没说**的门禁一律放行——
        取证方压根没交结论的门禁，在报告里却长得跟通过一样。
        """
        编 = _编排(缺席=True)  # 12 道门禁一道结论都没给
        结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
        assert 结果.is_error, "一道门禁都没取证却整体放行——违反 E041"
        错 = 结果.error
        assert 错 is not None
        assert 错.code is ErrorCode.GATE_NO_EVIDENCE, f"应报无证据，却报了 {错.code}"

    def test_未接线取证不等于全部通过(self) -> None:
        """未注入取证实现时**不得**声称门禁通过——须显式报无证据。

        默认实现返回空 dict，空 dict 与「全部通过」必须区分开：
        前者是「没人取证」，后者是「取过了，都好」。
        """
        编排 = 编排器(llm=FakeLlm(), storage=FakeStorage())
        结果 = 编排.跑到终(档位=TierId.PRO, 需求="通知")
        assert 结果.is_error, "取证未接线却当作门禁全过"
        错 = 结果.error
        assert 错 is not None
        assert 错.code is ErrorCode.GATE_NO_EVIDENCE

    def test_缺证据时一次性升级不空转(self) -> None:
        """环境性缺失须**一次性**升级，不得走「退 2 次」（反死循环铁律）。

        取证方压根没交结论 → 属环境性缺失，第 1 轮就该请用户裁决，
        不该先白白退回重写两轮。
        """
        编 = _编排(缺席=True)
        结果 = 编.跑到终(档位=TierId.PRO, 需求="通知")
        assert 结果.is_error
        重写 = [e for e in 编.事件 if e.kind is EventKind.GATE_REWORK]
        assert not 重写, f"无证据却空转重写了 {len(重写)} 轮——违反反死循环铁律"
        升级 = [e for e in 编.事件 if e.kind is EventKind.GATE_ESCALATED]
        assert 升级, "环境性缺失未升级用户决策"


class TestD065幂等与退回:
    def test_退回重写重新问模型(self) -> None:
        """退回重写**不得**命中幂等（架构 D-065）。

        若重起草复用首轮的 `call_id`，幂等会跳过它、退回拿到同一版正文，
        于是反复退回却永不改变——门禁成了摆设。故比对：退回一轮的模型
        调用数必须**多于**全通过的那一次。
        """
        驳回 = _编排(驳回={"G2": 1})
        全过 = _编排()
        驳回.跑到终(档位=TierId.PRO, 需求="通知")
        全过.跑到终(档位=TierId.PRO, 需求="通知")
        assert len(驳回.发出的请求) > len(全过.发出的请求), (
            f"退回后模型调用 {len(驳回.发出的请求)} 次，"
            f"与全通过的 {len(全过.发出的请求)} 次相同——重起草被幂等跳过了"
        )

    def test_同一重试命中幂等(self) -> None:
        """同一 `call_id` 不得重复问模型（网络重试幂等，D-027/D-065）。"""
        首 = _编排()
        首.跑到终(档位=TierId.PRO, 需求="通知")
        首轮 = len(首.发出的请求)
        assert 首轮 > 0
        # 已完成清单注入后续跑，问的次数不得增加
        续 = 编排器(
            llm=FakeLlm(),
            storage=FakeStorage(),
            门禁取证=lambda 请求: {
                g.value: GateRecord(gate=g.value, passed=True) for g in 请求.门禁
            },
            初始状态=首.终态,
            初始已完成=首._建过程(档位=TierId.PRO).已完成,
        )
        续.跑到终(档位=TierId.PRO, 需求="通知")
        assert len(续.发出的请求) < 首轮, "续跑重复问了模型——幂等失效"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
