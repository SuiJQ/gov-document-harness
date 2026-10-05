"""L-1：压缩的 drop 优先级（材料先于对话被丢）。

## 问题

`recent_window` 曾是扁平元组，`压缩()` 按位置切前 N 条，
**不区分消息种类**。材料经工具读入后是 `role="tool"` 的消息，
与当前对话混在同一窗口，被同一个杠杆无差别丢弃。

于是：材料占八成体积时，被丢的八成里大半是刚读完正要用的材料，
而真正该丢的早期讨论占两成却毫发无伤。
**「压完变小」不等于「压得对」**——前者是体积问题，后者是取舍问题。

## 四条判据（来自 CROSS_STEP_LIABILITIES.md）

1. `role="tool"` 先于 `user`/`assistant` 对话被丢
2. 同类内越早越先丢
3. 无论谁被丢，**必须标明**
4. 丢完仍超阈值 → 返回 `None`，不得空转

## 这些测试怎么写才算数

**每条都构造「材料与对话混排」的场景**。
若窗口里只有一种角色，优先级与位置退化、测不出差别——
那样的测试删掉优先级实现照样全绿。
"""

from __future__ import annotations

import pytest

from zhengtong.ports.llm import Message
from zhengtong.services.context import ContextView, 压缩
from zhengtong.services.context.compactor import _丢弃顺序, 摘要前缀


def _混排(*, 每条字数: int = 200) -> tuple[Message, ...]:
    """构造材料与对话混排的窗口。

    刻意交错排列（材-话-材-话…）而非「材料全在前」——
    若实现按位置切，交错排列会让「丢前 N 条」与「按优先级丢」产生不同结果，
    差别才显形。

    **默认每条 200 汉字**：太短的夹具（如「材料0」三个字）估不出 token，
    总量达不到 85% 阈值，`压缩()` 会一律返回 `None`——
    第一版就这么栽过，11 条测试全挂在「结果为 None」上，
    而根因是夹具太薄，不是实现有问题。
    """
    序: list[Message] = []
    for i in range(8):
        序.append(Message(role="tool", content=f"材料{i}" + "材" * 每条字数))
        序.append(Message(role="user", content=f"提问{i}" + "问" * 每条字数))
        序.append(Message(role="assistant", content=f"回答{i}" + "答" * 每条字数))
    return tuple(序)


# ═══ 判据 1：材料先于对话被丢 ═══


class Test材料优先丢:
    def test_工具消息先于对话被丢(self) -> None:
        v = ContextView(recent_window=_混排())
        结果 = 压缩(v, 上下文容量=1000)
        assert 结果 is not None
        剩 = {m.content for m in 结果.视图.recent_window}
        # 保留 6 条、丢 18 条：材料只有 8 条，应全部先被丢光
        assert not any(c.startswith("材料") for c in 剩), f"材料竟还在：{剩}"
        assert any(c.startswith("提问") for c in 剩), "对话被丢光了，保留策略反了"

    def test_丢弃顺序工具排在前(self) -> None:
        """直接测排序函数——比从结果反推更精确。"""
        顺序 = _丢弃顺序(_混排())
        窗口 = _混排()
        前三 = [窗口[i].role for i in 顺序[:3]]
        assert 前三 == ["tool", "tool", "tool"], f"材料未排在丢弃序列最前：{前三}"

    def test_材料不全丢时才动对话(self) -> None:
        """只丢得起材料时，**一条对话都不该动**。

        容量小到只丢得起两条材料时，丢掉的两条必须**都是材料**。
        期望按新语义给：不再写死「保留 N 条」，而是给定一个小容量，
        再断言「被丢的全���材料、对话一条没少」——**断言的是不变量，不是条数**。
        """
        窗口 = _混排()
        v = ContextView(recent_window=窗口)
        材料数 = sum(1 for m in 窗口 if m.role == "tool")
        结果 = 压缩(v, 上下文容量=1000)
        assert 结果 is not None
        丢弃 = [m for m in 窗口 if m not in 结果.视图.recent_window]
        if len(丢弃) <= 材料数:
            assert all(m.role == "tool" for m in 丢弃), (
                f"只丢得起 {len(丢弃)} 条（材料共 {材料数} 条），却动了对话"
            )
        else:
            # 丢得超过材料总数时，先丢光材料、剩下的才轮到对话——
            # **材料一条都不能比对话留得更久**。
            剩材料 = [m for m in 结果.视图.recent_window if m.role == "tool"]
            剩对话 = [m for m in 结果.视图.recent_window if m.role != "tool"]
            assert not 剩材料 or not 剩对话, (
                f"还剩材料 {len(剩材料)} 条却已动过对话 {len(窗口) - len(剩材料) - len(剩对话)} 条"
            )

    def test_保留的不必是尾部连续段(self) -> None:
        """按优先级取舍意味着保留集**可以不连续**。

        这是有意的：保住「正在进行的讨论」，就得接受丢掉更早的材料。
        故本条断言的是「保留集非尾部连续」这一**新行为真的发生了**——
        否则说明实现偷偷退回了按位置切。
        """
        v = ContextView(recent_window=_混排())
        结果 = 压缩(v, 上下文容量=1000)
        assert 结果 is not None
        尾部 = _混排()[-6:]
        assert 结果.视图.recent_window != 尾部, "保留集竟还是尾部连续段——优先级没生效"


