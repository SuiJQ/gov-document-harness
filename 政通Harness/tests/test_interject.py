"""插话真的通了（D-012：指令队列 + 阶段边界检查）。

## 为什么这条线此前是断的，而没人发现

`flow.inject_directive` 的处理器一直在、工具描述一直在、决策 D-012 一直在，
但 **`工具依赖.转达` 恒为 `None`**——装配根从没赋值。

后果：模型调插话只会拿到一句「未接线」，而**流程照走、不报错**。
用户的插话静默消失，日志里也看不出来。

这与 D-286（`问用户`）、D-274（`token_入/出`）是同一族：
**被前置的失败挡住、因而从未执行的代码里，藏着第二个缺陷。**
接缝不接，下面的一切都验不到。

## 两个对象要分清（我第一版就搞错了）

| 对象 | 寿命 | 有什么 |
|---|---|---|
| `编排器` | 长 | LLM 端口、**指令队列**、`收用户指令` |
| `_过程` | 每次运行建一个 | **`_生效指令`**、`_构造请求`、`推进一步` |

插话提交在 `编排器` 上（界面可能在 `跑到终` 建过程**之前**就提交了），
生效判断在 `_过程` 上（阶段边界）。混了就是 `AttributeError`。

## 本文件钉三件事

1. **装配根真的把转达接到编排器上**
2. **插话在阶段边界生效，不打断正在进行的模型请求**（D-012 原文）
3. **插话真的进了模型请求**，且标成「用户插话」而不是系统规则
"""

from __future__ import annotations

import sys
from pathlib import Path

仓库根 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(仓库根 / "tests"))


def _编排():
    import test_step14_orchestrate_gate as M

    return M._编排()


def _过程(编):
    """建一次过程。**必须先建**——`取一步` 的判据正是「一步一阶段」。

    档位显式交 PRO 而非留空：编排器不再有静默默认档（D-380），
    而本文件只验插话不验档位——PRO 与硬编码时代的实际行为一致。
    """
    from zhengtong.domain.tier import TierId

    return 编._建过程(档位=TierId.PRO)


class Test装配接线:
    def test_转达由编排器提供且能接上(self) -> None:
        """`接转达` 这个口子接上之后，`工具依赖.转达` 不再是 `None`。

        用装配根那条真实路径验：`建工具运行器 → 编排器 → 接转达`。
        此前**连这个口子都没有**，所以「接上」根本无从谈起。
        """
        from zhengtong.adapters.fakes import (
            FakeContentPack,
            FakeRetriever,
            FakeStorage,
            FakeUserFiles,
            FakeValidators,
        )
        from zhengtong.domain.ids import RunId
        from zhengtong.services.tools.factory import 建工具运行器

        运行器 = 建工具运行器(
            storage=FakeStorage(),
            user_files=FakeUserFiles(),
            retriever=FakeRetriever(),
            content_pack=FakeContentPack(),
            validators=FakeValidators(),
            run_id=RunId("r-0001"),
        )
        依赖 = 运行器._d  # 判据要看到装配后的真实状态
        assert 依赖.转达 is None, "工具层建成时就不该有转达——它是后补的"

        编 = _编排()
        运行器.接转达(编.收用户指令)
        assert 依赖.转达 is not None, "接转达 后仍是 None——接缝没通"
        assert callable(依赖.转达), f"转达不可调用：{依赖.转达!r}"

    def test_插话能被工具层调通(self) -> None:
        """从工具处理器一路走到编排器的队列，中间不断。"""
        from zhengtong.adapters.fakes import (
            FakeContentPack,
            FakeRetriever,
            FakeStorage,
            FakeUserFiles,
            FakeValidators,
        )
        from zhengtong.domain.ids import RunId
        from zhengtong.domain.role import RoleId
        from zhengtong.services.tools.factory import 建工具运行器
        from zhengtong.services.tools.handlers import flow_inject_directive

        运行器 = 建工具运行器(
            storage=FakeStorage(),
            user_files=FakeUserFiles(),
            retriever=FakeRetriever(),
            content_pack=FakeContentPack(),
            validators=FakeValidators(),
            run_id=RunId("r-0001"),
        )
        编 = _编排()
        运行器.接转达(编.收用户指令)

        got = flow_inject_directive(
            运行器._d,
            RoleId.TEAM_LEAD,
            {"text": "重点突出防汛"},
        )
        assert got.is_ok, f"插话被拒：{got.error}"
        assert 编._指令队列.qsize() == 1, "插话没进编排器的队列"


