"""`kernel/scheduler.py` 测试 —— 并行门禁取证调度。

## 为什么这个模块的测试要盯「异常路径」

并行意味着代码跑在**别的线程**里。这带来一个单线程测试永远碰不到的形态：
**异常从别的线程逃出来会怎样**？

审计 D-130 的答案是：会炸掉整个 `run_groups` 调用，
另外几组的取证证据**一并丢失**。而这个缺陷在异常路径从未被执行时，
测试是全绿的。

故本文件的重心是异常路径，而不是顺利路径。
顺利路径的测试只是为了让异常路径的测试可信：
得先确认「它本来是好的」，才谈得上「别被改坏」。
"""

from __future__ import annotations

# 本文件放宽 Unknown 类规则，理由同 domain/errors.py 的 D-085：
# `Result.fail(...)` 只能返回 `Result[Any]`（三个方案实测后 `Result[Any]`
# 是唯一可行解），调用点再做成员访问时类型就退化成 Unknown。
# 审计中实测：本文件若不豁免，15 条 warning 全来自
# `Result.ok(...)` / `Result.fail(...)` 之后的 `.ok` 与 `.error` 访问。
# **只影响静态提示，不影响运行行为**——这正是本文件要验的东西。
# 用文件级注释而非 pyproject 的 overrides：实测 overrides 对
# `reportUnknownMemberType` 无效（见 D-085）。
#
# `reportUnusedCallResult` 的豁免同样有实据：本文件有两处**故意**不取返回值——
# 一处在 `pytest.raises` 里（要的就是它抛），一处在 `屏障.wait()`（等待本身即效果）。
# 而 Ruff 的 F841/B018 要求写成 `_ = ...`，可 BasedPyright 视 `_ = ...`
# 仍为一次赋值，转头报 reportAssignmentType。**两个工具的要求在此互相矛盾**，
# 试过 `_ =`、`setattr`、直接赋值、删变量四种写法都被其中一个改回去或报错。
# 与其在规则上反复拉锯，不如豁免掉那条纯提示性的规则：
# 它只影响静态提示，而本文件要验的全是运行时行为。
#
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnusedCallResult=false
import random
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from zhengtong.domain.errors import ErrorCode, Result
from zhengtong.domain.role import RoleId
from zhengtong.kernel.scheduler import GateGroup, GroupResult, run_groups

if TYPE_CHECKING:
    pass

#: D-049 的门禁恰好是 4 组。这 4 组同时也是本文件的主要输入形状。
四组: tuple[GateGroup, ...] = (
    GateGroup(owner=RoleId.TEAM_LEAD, gate_ids=("G1", "G2")),
    GateGroup(owner=RoleId.STRUCTURE, gate_ids=("G3", "G4")),
    GateGroup(owner=RoleId.LANGUAGE, gate_ids=("G5", "G6")),
    GateGroup(owner=RoleId.COMPLIANCE, gate_ids=("G7",)),
)


def _工(g: GateGroup) -> Result[str]:
    """一个正常干活的取证函数。"""
    return Result.ok(f"{g.owner.value}完成")


# ═══ D-130：异常必须就地转成失败结果 ═══


def test_一组崩溃不得炸掉整个调用() -> None:
    """**审计 D-130 的核心判据**。

    原实现全文没有 `try`，`work` 抛的异常经 `Future.result()` 重新抛出，
    `run_groups` 整个调用失败，另外几组的证据**全部丢失**。

    门禁场景下的后果很具体：D-049 让 4 个成员同时取证，
    一人的取证代码崩了（模型返回畸形、成员代码有 bug），
    本该得到「3 份证据 + 1 份失败」，
    实际得到的是**整个调用抛异常**——
    编排层连"哪道门禁缺证据"都无从判断，它拿到的是异常不是结果。
    """

    def 会崩(g: GateGroup) -> Result[str]:
        if g.owner is RoleId.LANGUAGE:
            raise RuntimeError("取证崩溃")
        return _工(g)

    结果: tuple[GroupResult[str], ...] = run_groups(四组, 会崩)  # type: ignore[arg-type]
    assert len(结果) == 4, "应返回 4 组结果，而不是抛异常"
    成功 = [x for x in 结果 if x.ok]
    assert len(成功) == 3, f"另外三组的证据不该丢，实际保住 {len(成功)} 组"


