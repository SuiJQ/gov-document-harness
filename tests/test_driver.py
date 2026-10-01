"""`drive()` 驱动循环测试。

重点测三件容易错、且后果严重的事：

1. **退出前必须 flush**：不 flush 的话，总线里没处理完的事件随进程退出消失，
   用户看不到失败原因、日志里也没有——前面所有脱敏与落盘都白搭。
2. **失败是正常返回不是异常**（架构模块树原话）。
3. **死循环保险丝**：编排层写错时会挂死用户界面。
"""

# 本文件放宽 Unknown 类规则，理由同 tests/test_domain_behavior.py（D-085）：
# `Result.ok(...)` 的返回类型是 `Result[Any]`（三方案实测后确认唯一可行解），
# 故拿它构造结果或做成员访问时类型为 Unknown。只影响静态提示，
# 不影响运行行为，也不影响断言有效性。
#
# 另外多处 `step(state)` 故意不读 `state`——那些用例就是要验证
# "状态无关时 drive() 仍正常工作"，参数必须留着以满足签名。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnusedCallResult=false, reportUnusedParameter=false
from __future__ import annotations

import pytest

from zhengtong.domain.errors import ErrorCode, Result
from zhengtong.domain.event import Event, EventKind
from zhengtong.domain.ids import RunId
from zhengtong.domain.phase import PhaseId
from zhengtong.kernel.bus import EventBus
from zhengtong.kernel.driver import DriveOutcome, Step, drive
from zhengtong.kernel.redactor import Redactor

RUN = RunId("r-drive")


def _bus() -> EventBus:
    return EventBus(Redactor())


def _kinds(bus_seen: list[Event]) -> list[str]:
    return [e.kind.value for e in bus_seen]


# ── 正常推进 ──


def test_正常跑完三步() -> None:
    bus = _bus()
    seen: list[Event] = []
    bus.subscribe(seen.append)
    n = {"i": 0}

    def step(state: int) -> Result[Step[int]]:
        n["i"] += 1
        return Result.ok(Step(phase=PhaseId.OUTLINE, state=state + 1, done=n["i"] >= 3))

    got = drive(run_id=RUN, bus=bus, initial_state=0, step=step)
    assert got.is_ok
    assert got.unwrap().steps == 3
    bus.stop()

    kinds = _kinds(seen)
    assert kinds[0] == "run.started"
    assert kinds[-1] == "run.finished"
    assert kinds.count("phase.advanced") == 2, "三步里前两步应各发一次推进事件"


def test_完成事件带步数() -> None:
    bus = _bus()
    seen: list[Event] = []
    bus.subscribe(seen.append)

    def step(state: int) -> Result[Step[int]]:
        return Result.ok(Step(phase=PhaseId.DONE, done=True))

    got = drive(run_id=RUN, bus=bus, initial_state=0, step=step)
    assert got.unwrap().ok
    bus.stop()
    last = seen[-1]
    assert last.kind is EventKind.RUN_FINISHED
    assert last.data["steps"] == 1


# ── 失败是正常返回（不是异常）──


def test_步骤失败返回Result而非抛异常() -> None:
    """门禁判不通过、上下文超限都是**预期内**的正常结局，用异常表达
    会让 try 块越写越长，真正的崩溃反而被淹没在里面。"""
    bus = _bus()
    seen: list[Event] = []
    bus.subscribe(seen.append)

    def step(state: int) -> Result[Step[int]]:
        return Result.fail(ErrorCode.KERNEL_STALLED, "测试用失败")

    got = drive(run_id=RUN, bus=bus, initial_state=0, step=step)
    assert got.is_error
    err = got.error
    assert err is not None and err.code is ErrorCode.KERNEL_STALLED
    bus.stop()

    assert _kinds(seen) == ["run.started", "run.failed"], "失败须发 run.failed"