class Test装配根真的接了:
    """**必须走真实装配根**。

    第一版判据只验「`接转达` 这个口子存在、且手动调它能通」——
    证伪时把 `app.py` 里那一行删掉，**7 条全绿**。
    原因很简单：判据自己搭的工具层，自己调的口子，
    压根没经过装配根那条路径。

    与 D-290 同型：判据量的是「机制在」，不是「生产路径在用」。
    """

    def test_装配根把转达接上了(self) -> None:
        """**必须先触发编排器建出来**——它是懒建的。

        `App.编排器` 是 property，第一次调用才 new 编排器，
        `接转达` 就在那一刻后补。所以 `build_app()` 刚返回时
        `转达` **必然是 None**，那是正确状态，不是缺陷。

        第一版判据在 `build_app()` 之后直接断言非空，于是**恒红**——
        差点把「编排器懒建」这个既有设计当成接线失败去改。
        """
        from zhengtong.app import build_app

        with build_app() as 应用:
            assert 应用.tools._d.转达 is None, (
                "编排器还没建，转达本就不该有——若这里非空说明别处乱接了"
            )
            应用.编排器()  # 触发懒建
            依赖 = 应用.tools._d  # 判据要看装配后的真实状态
            assert 依赖.转达 is not None, "编排器建好后转达仍是 None——装配根漏了 `接转达`"

    def _经装配根插话(self, 文本: str) -> int:
        """从真实 App 走一遍 `flow.inject_directive`，看编排器队列涨没涨。"""
        from zhengtong.app import build_app
        from zhengtong.domain.role import RoleId
        from zhengtong.services.tools.handlers import flow_inject_directive

        with build_app() as 应用:
            编 = 应用.编排器()
            依赖 = 应用.tools._d
            got = flow_inject_directive(依赖, RoleId.TEAM_LEAD, {"text": 文本})
            assert got.is_ok, f"插话被拒：{got.error}"
            return 编._指令队列.qsize()

    def test_经装配根插话真进队列(self) -> None:
        assert self._经装配根插话("重点突出防汛") == 1, "插话没进编排器队列"


class Test阶段边界生效:
    def test_排队时不立即注入(self) -> None:
        """**D-012 原文：插话在检查点生效。**

        排队的那一刻正在跑的模型请求**不该**被改写——
        改写正在飞的请求等于打断，而打断模型请求的代价是重新生成。
        """
        编 = _编排()
        编.收用户指令("重点突出防汛")
        过程 = _过程(编)
        assert 过程._生效指令 == [], f"指令不该立刻生效：{过程._生效指令}"
        assert 编._指令队列.qsize() == 1, "指令没排进队列"

    def test_下一阶段边界取走(self) -> None:
        编 = _编排()
        编.收用户指令("重点突出防汛")
        过程 = _过程(编)
        过程.推进一步(0)
        assert 过程._生效指令 == ["重点突出防汛"], f"边界没取到指令：{过程._生效指令}"

    def test_阶段中途提交不打断本阶段(self) -> None:
        """**D-012 最核心的一条保证**，此前没有任何判据覆盖。

        边界之后、阶段进行中提交插话，**本阶段的请求不许带上它**——
        带上就等于改写正在飞的请求，而改写的代价是整个响应作废重来。

        > 这条是补的：证伪时我把「立刻生效」的错误注入进去，
        > 已有判据全绿——因为它们都在**阶段开始前**提交，
        > 根本进不到「阶段进行中」这个窗口。
        > **判据没覆盖的那个窗口，恰恰是这个功能最容易坏的地方。**
        """
        from zhengtong.domain.phase import PhaseId
        from zhengtong.domain.role import RoleId

        编 = _编排()
        过程 = _过程(编)
        过程.推进一步(0)  # 边界：队列此时是空的
        编.收用户指令("阶段中途来的话")  # 阶段进行中提交
        请求 = 过程._构造请求(RoleId.TEAM_LEAD, PhaseId.DRAFTING)
        内容 = "\n".join(m.content for m in 请求.messages)
        assert "阶段中途来的话" not in 内容, (
            "本阶段的请求带上了中途来的指令——改写了正在飞的请求（D-012 禁止）"
        )

    def test_指令进请求且标成用户插话(self) -> None:
        from zhengtong.domain.phase import PhaseId
        from zhengtong.domain.role import RoleId

        编 = _编排()
        编.收用户指令("重点突出防汛")
        编.收用户指令("不要超过八百字")
        过程 = _过程(编)
        过程._生效指令 = 编._取新指令()
        请求 = 过程._构造请求(RoleId.TEAM_LEAD, PhaseId.DRAFTING)
        内容 = "\n".join(m.content for m in 请求.messages)
        assert "重点突出防汛" in 内容, "插话没进请求"
        assert "不要超过八百字" in 内容
        assert "[用户插话]" in 内容, f"插话没标来源：{内容[-300:]}"
        # **不能被当成系统规则**
        for m in 请求.messages:
            if "重点突出防汛" in m.content:
                assert m.role == "user", f"插话被标成 {m.role}——它不是系统规则"

    def test_取完即换新不重复烧token(self) -> None:
        """上一阶段的指令**不该在下一阶段再发一遍**。

        不然每阶段都重发，token 白烧，且模型会以为用户反复强调。
        """
        编 = _编排()
        编.收用户指令("第一阶段的话")
        第一次 = _过程(编)
        第一次.推进一步(0)
        assert 第一次._生效指令 == ["第一阶段的话"]
        第二次 = _过程(编)
        第二次.推进一步(1)
        assert 第二次._生效指令 == [], f"上一阶段的指令被重复发送：{第二次._生效指令}"

    def test_空插话不入队(self) -> None:
        编 = _编排()
        编.收用户指令("   ")
        编.收用户指令("")
        assert 编._指令队列.qsize() == 0, "空插话入队了"