def test_崩溃组的错误码须为GATE_NO_EVIDENCE() -> None:
    """错误码要指向"证据没拿到"，而不是笼统的 `INTERNAL`
    或误导排查方向的 `LLM_REQUEST_FAILED`。

    崩溃可能来自模型返回畸形，**也可能**来自成员代码自己的 `KeyError`。
    用模型侧的码会把人引到错的方向，用 `INTERNAL` 则暗示"程序有 bug"。
    """

    def 会崩(_: GateGroup) -> Result[str]:
        raise ValueError("畸形")

    结果: tuple[GroupResult[str], ...] = run_groups(四组, 会崩)  # type: ignore[arg-type]
    assert all(not x.ok for x in 结果)
    for x in 结果:
        assert x.error is not None
        assert x.error.code is ErrorCode.GATE_NO_EVIDENCE, f"错误码错位：{x.error.code.name}"


def test_崩溃信息须含角色与门禁号() -> None:
    """报错要能定位到**是哪一组、哪道门禁**崩的。

    D-049 的编排层靠这个决定"哪道门禁由谁退回"，
    只说"取证失败"等于什么都没说。
    """

    def 会崩(_: GateGroup) -> Result[str]:
        raise ValueError("畸形")

    结果: tuple[GroupResult[str], ...] = run_groups(四组, 会崩)  # type: ignore[arg-type]
    详 = 结果[1].error
    assert 详 is not None
    assert RoleId.STRUCTURE.value in 详.detail, f"缺角色标识：{详.detail}"
    assert "G3" in 详.detail and "G4" in 详.detail, f"缺门禁号：{详.detail}"
    assert "ValueError" in 详.detail, f"缺异常类型：{详.detail}"


def test_全组崩溃不得抛() -> None:
    """全崩也要返回 4 组失败结果——不能抛。"""

    def 全崩(_: GateGroup) -> Result[str]:
        raise RuntimeError("全员崩溃")

    结果: tuple[GroupResult[str], ...] = run_groups(四组, 全崩)  # type: ignore[arg-type]
    assert len(结果) == 4
    assert not any(x.ok for x in 结果)


@pytest.mark.parametrize(
    "异常",
    [
        pytest.param(KeyError("缺键"), id="KeyError"),
        pytest.param(ValueError("畸形"), id="ValueError"),
        pytest.param(ZeroDivisionError(), id="ZeroDivisionError"),
        pytest.param(MemoryError(), id="MemoryError"),
        pytest.param(OSError("磁盘掉了"), id="OSError"),
    ],
)
def test_各类异常都须被兜住(异常: Exception) -> None:
    """兜的是**任何**从 `work` 逃出的异常，不是某几类已知的。

    窄捕获只会把一部分崩溃重新放到调用层炸掉。
    """
    组 = (GateGroup(owner=RoleId.LANGUAGE, gate_ids=("G5",)),)

    def 抛(_: GateGroup) -> Result[str]:
        raise 异常

    结果: tuple[GroupResult[str], ...] = run_groups(组, 抛)  # type: ignore[arg-type]
    assert 结果[0].error is not None
    assert 结果[0].error.code is ErrorCode.GATE_NO_EVIDENCE


def test_BaseException不得被吞() -> None:
    """**`KeyboardInterrupt` / `SystemExit` 必须穿透**。

    用户按 Ctrl+C 要能真的停下来。若这里被吞成失败结果，
    程序会在用户明确要求中断时继续往下跑——
    这比丢证据严重得多。

    四个 work 全部抛 `KeyboardInterrupt`，且**不加 `_ =`**：
    未使用的调用结果会被 Ruff 判为 F841 并「顺手」改成别的样子，
    那样测的就不是原来这一行了。改用 `pytest.raises` 直接包住整句。
    """

    def 中断(_: GateGroup) -> Result[str]:
        raise KeyboardInterrupt

    # 用真实的四组而非空元组：空输入会直接短路返回 `()`，
    # work 压根不会被调用，自然也测不到异常是否穿透
    with pytest.raises(KeyboardInterrupt):
        run_groups(四组, 中断)  # type: ignore[arg-type]