def test_失败原因进入事件且已flush() -> None:
    """flush 保证用户真能看到失败原因——不 flush 就随进程退出消失了。"""
    bus = _bus()
    seen: list[Event] = []
    bus.subscribe(seen.append)

    def step(state: int) -> Result[Step[int]]:
        return Result.fail(ErrorCode.GATE_FAILED, "第 7 条红线未通过")

    drive(run_id=RUN, bus=bus, initial_state=0, step=step)
    # 不再调 bus.stop()/flush() 就能看到事件，即证明 drive() 内部已排空
    assert len(seen) == 2, f"drive() 返回后仍有 {len(seen) - 2} 条事件未处理"
    assert seen[-1].data["detail"] == "第 7 条红线未通过"
    bus.stop()


def test_成功路径同样已flush() -> None:
    bus = _bus()
    seen: list[Event] = []
    bus.subscribe(seen.append)

    def step(state: int) -> Result[Step[int]]:
        return Result.ok(Step(phase=PhaseId.DONE, done=True))

    drive(run_id=RUN, bus=bus, initial_state=0, step=step)
    assert len(seen) == 2, "成功路径也须在返回前排空总线"
    bus.stop()


# ── 死循环保险丝 ──


def test_死循环被保险丝拦住() -> None:
    """编排层一旦写成"总返回同一步"，没保险丝就会挂死用户界面。"""
    bus = _bus()

    def step(state: int) -> Result[Step[int]]:
        return Result.ok(Step(phase=PhaseId.OUTLINE, state=state))  # 永不 done

    got = drive(run_id=RUN, bus=bus, initial_state=0, step=step, max_steps=20)
    assert got.is_error
    err = got.error
    assert err is not None
    assert err.code is ErrorCode.KERNEL_STALLED
    assert "20" in err.detail and "死循环" in err.detail
    bus.stop()


def test_保险丝默认值足够真实流程使用() -> None:
    """一个公文流程只有几十步，默认 10000 不该误伤真实任务。"""
    # 不用 `inspect.signature`：它返回 Any，与 reportAny 冲突且没有必要。
    # 直接断言"不传 max_steps 时不会在几步内就熔断"——
    # 跑 200 步正常推进，熔断阈值为 10000 时不可能触发。
    bus = _bus()
    seen: list[Event] = []
    bus.subscribe(seen.append)

    def step(state: int) -> Result[Step[int]]:
        return Result.ok(Step(phase=PhaseId.OUTLINE, state=state + 1, done=state >= 200))

    got = drive(run_id=RUN, bus=bus, initial_state=0, step=step)
    assert got.is_ok and got.unwrap().steps == 201
    bus.stop()


# ── 断点续跑（D-027）──


def test_续跑起点进入事件() -> None:
    """resume_from 由调用方从 RunState.last_seq 算出，内核不读盘。"""
    bus = _bus()
    seen: list[Event] = []
    bus.subscribe(seen.append)

    def step(state: int) -> Result[Step[int]]:
        return Result.ok(Step(phase=PhaseId.DONE, done=True))

    drive(run_id=RUN, bus=bus, initial_state=0, step=step, resume_from=42)
    bus.stop()
    assert seen[0].kind is EventKind.RUN_STARTED
    assert seen[0].data["resume_from"] == 42


def test_非续跑不带该字段() -> None:
    bus = _bus()
    seen: list[Event] = []
    bus.subscribe(seen.append)

    def step(state: int) -> Result[Step[int]]:
        return Result.ok(Step(phase=PhaseId.DONE, done=True))

    drive(run_id=RUN, bus=bus, initial_state=0, step=step)
    bus.stop()
    assert "resume_from" not in seen[0].data, "非续跑时不应出现该键"


# ── D-023：同一循环，两个投影 ──