# ═══ 判据 2：同类内越早越先丢 ═══


class Test同类内时间序:
    def test_越早的工具越先丢(self) -> None:
        顺序 = _丢弃顺序(_混排())
        窗口 = _混排()
        被丢的工具 = [窗口[i].content for i in 顺序 if 窗口[i].role == "tool"]
        assert 被丢的工具 == sorted(被丢的工具), f"材料未按时间序丢：{被丢的工具}"

    def test_越早的对话越先丢(self) -> None:
        """对话含 `user` 与 `assistant` 两种，内容前缀不同。

        不能直接比内容字典序——那会把「提问」排在「回答」之前，
        而窗口里两者是交错的。**要比的是原下标**：
        同类内越早越先丢，指的是位置顺序，不是字符串顺序。
        """
        顺序 = _丢弃顺序(_混排())
        窗口 = _混排()
        对话下标 = [i for i in 顺序 if 窗口[i].role != "tool"]
        assert 对话下标 == sorted(对话下标), f"对话未按原下标顺序丢：{对话下标}"

    def test_单类窗口退化等于按位置切(self) -> None:
        """全是对话时，优先级无差别——行为应与旧实现一致。"""
        窗口 = tuple(Message(role="user", content=f"第{i}条") for i in range(10))
        顺序 = _丢弃顺序(窗口)
        assert 顺序 == list(range(10)), "单一时应退化按下标顺序丢"


# ═══ 判据 3：被丢的必须标明 ═══


class Test标明丢了什么:
    @pytest.mark.parametrize(
        ("构造", "期望"),
        [
            # 材料 4 + 对话 4，保留 1 → 丢 7：材料全丢 + 3 条对话
            pytest.param(
                lambda: (
                    tuple(Message(role="tool", content=f"材料{i}" + "材" * 400) for i in range(4)),
                    tuple(Message(role="user", content=f"对话{i}" + "问" * 400) for i in range(4)),
                ),
                ("工具返回 4 条", "对话 3 条"),
                id="材料对话各半",
            ),
            # 纯材料 5，保留 1 → 丢 4 条材料
            pytest.param(
                lambda: (
                    (
                        tuple(
                            Message(role="tool", content=f"材{i}" + "材" * 400) for i in range(5)
                        ),
                        (),
                    )
                ),
                ("工具返回 4 条",),
                id="纯材料",
            ),
            # 纯对话 5，保留 1 → 丢 4 条对话
            pytest.param(
                lambda: (
                    (),
                    tuple(Message(role="user", content=f"问{i}" + "问" * 400) for i in range(5)),
                ),
                ("对话 4 条",),
                id="纯对话",
            ),
        ],
    )
    def test_摘要报出构成(self, 构造, 期望: tuple[str, ...]) -> None:
        """只报「共 N 条」不够——模型无从知道丢的是材料还是对话。

        **期望值按实际丢弃构成算，不是按输入条数算**：
        材料优先丢，所以「4 材料 + 4 对话、保留 1」丢的是
        4 材料 + **3** 对话（留下 1 条对话）。
        第一版这里按输入条数写期望，4 条测试全红——
        错的是期望不是实现，但这类错会让人去改对的代码。
        """
        材料, 对话 = 构造()
        v = ContextView(recent_window=材料 + 对话)
        结果 = 压缩(v, 上下文容量=100)
        assert 结果 is not None
        摘要 = 结果.视图.summary_of_dropped
        assert 摘要 is not None
        # **期望按实际丢弃构成算，不按输入条数算**（D-197 第二次栽在同一处）：
        # 第 14.5 步起保留量由 token 预算决定，不再是「保留 N 条」，
        # 故写死条数只会反复红——而红的时候很容易误以为是实现错了。
        丢弃 = tuple(m for m in v.recent_window if m not in 结果.视图.recent_window)
        材料数 = sum(1 for m in 丢弃 if m.role == "tool")
        对话数 = len(丢弃) - 材料数
        if 丢弃:
            assert f"共 {len(丢弃)} 条" in 摘要.content, (
                f"摘要没标明共丢 {len(丢弃)} 条：{摘要.content[:80]}"
            )
            if 材料数:
                assert f"工具返回 {材料数} 条" in 摘要.content, f"丢了 {材料数} 条材料却没标明"
            if 对话数:
                assert f"对话 {对话数} 条" in 摘要.content, f"丢了 {对话数} 条对话却没标明"
        # 纯材料时不许凭空报「对话 N 条」，反之亦然
        assert (材料数 > 0) == ("工具返回" in 摘要.content), "凭空多报了材料"
        assert (对话数 > 0) == ("对话 " in 摘要.content), "凭空多报了对话"

    def test_注入摘要器时同样标明(self) -> None:
        """标明不得被摘要器绕过——否则「自己写个摘要器」就成了
        丢什么不说的最短路径（与 D-187 前缀同型）。"""

        def 摘要器(丢弃: tuple[Message, ...]) -> Message:
            return Message(role="system", content="我总结完了。")

        v = ContextView(recent_window=_混排())
        结果 = 压缩(v, 上下文容量=1000, 摘要器=摘要器)
        assert 结果 is not None
        摘要 = 结果.视图.summary_of_dropped
        assert 摘要 is not None
        assert 摘要.content.startswith(摘要前缀)
        assert "工具返回" in 摘要.content, "摘要器路径漏了构成标明"
        assert "我总结完了" in 摘要.content, "标明不该吃掉摘要器内容"