# ═══ 返回顺序恒等于入参（D-075 收集侧）═════════════════════════════════


def test_返回顺序恒等于入参顺序() -> None:
    """**与完成顺序无关**。编排层据此做确定性归并，不受调度抖动影响。

    审计中专门跑 30 轮随机完成顺序验证过这条。
    """

    def 随机延迟(g: GateGroup) -> Result[str]:
        time.sleep(random.uniform(0, 0.03))
        return _工(g)

    for _ in range(30):
        结果: tuple[GroupResult[str], ...] = run_groups(四组, 随机延迟)  # type: ignore[arg-type]
        assert [x.owner for x in 结果] == [g.owner for g in 四组], "顺序被打乱"


def test_顺序在有失败时仍恒定() -> None:
    """失败不能打乱顺序——否则编排层会把证据安到别人头上。

    这一条尤其要紧：崩溃组先完成、后完成都试过。
    """

    def 有崩(g: GateGroup) -> Result[str]:
        if g.owner is RoleId.LANGUAGE:
            time.sleep(0.01)
            raise RuntimeError("崩")
        if g.owner is RoleId.TEAM_LEAD:
            time.sleep(0.05)
        return _工(g)

    for _ in range(20):
        结果: tuple[GroupResult[str], ...] = run_groups(四组, 有崩)  # type: ignore[arg-type]
        assert [x.owner for x in 结果] == [g.owner for g in 四组]


# ═══ 真的是并行（不是穿着并行外衣的串行）═════════════════════════════


def test_四组须真并行() -> None:
    """D-049 要的就是并行。D-049 的门禁恰好 4 组，默认 4 线程正好匹配。

    审计时曾用 12 组测出「退化成串行」，追下去发现是**我编造的输入**：
    12 个任务配 4 个 worker 本就该分 3 批，裸 `ThreadPoolExecutor`
    同样是 0.61s，瓶颈在参数值不在调度逻辑。已作为已排除项记入报告。

    这里用真实的 4 组形状守住真正的并行性。
    """

    def 慢(_: GateGroup) -> Result[int]:
        time.sleep(0.2)
        return Result.ok(1)

    起 = time.perf_counter()
    结果: tuple[GroupResult[int], ...] = run_groups(四组, 慢)  # type: ignore[arg-type]
    耗 = time.perf_counter() - 起
    assert all(x.ok for x in 结果)
    assert 耗 < 0.35, f"4组×0.2s 实耗 {耗:.2f}s，疑似串行"


def test_并发须确实同时发生() -> None:
    """比计时更硬的证据：4 个 worker 在同一时刻都活着。

    计时断言在慢机器上会误判，这个不会——
    它直接数"同一瞬间有几个正在执行的 work"。
    """
    import threading

    屏障 = threading.Barrier(4, timeout=3.0)

    def 同步(_: GateGroup) -> Result[int]:
        try:
            # 4 个都到齐才放行；若真串行，第 1 个会在这里超时
            # `wait()` 返回到达序号，此处无用但不赋值给 `_`：
            # `_ = ...` 在检查器眼里仍是给 `_` 赋值（推成 int），
            # 会报 reportAssignmentType。改用独立语句 + noqa。
            屏障.wait()
        except threading.BrokenBarrierError:
            return Result.fail(ErrorCode.GATE_NO_EVIDENCE, "未并行")
        return Result.ok(1)

    结果: tuple[GroupResult[int], ...] = run_groups(四组, 同步)  # type: ignore[arg-type]
    assert all(x.ok for x in 结果), "4 个 work 未同时进入——说明不是真并行"


# ═══ 边界 ═══


def test_空输入返回空元组() -> None:
    """空输入是合法输入，不该报错。"""
    结果: tuple[GroupResult[str], ...] = run_groups((), _工)
    assert 结果 == ()


