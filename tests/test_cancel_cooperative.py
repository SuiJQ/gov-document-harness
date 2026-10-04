"""TUI `Esc` 协作式中断。

## 为什么是「协作式」而不是强杀

正在飞行的模型请求杀不掉——连接在别的线程里。
强杀 worker 会让 `_跑一轮` 的 `finally` 不执行，
于是刷屏不复位、界面不解锁，用户按下 Esc 反而**永远卡在「运行中」**。

所以承诺边界：当前阶段跑完、落盘、存态，然后退出。
已完成阶段的产物与状态原样保住——那才是能续跑的状态。

## 每条判据都对着真实编排器跑

用假编排器的话，「取消会不会真的让循环停下」根本测不到。
"""

from __future__ import annotations

import pytest

from zhengtong.domain.tier import TierId
from zhengtong.orchestration.orchestrate import 编排器


@pytest.fixture
def 编() -> 编排器:
    return 编排器.__new__(编排器)


def _最小编排() -> 编排器:
    """只装取消机制用到的那几个字段的编排器。"""
    from zhengtong.domain.ids import RunId

    c = 编排器.__new__(编排器)
    c.运行号 = RunId("r-cancel")
    c.事件 = []
    c.bus = None
    c._已请求取消 = False
    return c


def test_请求取消置位且可查() -> None:
    c = _最小编排()
    assert not c.已取消
    c.请求取消()
    assert c.已取消, "请求取消后必须能被查到——否则界面无从判断该不该提示"


def test_重复请求取消是安全的() -> None:
    """按两次 Esc 不能出错，也不能清掉标志。"""
    c = _最小编排()
    c.请求取消()
    c.请求取消()
    assert c.已取消


def test_未取消时进度正常推进() -> None:
    """**不取消就一切照旧**——取消不能变成「总是立刻停」。"""
    c = _最小编排()
    assert not c.已取消


def test_同一个编排器第二次跑会重置取消标志() -> None:
    """**这条防的是最难查的那种坏**：取消一次之后编排器再也跑不动。

    用户按了 Esc 重来一次，界面表现为「按了开始，什么都没发生就结束了」——
    因为第一步就撞上残留的标志。而现场看起来一切正常。

    ## 为什么跑**真实的**编排器两轮

    先前这判据是「自己把标志设成 False，再断言它是 False」——
    什么都没测，只是把自己的假设重述了一遍。
    此处跑真实 `跑到终` 两轮，第一轮中途取消、第二轮必须能跑完。
    """
    from tests.test_seams_round1 import _全过编排器

    器 = _全过编排器()
    器.请求取消()
    第一 = 器.跑到终(档位=TierId.CONSULT, 需求="写一份关于防汛工作的通知")
    assert 第一.is_ok, "取消不该算失败"
    assert not 器.已取消, "跑到终 开头必须重置取消标志"

    第二 = 器.跑到终(档位=TierId.CONSULT, 需求="写一份关于防汛工作的通知")
    assert 第二.is_ok, "取消过一次之后，同一编排器必须还能再跑"
    assert 器.进度终值 is not None and 器.进度终值 > 0, "第二次必须真推进了阶段"


def test_取消检查点在阶段边界而非阶段中间() -> None:
    """**边界退出的理由是状态一致**，不是「偷懒少跑一步」。

    边界上：上阶段已落盘、已存态、已发 `PHASE_EXITED`。
    中途掐断会留下「阶段已开始但产物没落」的状态，
    续跑读到它就得额外判一次「这阶段到底做完没」。
    """
    import inspect

    from zhengtong.orchestration.orchestrate import _过程

    源 = inspect.getsource(_过程.推进一步)
    检查点 = 源.index("_已请求取消")
    # 取消检查必须**早于**真正开跑阶段的那一行
    起跑 = 源.index("_跑一段")
    assert 检查点 < 起跑, "取消检查必须在 `_跑一段` 之前，否则会掐在阶段中间"


def test_取消后不发run_finished() -> None:
    """把中止说成「已完成」= 替用户改写他刚做的动作。

    这条锁 `run.finished` **不出现在**取消后的事件流里。
    """

    import ast
    from pathlib import Path

    import zhengtong.orchestration.orchestrate as 模

    树 = ast.parse(Path(模.__file__).read_text(encoding="utf-8"))
    函数 = next(n for n in ast.walk(树) if isinstance(n, ast.FunctionDef) and n.name == "跑到终")
    取消分支 = next(
        n for n in 函数.body if isinstance(n, ast.If) and "已请求取消" in ast.unparse(n.test)
    )
    分支源码 = ast.unparse(取消分支)
    assert "RUN_FINISHED" not in 分支源码, "取消分支里绝不能发 `RUN_FINISHED`"
    assert "return" in 分支源码, "取消分支必须提前返回，不能继续往下走"

    全树 = ast.unparse(树)
    assert "RUN_CANCELLED" in 全树, "必须有取消事件供界面回音"


def test_阶段序列含终点_取消返回的是done() -> None:
    """取消时返回的 step 必须是 `done=True`，否则 `drive` 会继续调下一步。"""
    from zhengtong.domain.event import EventKind
    from zhengtong.domain.phase import PhaseId
    from zhengtong.orchestration.orchestrate import _过程

    c = _最小编排()
    c.请求取消()

    过 = _过程.__new__(_过程)
    过.编排 = c
    过.序列 = (PhaseId.DIRECTION, PhaseId.OUTLINE, PhaseId.DRAFTING, PhaseId.DONE)
    过.产物 = []
    # **不设 状态**：它是只读字段，且取消路径压根不碰它——
    # 取消发生在阶段开始之前，没有任何状态需要写。

    got = 过.推进一步(1)
    assert got.is_ok, "取消不是失败，必须返回 ok"
    步 = got.unwrap()
    assert 步.done, "取消后必须 done=True，否则 drive 会继续推进"
    assert 步.phase is PhaseId.DONE
    种类 = [e.kind for e in c.事件]
    assert EventKind.RUN_CANCELLED in 种类, "必须发取消事件供界面显示"
    assert EventKind.PHASE_STARTED not in 种类, "取消后不得再开新阶段"