def test_同一循环两种投影看到同一事件序列() -> None:
    """这是 `drive()` 存在的核心理由：TUI 与无界面模式**必须**
    看到完全相同的事件序列，否则行为迟早分叉，届时"调试模式复现不了"
    就无从谈起。"""
    from zhengtong.kernel.projection import DebugProjection, StandardProjection

    bus = _bus()
    std, dbg = StandardProjection(), DebugProjection()
    all_seen: list[Event] = []
    bus.subscribe(all_seen.append)
    bus.subscribe(std.accept)
    bus.subscribe(dbg.accept)

    def step(state: int) -> Result[Step[int]]:
        return Result.ok(Step(phase=PhaseId.DRAFTING, done=True))

    drive(run_id=RUN, bus=bus, initial_state=0, step=step)
    bus.stop()

    # 调试模式全量透传；标准模式只放行白名单
    assert len(dbg.lines()) == len(all_seen)
    # 本次只有 run.started / run.finished，两者都不在白名单 → 标准模式无输出
    assert std.lines() == (), "run.started/finished 不该进标准界面（避免与成果卡抢注意力）"


def test_终态对象可直接判断成败() -> None:
    bus = _bus()

    def step(state: int) -> Result[Step[int]]:
        return Result.ok(Step(phase=PhaseId.DONE, done=True))

    got = drive(run_id=RUN, bus=bus, initial_state=0, step=step)
    assert isinstance(got.unwrap(), DriveOutcome)
    assert got.unwrap().ok and got.unwrap().run_id == RUN
    bus.stop()


# ── D-137：step 抛异常时先留痕再抛 ──────────────────────────────────
#
# 本节的文件头第 1 条写的是「退出前必须 flush：日志里也没有——
# 前面所有脱敏与落盘都白搭」。而异常穿透时正是日志里什么都没有。


def test_异常穿透前已留下失败原因() -> None:
    """**走 `raise` 与走 `Result.fail` 的留痕不该差这么多。**

    实测（journal 落盘）：

        走 Result.fail → ['run.started', 'run.failed']
        走 raise       → ['run.started']            ← 原因消失

    而本文件头第 1 条写的正是「用户看不到失败原因、日志里也没有」。
    **注释担心的场景，正在它自己守护的这行代码里发生。**
    """
    收到: list[Event] = []
    总线 = _bus()
    总线.subscribe(收到.append)

    def 抛(state: int) -> Result[Step[int]]:
        raise ValueError("编排层在第3步写错了")

    with pytest.raises(ValueError, match="编排层在第3步写错了"):
        drive(run_id=RUN, bus=总线, initial_state=0, step=抛, max_steps=5)
    总线.stop()

    assert _kinds(收到) == ["run.started", "run.failed"]
    失败 = 收到[-1]
    assert 失败.data["detail"], "失败原因必须进事件，否则用户无从判断"
    assert "编排层在第3步写错了" in str(失败.data["detail"])


def test_异常须原样重抛不得吞掉() -> None:
    """**吞异常才是错的**：那会让编排层的 bug 变成
    「程序莫名其妙不写了」，比崩溃更难查。

    故既要留痕，也要把原异常连栈一起抛给调用方。
    """
    总线 = _bus()

    def 抛(state: int) -> Result[Step[int]]:
        raise ZeroDivisionError("boom")

    with pytest.raises(ZeroDivisionError):
        drive(run_id=RUN, bus=总线, initial_state=0, step=抛, max_steps=5)
    总线.stop()


def test_用户中断不包装成失败() -> None:
    """**`BaseException` 里的中断不该被包装成 run.failed。**

    `step` 是任意用户回调，能抛的类型不可穷举，故用 `Exception`
    而非具体类型；但 KeyboardInterrupt / SystemExit 属于
    「用户主动中断」，把它记成运行失败是误报。
    """
    收到: list[Event] = []
    总线 = _bus()
    总线.subscribe(收到.append)

    def 中断(state: int) -> Result[Step[int]]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        drive(run_id=RUN, bus=总线, initial_state=0, step=中断, max_steps=5)
    总线.stop()

    assert _kinds(收到) == ["run.started"], "用户中断不是运行失败"