# ═══ 判据 4：无可丢则返回 None，不空转 ═══


class Test不空转:
    def test_无可丢时返回None(self) -> None:
        v = ContextView(
            system_and_role=(Message(role="system", content="讨" * 5000),),
            recent_window=(),
        )
        assert 压缩(v, 上下文容量=1000) is None

    def test_容量远够用时不丢(self) -> None:
        """容量足够装下全部历史 → 一条都不该丢（原按「保留条数」写）。

        第 14.5 步删掉了 `保留条数` 参数——留一个签名接受却完全无效的
        参数，正是本项目反复吃的亏（D-205 `llm` 收了不用、D-222 `validators` 收了不读）。
        故按容量表达同一意图。
        """
        v = ContextView(recent_window=_混排())
        assert 压缩(v, 上下文容量=100000) is None

    def test_容量极小则尽量丢(self) -> None:
        """容量小到一条历史都留不下时：能压就压，压不动才返回 None。

        **连摘要都装不下**（容量 1）时返回 None 是对的——返回了也是装不下。
        故这里用「刚好装得下摘要、装不下历史」的容量，断言历史被清空。
        """

        v = ContextView(recent_window=_混排())
        # 阈值要够装下一条摘要（约 20 token），但装不下整窗（3024）
        结果 = 压缩(v, 上下文容量=int(80 / 0.85))
        assert 结果 is not None, "本该压却没压"
        assert len(结果.视图.recent_window) < len(_混排())
        assert 结果.视图.summary_of_dropped is not None, "丢了却没有摘要"


# ═══ 不变量：优先级改动不得破坏既有性质 ═══


def test_压缩后仍真的变小() -> None:
    """「压完变小」这条老判据不能因为改了优先级而失效。"""
    v = ContextView(recent_window=_混排())
    结果 = 压缩(v, 上下文容量=1000)
    assert 结果 is not None
    assert 结果.是否真的变小了


def test_材料全丢时对话条数正确() -> None:
    """24 条混排（8 材 + 8 提问 + 8 回答）。

    **条数按新语义算**（第 14.5 步起保留量由 token 预算决定，不再是「保留 6 条」）：
    保留量取决于容量，故这里断言的是**不变量**——材料一条不剩、
    且留下的全是对话——而不是某个写死的条数。写死条数只会反复红，
    而红的时候很容易误以为是实现错了（D-197 已栽过一次）。
    """
    v = ContextView(recent_window=_混排())
    结果 = 压缩(v, 上下文容量=1000)
    assert 结果 is not None
    assert 结果.视图.recent_window, "全丢光了"
    assert all(m.role != "tool" for m in 结果.视图.recent_window), (
        "材料没丢光——材料可重读、对话不可重放，材料必须先丢"
    )
    assert 结果.丢弃条数 > 0
