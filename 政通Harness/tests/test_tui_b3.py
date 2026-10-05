"""B3：一个框变两种角色 + 键位收敛。

## 这一轮改的是「操控方式」，不是功能

B1 埋了 `ctrl+p / ctrl+o / ctrl+d` 三个键——**那是我凭空猜的**，
用户第一次见不可能把无关联的字母对上号，`ctrl+p` 还与 Textual 内置
命令面板撞键。评估后收敛为**三个键，且都是高频动作**。

## 判据守四件事

1. **键位就是那三个，且 `ctrl+q` 真的解绑了**
2. **只有一个输入框**（不是两个），角色随状态变
3. **插话真的发得出去**，且空的不发
4. **完成态禁用**，不做成「再写一份」

## 关于「证伪无效」留下的那条

补 `test_阶段中途提交不打断本阶段` 时才发现：
已有的插话判据**全部在阶段开始前提交**，
根本进不到「阶段进行中」这个窗口——

> **判据没覆盖的那个窗口，恰恰是这个功能最容易坏的地方。**
> D-012 整条决策的实质是「不打断正在飞的请求」，
> 而没有任何一条判据在测它。
"""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Static, TextArea

from zhengtong.adapters.tui import RequestBox
from zhengtong.adapters.tui import ZhengTongApp as App类
from zhengtong.presentation.tui import 事件桥接

pytest.importorskip("textual", reason="TUI 需要 textual")


def _框文本(应用) -> str:
    return "\n".join(str(w.content) for w in 应用.query_one(RequestBox).query(Static))


class Test键位收敛:
    def test_只有三个键(self) -> None:
        from zhengtong.adapters import tui_theme as 主题

        assert set(主题.键位表) == {"ctrl+enter", "escape", "ctrl+o"}, (
            f"键位表与结论不符：{sorted(主题.键位表)}"
        )

    def test_退出来自O(self) -> None:
        """退出绑 `ctrl+o`（Out），**不是 `ctrl+q`**。

        `ctrl+q` 是 Textual 的默认退出键。保留它等于两个键都能退，
        而退出是低频动作——记两个比记一个更容易记混。
        """
        from zhengtong.adapters.tui import ZhengTongApp

        键 = set()
        for b in ZhengTongApp.BINDINGS:
            k = b[0] if isinstance(b, tuple) else b.key
            键.update(str(k).split(","))
        assert "ctrl+o" in 键, f"退出没绑 ctrl+o：{键}"
        # **不断言 `ctrl+q` 不在 BINDINGS 里**——
        # 覆盖默认绑定的办法就是给它绑一个空动作，那条绑定必然在表里。
        # 「ctrl+q 还在」这件事由 `test_ctrl_q按了不退` 从行为上断：
        # **配置里有它不等于它生效**，判据该断行为。
        # （第一版在这儿断言「不在表里」，和「用它覆盖默认」自相矛盾。）

    def test_ctrl_q按了不退(self) -> None:
        """**解绑是行为，不是配置。**配置删了不等于按键失效。"""

        async def 跑() -> None:
            应用 = App类(桥=事件桥接())
            async with 应用.run_test() as 试点:
                await 试点.press("ctrl+q")
                await 试点.pause()
                assert 应用.is_running, "ctrl+q 把程序关了——没解绑成功"

        asyncio.run(跑())

    def test_低频专属键已从表里撤掉(self) -> None:
        from zhengtong.adapters import tui_theme as 主题

        assert set(主题.键位表_未实现) == {"ctrl+p", "ctrl+d", "ctrl+q"}, (
            "「明确不做」清单要与结论一致，免得日后当成漏做又捡回来"
        )


