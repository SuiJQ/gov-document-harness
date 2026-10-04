"""字形检测（D-045）与刷新耗时（B4）。

## 为什么不测「字体有没有汉字字形」

因为**测不了**。程序没法问终端「你能不能画『政』字」，
问出来的答案是「能」，然后显示成豆腐块 □□□。

所以本文件只钉**可测的那部分**，并且在判据里写明这个边界：
测不到的别假装测到了，否则将来有人以为「跑过检测就一定不乱码」。

真正有价值的是这个反直觉的发现：

> **GBK 能编码绝大多数汉字。**只看编码名会误判为「没问题」，
> 而界面里真会用的 `⚠ ▣ ✓ ✗` 有一部分不在 GBK 里，一写就抛异常。
> 故判据测的是**往返**（encode 出去再 decode 回来），不是编码名。

## 关于耗时判据

虚拟滚动没做（公文 100–300 行、上限 1000 行，差两个数量级），
但「每次刷新都 `remove_children()` + `mount()` 全量重排」这个风险是真的。
所以钉一条**耗时上界**——不是性能优化，是**回归哨兵**：
哪天有人把刷新改成 O(n²)，这里会红。

阈值给得宽松（`耗时上界秒 = 3.0`）：判据要抓的是数量级退化，
不是毫秒级抖动。定时判据卡在毫秒上是自找麻烦。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from textual.widgets import Static, TextArea

from zhengtong.adapters.terminal import 中文能否往返, 查终端显示能力
from zhengtong.adapters.tui import PreviewPanel, StatusLine, 预览上限字
from zhengtong.adapters.tui import ZhengTongApp as App类
from zhengtong.presentation.tui import 事件桥接

pytest.importorskip("textual", reason="TUI 需要 textual")

#: 刷新耗时上界（秒）。**给得宽松**：抓数量级退化，不抓毫秒抖动。
耗时上界秒 = 3.0


class 假流:
    """带指定编码的假流。

    ## 为什么**不继承 `io.TextIOBase`**

    第一版继承了，然后 `self.encoding = 编码` 直接抛
    `AttributeError: attribute 'encoding' of '_io._TextIOBase' objects is not writable`
    ——**那个属性是只读的**。

    继承 `TextIOBase` 只会带来一堆用不上的抽象方法，
    而这里只需要 `encoding` 与 `isatty()` 两样。
    故用普通类：**按消费方真正用到的东西来伪造**。

    `isatty` 做成方法而非属性，也是被实测教过一次——
    第一版写成属性，探测代码 `目标.isatty()` 抛 `TypeError: not callable`，
    顺带暴露出「探测代码自身没有兜底」这个真问题。
    """

    def __init__(self, 编码: str, *, 终端: bool = True) -> None:
        self._编码 = 编码
        self._终端 = 终端

    @property
    def encoding(self) -> str:
        return self._编码

    def isatty(self) -> bool:
        return self._终端


class Test中文往返:
    def test_utf8通过(self) -> None:
        好, 提示 = 中文能否往返(假流("utf-8"))
        assert 好, f"utf-8 应当通过：{提示}"
        assert 提示 == ""

    def test_gb18030通过(self) -> None:
        """GB18030 是 GBK 的超集，界面用的符号都在里头。"""
        好, 提示 = 中文能否往返(假流("gb18030"))
        assert 好, f"gb18030 应当通过：{提示}"

    def test_gbk写不出界面符号(self) -> None:
        """**本文件最重要的一条。**

        GBK 能编码绝大多数汉字——只看编码名会判成「没问题」，
        而界面里真会出现的 `⚠` 不在 GBK 里，**一写就抛**。
        """
        好, 提示 = 中文能否往返(假流("gbk"))
        assert not 好, "GBK 被判成可用——那会让乱码问题在运行中才炸"
        assert "⚠" in 提示 or "写不出" in 提示, f"提示没说清是哪个字符：{提示}"

    def test_ascii当然不行(self) -> None:
        好, 提示 = 中文能否往返(假流("ascii"))
        assert not 好
        assert "政" in 提示, f"提示该指出是哪个字：{提示}"

    def test_未知编码不当成通过(self) -> None:
        """不能因为 `LookupError` 就一路 `except Exception: return True`。"""
        好, 提示 = 中文能否往返(假流("根本不存在的编码"))
        assert not 好
        assert "未知" in 提示, f"提示没说清：{提示}"

    def test_探测不崩(self) -> None:
        """`isatty` 抛异常时，**探测自己变成提示**。

        这条路径存在的意义是「起不来之前先说清楚」——
        探测代码崩在 `getattr` 上，用户看到的是「程序起不来」，
        那就把「提示」这个功能本身变成了崩溃源。
        """

        class 坏流:
            encoding = "utf-8"

            def isatty(self) -> bool:
                raise OSError("终端句柄失效")

        结果 = 查终端显示能力(坏流())
        assert 结果.提示, "探测失败时必须给提示，不能静默"
        assert 结果.正常 is False


class Test探测结果:
    def test_非终端给提示(self) -> None:
        结果 = 查终端显示能力(假流("utf-8", 终端=False))
        assert not 结果.是终端
        assert "不是交互终端" in 结果.提示, f"没提示非终端：{结果.提示}"

    def test_正常时没有提示(self) -> None:
        结果 = 查终端显示能力(假流("utf-8", 终端=True))
        assert 结果.正常, f"utf-8 + 交互终端应当无提示：{结果.提示}"
        assert 结果.提示 == ""

    def test_医生能收到提示(self) -> None:
        """**D-045 的管道此前没人传值**——实测 `--doctor` 一个字形提示都没有。

        这条钉的是「管道真的通了」：`跑自检` 收了 `字体` 就该出现在报告里。
        """
        from zhengtong.presentation.doctor import 跑自检

        结果 = 跑自检(字体="输出编码「ascii」写不出字符「政」")
        报告 = 结果.报告()
        assert "ascii" in 报告, f"提示没进报告：{报告}"
        # **仅提示、不阻止**（用户明确要求）
        assert 结果.全通过, "字形不合格竟判成不通过——违反「仅提示不阻止」"


class Test界面显示提示:
    def test_有提示才显示(self) -> None:
        async def 跑() -> None:
            应用 = App类(桥=事件桥接(), 终端提示="终端宽度只有 40 列")
            async with 应用.run_test():
                文案 = str(应用.query_one("#notice", StatusLine).content)
                assert "终端提示" in 文案, f"提示没显示：{文案}"
                assert "40" in 文案

        asyncio.run(跑())

    def test_没提示就留空(self) -> None:
        """正常终端不该在界面上多一行字。"""

        async def 跑() -> None:
            应用 = App类(桥=事件桥接())
            async with 应用.run_test():
                文案 = str(应用.query_one("#notice", StatusLine).content)
                assert 文案 == "", f"没提示却显示了：{文案}"

        asyncio.run(跑())

    def test_有提示时仍能正常跑(self) -> None:
        """**仅提示、不阻止**：提示出现时该干的活照样能干。"""

        async def 跑() -> None:
            应用 = App类(桥=事件桥接(), 终端提示="编码有问题")
            async with 应用.run_test() as 试点:
                应用.query_one("#need", TextArea).text = "写一份防汛通知"
                await 试点.pause()
                assert 应用.query_one("#need", TextArea).text == "写一份防汛通知", (
                    "有终端提示就影响了输入——违反「仅提示不阻止」"
                )

        asyncio.run(跑())


class Test刷新耗时:
    """**回归哨兵**，不是性能优化。"""

    def test_满上限稿刷新不超上界(self) -> None:
        """把预览塞到 `预览上限字`（20000 字）再刷一次，测耗时。

        ## 为什么盯这个而不是上虚拟滚动

        公文正文 100–300 行、上限约 1000 行，虚拟滚动是给十万行以上准备的，
        差两个数量级。而真正的风险是**每次刷新都全量 `remove_children()`
        + `mount()` 重排**——事件来得密 + 稿件长时会卡。

        所以这里钉的是**上界**：哪天有人改成 O(n²)，这里会红。
        """

        async def 跑() -> float:
            应用 = App类(桥=事件桥接())
            async with 应用.run_test() as 试点:
                正文 = "政" * 预览上限字
                应用.query_one(PreviewPanel).显示(正文)
                await 试点.pause()
                开始 = time.perf_counter()
                应用.query_one(PreviewPanel).显示(正文)
                await 试点.pause()
                return time.perf_counter() - 开始

        耗时 = asyncio.run(跑())
        assert 耗时 < 耗时上界秒, (
            f"满稿刷新耗时 {耗时:.2f}s，超出上界 {耗时上界秒}s。全量重排可能变成了 O(n²)"
        )

    def test_短稿刷新更快(self) -> None:
        """短稿应当比满稿快。

        **这条比上界那条更能抓退化**：若有人改成 O(n²)，
        满稿仍可能在小体量下勉强过关，但两者的**比值**会立刻拉开。
        """

        def 测(字数: int) -> float:  # 同步包装：外层不是 async，就不能是 async
            async def 内() -> float:
                应用 = App类(桥=事件桥接())
                async with 应用.run_test() as 试点:
                    正文 = "政" * 字数
                    应用.query_one(PreviewPanel).显示(正文)
                    await 试点.pause()
                    开始 = time.perf_counter()
                    应用.query_one(PreviewPanel).显示(正文)
                    await 试点.pause()
                    return time.perf_counter() - 开始

            return asyncio.run(内())

        短 = 测(500)
        长 = 测(预览上限字)
        assert 长 < 耗时上界秒, f"满稿耗时 {长:.2f}s 超出上界"
        # **宽松比值**：O(n²) 在 40 倍差距下会是 1600 倍，
        # 而线性是 40 倍。取 8 倍留足余量，又足够抓住平方退化。
        assert 长 < max(短 * 8.0, 0.05), f"满稿 {长:.3f}s vs 短稿 {短:.3f}s——增长过快，疑似非线性"

    def test_超上限会截断并明说(self) -> None:
        """**上限本身也钉住**——它是被虚拟滚动替代掉的那道防线。

        截断必须**明说**：用户看到半篇稿子却以为看全了，
        比看不到更糟（D-007 的精神：不标明残缺）。
        """

        async def 跑() -> None:
            应用 = App类(桥=事件桥接())
            async with 应用.run_test():
                超长 = "政" * (预览上限字 + 1000)
                应用.query_one(PreviewPanel).显示(超长)
                await asyncio.sleep(0)
                文本 = "\n".join(str(w.content) for w in 应用.query_one(PreviewPanel).query(Static))
                assert "预览只显示前" in 文本, f"截断没明说：{文本[:120]}"

        asyncio.run(跑())
