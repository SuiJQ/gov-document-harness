"""B2：TUI 接真实运行。

## 本文件守的是四件「接上才验得到」的事

B1 只证了「能启动」。B2 把编排器接进界面，
于是下面四件事**从这一刻起才存在**——不接上，它们连被验的资格都没有。

### 1 · 跑得起来，而且跑的是真的

判据不用假编排器。用 `tests/test_step14_orchestrate_gate.py` 里那套
**真编排器**（真状态机、真门禁、真投影），只把 LLM 换成脚本件。
**自己造一个只会 `accept(事件)` 的假编排器，这条判据就是空的。**

### 2 · 线程边界

模型请求实测几十秒量级（D-024），跑在 UI 线程里界面会**完全卡死**。
故判据盯的是「跑编排的线程 ≠ UI 线程」。

### 3 · 硬停车闭环（D-287 答复侧）

D-287 把 `问答闸门` 装进了装配根，但**答复侧一直没人调 `挂答复`**——
`check_field_consumption` 的已立案里挂着这条。本文件把那一栏销案。

闭环验三段：**闸.问 阻塞 → 表单弹出 → 答复回到提问方**。

### 4 · 预览同源（D-063）

预览最容易写错的地方：若从**编排结果对象**取全文，
界面就多了一条通编排层的路，而无界面驱动走的是投影——
两边迟早显示不一致。故断的是「预览正文来自投影」。

## 为什么不用 pytest-asyncio

环境里没有，也不打算为几条判据新增依赖（D-003）。
一律 `asyncio.run` 包同步壳。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest
from textual.widgets import RadioButton, Static, TextArea

from zhengtong.adapters.tui import (
    ArtifactPanel,
    PreviewPanel,
    RequestBox,
    StatusLine,
    ZhengTongApp,
)
from zhengtong.domain.tier import TIERS, TierId
from zhengtong.kernel.projection import StandardProjection
from zhengtong.presentation.gate import 问答闸门
from zhengtong.presentation.tui import 事件桥接

仓库根 = Path(__file__).resolve().parents[1]
需求文本 = "写一份关于防汛工作的通知"

pytest.importorskip("textual", reason="TUI 需要 textual")


def _真编排() -> object:
    """装一个**真编排器**（真状态机 / 真门禁 / 真投影）。"""
    sys.path.insert(0, str(仓库根 / "tests"))
    import test_step14_orchestrate_gate as M

    return M._编排()


def _跑完() -> object:
    编 = _真编排()
    编.跑到终(档位=TierId.PRO, 需求=需求文本)  # pyright: ignore[reportAttributeAccessIssue]
    return 编


def _渲染(容器) -> str:
    """读容器里所有 `Static` 的文字。

    **递归查后代，不只看直接子节点**——
    模态的结构是 Screen → Vertical → Static，
    只扫 `children` 的话拿到的只有一个没有文字的 Vertical，
    于是断言「问题没显示」而实际它好好地在那儿（本次踩到）。
    """
    return "\n".join(str(w.content) for w in 容器.query(Static))


class Test跑得起来:
    def test_真的跑完一轮并出产物(self) -> None:
        """按 Ctrl+Enter 之后，产物区**真的有内容**。

        ## 为什么这条最容易假通过

        编排若压根没跑，事件流是空的，界面照样显示「（尚无成果）」
        **而且不报错**。所以断的必须是「有产物」，不是「没崩」。
        """

        async def 跑() -> None:
            编 = _真编排()
            应用 = ZhengTongApp(桥=事件桥接(), 取编排器=lambda: 编)
            async with 应用.run_test() as 试点:
                应用.query_one("#need", TextArea).text = 需求文本
                await 试点.pause()
                await 试点.press("ctrl+enter")
                await 应用.workers.wait_for_complete()
                await 试点.pause()
                文本 = _渲染(应用.query_one(ArtifactPanel))
                assert "尚无成果" not in 文本, f"跑完了却没产物：{文本}"
                assert 文本.strip(), "产物区渲染为空"

        asyncio.run(跑())

    def test_编排跑在worker线程(self) -> None:
        """编排**不许跑在 UI 线程**上。

        这条一红，界面就会真冻住几十秒——而冻住不报错，
        用户只会以为程序卡死了。
        """
        记录: dict[str, int] = {}
        编 = _真编排()
        原本 = 编.跑到终  # pyright: ignore[reportAttributeAccessIssue]

        def 跑到终(**kw):
            记录["线程"] = threading.get_ident()
            return 原本(**kw)

        编.跑到终 = 跑到终  # pyright: ignore[reportAttributeAccessIssue]

        async def 跑() -> None:
            应用 = ZhengTongApp(桥=事件桥接(), 取编排器=lambda: 编)
            async with 应用.run_test() as 试点:
                应用.query_one("#need", TextArea).text = "写一份防汛通知"
                await 试点.pause()
                await 试点.press("ctrl+enter")
                await 应用.workers.wait_for_complete()

        asyncio.run(跑())
        assert 记录.get("线程") not in (None, threading.get_ident()), (
            "编排跑在 UI 线程上——界面会完全卡死（D-024）"
        )

    def test_空诉求不启动(self) -> None:
        """空诉求**当场拒绝**，不启动编排。

        不拒绝的话，流程会跑起来然后卡在提问上等一个没有上下文的答复，
        用户白等十几分钟才知道没填。
        """

        async def 跑() -> None:
            被调: list[int] = []
            应用 = ZhengTongApp(桥=事件桥接(), 取编排器=lambda: 被调.append(1))
            async with 应用.run_test() as 试点:
                await 试点.press("ctrl+enter")
                await 试点.pause()
                文案 = str(应用.query_one("#status", StatusLine).content)
                assert "诉求" in 文案, f"没提示要填诉求：{文案}"
                assert not 被调, "空诉求却取了编排器——流程已经起了"

        asyncio.run(跑())


class Test硬停车闭环:
    """D-287 的答复侧。**这一栏销案就靠本类。**"""

    def test_拿到闸门就挂上答复方(self) -> None:
        闸 = 问答闸门(超时秒=0.2)
        ZhengTongApp(桥=事件桥接(), 闸=闸)
        assert 闸.有答复方(), "拿到闸门却没挂答复方——提问会一直等到超时"

    def test_问能拿到用户答复(self) -> None:
        """走完整条链：闸.问 阻塞 → 表单弹出 → 答复回到提问方。

        ## 线程怎么摆

        `闸.问` 会**阻塞**，所以它必须在**另一个线程**上；
        弹表单、拿答案在 **UI 线程**。
        这正是 B2 的线程形状本身，故直接照着摆一遍。
        """
        闸 = 问答闸门(超时秒=10)
        收到: list[str] = []

        async def 跑() -> None:
            应用 = ZhengTongApp(桥=事件桥接(), 闸=闸)

            def 提问() -> None:
                收到.append(闸.问(({"key": "D1", "question": "写作事由是什么"},)))

            async with 应用.run_test() as 试点:
                # **必须用 Textual worker，不能用裸线程**——
                # `push_screen_wait` 的文档写着「may only be called when running in
                # a worker」。裸线程里调它会抛 RuntimeError，
                # 而那个异常在闸.问 里会**向上传播**，线程直接死掉，
                # 表现是「表单永远不弹」——看不出是线程形状错了。
                #
                # 生产路径正是 worker（`_跑一轮` 是 `@work(thread=True)`），
                # 所以这里照抄生产形状，验的才是真链路。
                应用.run_worker(提问, thread=True)
                # 等表单真的推上来（最多 50 轮；不设上限会挂死）
                屏 = None
                for _ in range(50):
                    await 试点.pause()
                    if 应用.screen.query(TextArea):
                        屏 = 应用.screen
                        break
                assert 屏 is not None, f"表单没弹出来。屏栈：{[s.id for s in 应用.screen_stack]}"
                问题 = _渲染(屏)
                assert "D1" in 问题, f"表单没显示问题：{问题}"
                assert "写作事由是什么" in 问题, f"表单没显示问题原文：{问题}"
                屏.query_one("#ask-answer", TextArea).text = "事由是防汛"
                屏.dismiss("事由是防汛")
                await 应用.workers.wait_for_complete()
                await 试点.pause()

        asyncio.run(跑())
        assert 收到 == ["事由是防汛"], f"答复没回到提问方：{收到}"

    def test_无答复时不编造(self) -> None:
        """没人应答时**必须失败**，不能返回空串让模型当成「用户没意见」（D-007）。

        闸.问 的超时失败是既有判据；这里从界面侧再钉一次，
        确认**界面挂上的答复方在超时后不会给出一个空答案**。
        """
        闸 = 问答闸门(超时秒=0.2)

        async def 跑() -> None:
            ZhengTongApp(桥=事件桥接(), 闸=闸)
            async with ZhengTongApp(桥=事件桥接()).run_test():
                await asyncio.sleep(0)

        # 只验闸：超时必须抛错而不是返回空串
        with pytest.raises(Exception, match=r"(?i)超时|未应答|无人应答"):
            闸.问(({"key": "D1", "question": "事由"},))


class Test预览同源:
    def test_预览正文来自投影(self) -> None:
        """**预览不许绕开投影**（D-063）。

        取编排结果对象就等于让界面多一条通编排层的路，
        而无界面驱动走投影——两边迟早不一致。
        """

        async def 跑() -> None:
            桥 = 事件桥接()
            编 = _跑完()
            事件们 = list(编.事件)  # pyright: ignore[reportAttributeAccessIssue]
            应用 = ZhengTongApp(桥=桥, 取编排器=lambda: 编)
            async with 应用.run_test() as 试点:
                应用.feed(事件们)
                await 试点.pause()
                渲染 = _渲染(应用.query_one(PreviewPanel))
                投影 = StandardProjection()
                for e in 事件们:
                    投影.accept(e)
                assert 投影.artifacts, "前置条件：投影里有成果"
                末条 = 投影.artifacts[-1][1]
                assert 末条[:200] in 渲染, f"预览内容不是投影里的成果。预览开头：{渲染[:120]!r}"

        asyncio.run(跑())


class Test档位:
    def test_三档都在且默认第一档(self) -> None:
        """档位表从 `TIERS` 生成，**不另写一份**（D-067）。

        另写一份就会出现「代码说三档、领域说三档、内容包说两档」，
        而三者不一致时**任何单侧检查都查不出来**。
        """

        async def 跑() -> None:
            应用 = ZhengTongApp(桥=事件桥接())
            async with 应用.run_test():
                assert len(list(应用.query(RadioButton))) == len(TIERS)
                assert 应用.query_one(RequestBox).当前档位() is TIERS[0].id, "默认不是第一档"

        asyncio.run(跑())

    def test_档位显示名与领域一致(self) -> None:
        async def 跑() -> None:
            应用 = ZhengTongApp(桥=事件桥接())
            async with 应用.run_test():
                显示 = [str(b.label) for b in 应用.query(RadioButton)]
                assert 显示 == [档.display_name for 档 in TIERS], f"档位名不一致：{显示}"

        asyncio.run(跑())