class Test一个框两种角色:
    def test_只有一个输入框(self) -> None:
        """**两个框是本轮明确否决的方案。**

        用户打开程序看到两个输入框，不知道该往哪个打字——
        要靠标签、颜色、提示语去区分，都是在给不该存在的问题打补丁。
        """

        async def 跑() -> None:
            应用 = App类(桥=事件桥接())
            async with 应用.run_test():
                assert len(list(应用.query(TextArea))) == 1, "界面出现了多个输入框"

        asyncio.run(跑())

    def test_未开始是诉求且有提示(self) -> None:
        async def 跑() -> None:
            应用 = App类(桥=事件桥接())
            async with 应用.run_test():
                文本 = _框文本(应用)
                assert "诉求" in 文本, f"标题不是诉求：{文本}"
                提示 = str(应用.query_one("#need", TextArea).placeholder or "")
                assert "要写什么公文" in 提示, f"提示没说要写什么：{提示!r}"
                assert "Ctrl+Enter" in 提示, f"提示没说怎么提交：{提示!r}"

        asyncio.run(跑())

    def test_运行中变插话且变矮(self) -> None:
        async def 跑() -> None:
            应用 = App类(桥=事件桥接())
            async with 应用.run_test():
                框 = 应用.query_one(RequestBox)
                框.切到插话()
                await asyncio.sleep(0)
                文本 = _框文本(应用)
                assert "插话" in 文本, f"没切成插话：{文本}"
                提示 = str(应用.query_one("#need", TextArea).placeholder or "")
                assert "下一个阶段生效" in 提示, f"插话提示没说何时生效：{提示!r}"

        asyncio.run(跑())

    def test_切角色后焦点还在框里(self) -> None:
        """**焦点跳走就毁了这个设计。**

        用户提交完通常马上想补充，焦点跳走还得再点一次。
        """

        async def 跑() -> None:
            应用 = App类(桥=事件桥接())
            async with 应用.run_test() as 试点:
                框 = 应用.query_one(RequestBox)
                框.切到插话()
                框.清空并交焦点()
                await 试点.pause()
                assert 应用.focused is 应用.query_one("#need", TextArea), (
                    f"焦点没留在输入框：{应用.focused}"
                )

        asyncio.run(跑())


class Test插话发得出去:
    def test_空的不发且明说(self) -> None:
        """空插话**要说**，不能静默吞——否则用户以为那句丢了。"""

        async def 跑() -> None:
            from zhengtong.adapters.tui import StatusLine

            应用 = App类(桥=事件桥接())
            async with 应用.run_test():
                应用._跑着 = True
                应用._送插话("   ")
                文案 = str(应用.query_one("#status", StatusLine).content)
                assert "空" in 文案, f"空插话没提示：{文案}"

        asyncio.run(跑())

    def test_非运行中不发且明说(self) -> None:
        async def 跑() -> None:
            from zhengtong.adapters.tui import StatusLine

            应用 = App类(桥=事件桥接())
            async with 应用.run_test():
                应用._跑着 = False
                应用._送插话("重点突出防汛")
                文案 = str(应用.query_one("#status", StatusLine).content)
                assert "不在运行中" in 文案, f"非运行中没说清：{文案}"

        asyncio.run(跑())

    def test_运行中ctrl_enter送插话(self) -> None:
        """`Ctrl+Enter` 在运行中 = 送插话，**共用一个键**。

        分成两个键就是让用户先判断状态再选键，
        而状态在界面上明明写着——那是把判断推给用户。
        """

        async def 跑() -> None:
            from zhengtong.adapters.tui import StatusLine

            收到: list[str] = []

            class 假编排:
                """**四成员齐全**，不是「刚好够插话用」。

                界面依赖 `_界面编排器` 协议（bus/已取消/请求取消/收用户指令）。
                原先这里只写了 `收用户指令` 一个方法——那是「替身刚好够用」，
                与 D-239 同族：判据在跑，但它断言的是自己造的东西。
                协议一收紧，pyright 立刻报赋值不兼容，这才暴露出来。
                """

                bus = None
                已取消 = False

                def 请求取消(self) -> None: ...

                def 收用户指令(self, 文本: str) -> None:
                    收到.append(文本)

            应用 = App类(桥=事件桥接(), 取编排器=lambda: 假编排())
            async with 应用.run_test() as 试点:
                应用._跑着 = True
                应用._当前编排器 = 假编排()
                应用.query_one("#need", TextArea).text = "重点突出防汛"
                await 试点.pause()
                await 试点.press("ctrl+enter")
                await 试点.pause()
                assert 收到 == ["重点突出防汛"], f"插话没发出去：{收到}"
                文案 = str(应用.query_one("#status", StatusLine).content)
                assert "下一个阶段" in 文案, f"没说何时生效：{文案}"

        asyncio.run(跑())


class Test完成态:
    def test_完成后禁用且明说(self) -> None:
        """**不做成「再写一份」**——那会引入重跑语义（新建 run？覆盖产物？）。"""

        async def 跑() -> None:
            应用 = App类(桥=事件桥接())
            async with 应用.run_test():
                框 = 应用.query_one(RequestBox)
                框.锁定()
                await asyncio.sleep(0)
                文本 = _框文本(应用)
                assert "已完成" in 文本, f"没说完成：{文本}"
                assert 应用.query_one("#need", TextArea).disabled, "完成后输入框还能打字"

        asyncio.run(跑())

    def test_完成后提示如何再来一次(self) -> None:
        async def 跑() -> None:
            应用 = App类(桥=事件桥接())
            async with 应用.run_test():
                应用.query_one(RequestBox).锁定()
                await asyncio.sleep(0)
                提示 = str(应用.query_one("#need", TextArea).placeholder or "")
                assert "重新启动" in 提示, f"没说怎么再来一次：{提示!r}"

        asyncio.run(跑())