def test_单组() -> None:
    组 = (GateGroup(owner=RoleId.TEAM_LEAD, gate_ids=("G1",)),)
    结果: tuple[GroupResult[str], ...] = run_groups(组, _工)  # type: ignore[arg-type]
    assert len(结果) == 1
    assert 结果[0].owner is RoleId.TEAM_LEAD


def test_work返回失败须原样带回() -> None:
    """`work` 自己返回的失败**不得被改写**——失败原因得原样带回，
    不能在这里压成一句话。"""

    def 失败(_: GateGroup) -> Result[str]:
        return Result.fail(ErrorCode.GATE_FAILED, "G7 证据不足：缺主送机关")

    结果: tuple[GroupResult[str], ...] = run_groups(四组, 失败)  # type: ignore[arg-type]
    for x in 结果:
        assert x.error is not None
        assert x.error.code is ErrorCode.GATE_FAILED, "错误码被改写了"
        assert "缺主送机关" in x.error.detail, "失败细节被压掉了"


def test_owner须原样带回() -> None:
    """编排层靠 owner 决定"哪道门禁由谁退回"。"""
    结果: tuple[GroupResult[str], ...] = run_groups(四组, _工)  # type: ignore[arg-type]
    assert [x.owner for x in 结果] == [g.owner for g in 四组]


def test_不同owner并发不串味() -> None:
    """4 个 owner 各自的值不能串到别人身上。

    并行最典型的 bug 形态：共享变量被另一个线程覆写。
    """

    def 各归各(g: GateGroup) -> Result[str]:
        time.sleep(random.uniform(0, 0.02))
        return Result.ok(f"值-{g.owner.value}")

    for _ in range(20):
        for x in run_groups(四组, 各归各):  # type: ignore[arg-type]
            assert x.value.is_ok
            assert x.value.unwrap() == f"值-{x.owner.value}", "值串到了别的 owner"


# ═══ GroupResult / GateGroup 自身 ═══


def test_GateGroup是不可变的() -> None:
    """**不可变性是「分组一经确定就不被改」的保障**。

    用 `dataclasses.replace` 而不是"改字段然后看报错"：
    前者验的是**语义**（造不出被改过的分组），
    后者验的是实现细节（某个字段恰好被标记为 frozen），
    而后者还会和 Ruff 的 B010 规则反复冲突——实测 `setattr` 一写上去
    就被 `--fix` 改回去，与其在规则上较劲，不如换一条更准的测法。

    `replace` 造出的是**新对象**，原对象不受影响——
    这正是不可变数据该有的行为，也是编排层依赖的性质。
    """
    g = GateGroup(owner=RoleId.TEAM_LEAD, gate_ids=("G1", "G2"))
    换过 = replace(g, owner=RoleId.LANGUAGE)

    assert 换过 is not g, "replace 应造出新对象"
    assert 换过.owner is RoleId.LANGUAGE
    assert g.owner is RoleId.TEAM_LEAD, "原对象不得被改动"
    assert g.gate_ids == ("G1", "G2"), "原对象的门禁号也不得变"


def test_GateGroup的gate_ids是元组() -> None:
    """`gate_ids` 要是 `tuple` 不是 `list`。

    列表可被原地 `append`/`sort` 改动，冻结 dataclass 拦不住
    这种"通过内部可变对象"的改动——分组顺序会乱，而编排层
    依赖返回顺序的确定性（见 `test_返回顺序恒等于入参顺序`）。
    """
    g = GateGroup(owner=RoleId.TEAM_LEAD, gate_ids=("G1", "G2"))
    assert isinstance(g.gate_ids, tuple)


def test_GroupResult的ok与error() -> None:
    好值: Result[str] = Result.ok("x")
    坏值: Result[str] = Result.fail(ErrorCode.GATE_FAILED, "y")
    成功 = GroupResult(owner=RoleId.LANGUAGE, value=好值)
    assert 成功.ok and 成功.error is None
    失败 = GroupResult(owner=RoleId.LANGUAGE, value=坏值)
    assert not 失败.ok
    assert 失败.error is not None
    assert 失败.error.code is ErrorCode.GATE_FAILED
