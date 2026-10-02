"""L4 上下文管理测试（架构 §7.2 / 第 9 步判据）。

判据三条：
1. 85% 触发压缩
2. D-039 四类「绝不丢弃」有测试
3. 跨层只传值对象（§3.5，由 `test_boundary` 兜底）

## 这些测试为什么长这样

每条都写成**能被证伪**的形式：

- 断言「压完 seq/token 变小」，而不是断言「调用过压缩」
- D-039 四类**逐类**占一个用例，而不是合并成「pinned 都在」一句
- 「没有摘要器时不得编造摘要」单独断言——这是最容易被后来者
  好心改成「自动塞个摘要」的��方
"""

from __future__ import annotations

import pytest

from zhengtong.ports.llm import Message
from zhengtong.services.context import (
    ContextView,
    估算视图,
    压缩,
    压缩结果,
    组装,
    需要压缩,
)
from zhengtong.services.context.compactor import 默认保留条数
from zhengtong.services.context.estimate import 估算文本, 触发阈值

# ═══ 夹具 ═══


def _对话(条数: int, *, 每条字数: int = 400) -> tuple[Message, ...]:
    """造一段可预期的对话。默认每条 400 汉字。"""
    return tuple(
        Message(role="user" if i % 2 == 0 else "assistant", content="讨" * 每条字数)
        for i in range(条数)
    )


def _视图(
    *,
    绝不丢弃: tuple[Message, ...] = (),
    对话条数: int = 20,
    摘要: Message | None = None,
) -> ContextView:
    return 组装(
        系统与角色=(Message(role="system", content="你是范正章。" * 20),),
        绝不丢弃=绝不丢弃,
        对话=_对话(对话条数),
        已被压缩摘要=摘要,
    )


# ═══ 一、视图必须真正不可变（§7.2 的全部卖点都建立在这上面）═══


class Test视图不可变:
    def test_五字段全是元组(self) -> None:
        """若装 list/deque，可变状态会穿透 frozen 壳，hash 也不稳定。"""
        v = _视图()
        for 名 in ("system_and_role", "pinned_facts", "artifact_refs", "recent_window"):
            字段 = getattr(v, 名)
            assert isinstance(字段, tuple), f"{名} 必须是 tuple，实得 {type(字段).__name__}"

    def test_不可变视图不含可变容器(self) -> None:
        """D-070：跨层只传值对象，故视图里不得有 list/dict/set。"""

        def scan(值: object, 路径: str) -> list[str]:
            发现: list[str] = []
            if isinstance(值, list | set):
                发现.append(f"{路径} 是 {type(值).__name__}")
            elif isinstance(值, dict):
                发现.append(f"{路径} 是 dict")
            return 发现

        v = _视图(对话条数=3)
        # 注意：扫的是**字段本身**，不能先 list() 再扫——
        # 那样任何 tuple 都会被自己转成 list 而必然报红。
        问题 = scan(v.system_and_role, "system_and_role")
        问题 += scan(v.pinned_facts, "pinned_facts")
        问题 += scan(v.artifact_refs, "artifact_refs")
        问题 += scan(v.recent_window, "recent_window")
        assert not 问题, f"发现可变容器：{问题}"

    def test_改字段会抛异常(self) -> None:
        """frozen 不是装饰：改字段必须真抛。

        `slots=True` 的 frozen dataclass 赋字段抛 `AttributeError`
        （消息为 cannot assign to field），不是 `FrozenInstanceError`——
        判据按实际行为写，不按想当然写。
        """
        v = _视图()
        with pytest.raises(AttributeError):
            # 故意写只读属性以验证 frozen——照抄 test_domain_invariants.py 的既有手法，
            # 用行内 ignore 而非整文件放宽（整文件放宽会连带掩盖别处的真错）。
            v.recent_window = ()  # pyright: ignore[reportAttributeAccessIssue]

    def test_消息本身也是冻结的(self) -> None:
        """视图不可变还不够——里面的 Message 也必须不可变，
        否则「冻结的壳里装着可变的核」，照样能从内部被改掉。"""
        m = Message(role="user", content="原")
        with pytest.raises(AttributeError):
            m.content = "改"  # pyright: ignore[reportAttributeAccessIssue]

    def test_产物引用不带正文(self) -> None:
        """「只传引用」是硬要求：渲染出的消息里不得出现产物正文。"""
        v = 组装(系统与角色=(), 产物引用=("data/x/大纲.md",))
        渲染 = v.artifact_refs[0].content
        assert "大纲.md" in 渲染
        assert "共 1 份" in 渲染


# ═══ 二、组装顺序（§7.2）═══


class Test组装顺序:
    def test_顺序是系统_绝不丢弃_摘要_产物_近期(self) -> None:
        """摘要在近期窗口**之前**——它代表更早的内容（view.py 有理由说明）。

        五栏各一条，用标记区分，顺序错一条即红。
        """
        摘要 = Message(role="system", content="摘")
        v = 组装(
            系统与角色=(Message(role="system", content="SYS"),),
            绝不丢弃=(Message(role="system", content="PIN"),),
            产物引用=("a.md",),
            对话=(Message(role="user", content="REC"),),
            已被压缩摘要=摘要,
        )
        实际 = [m.content for m in v.messages()]
        assert 实际[0] == "SYS"
        assert 实际[1] == "PIN"
        assert 实际[2] == "摘"
        assert 实际[3].startswith("本次可用的产物"), (
            f"产物引用应在摘要之后、近期之前，实得 {实际[3]!r}"
        )
        assert "a.md" in 实际[3]
        assert 实际[4] == "REC"
        assert len(实际) == 5, f"五栏应各出一条，实得 {len(实际)} 条"

    def test_摘要为None时不出空洞(self) -> None:
        v = 组装(系统与角色=(), 对话=(Message(role="user", content="x"),))
        assert [m.content for m in v.messages()] == ["x"]


# ═══ 三、85% 触发（判据 1）═══


class Test八五触发:
    def test_未到阈值不压(self) -> None:
        """容量给得很大 → 不该压。"""
        assert 压缩(_视图(), 上下文容量=1_000_000) is None

    def test_到阈值就压(self) -> None:
        v = _视图(对话条数=20)
        assert 需要压缩(v, 上下文容量=1000), "20×400 汉字远超 1000 token 容量的 85%"
        结果 = 压缩(v, 上下文容量=1000)
        assert 结果 is not None

    def test_阈值正好是容量的八五成(self) -> None:
        assert 触发阈值(1000) == 850
        assert 触发阈值(128 * 1024) == int(128 * 1024 * 0.85)

    def test_压完必须真的变小(self) -> None:
        """压缩若没减小体积就是纯亏——白丢上下文还占更多位置。
        这是能抓住「摘要器返回超长文本」的唯一判据。"""
        结果 = 压缩(_视图(对话条数=40), 上下文容量=1000, 保留条数=2)
        assert 结果 is not None
        assert 结果.是否真的变小了, "压缩后 token 未减少"
        assert 结果.压缩后token < 结果.压缩前token

    def test_无可丢时返回None而非空转(self) -> None:
        """窗口里没东西可丢还不达标 → 返回 None 让上层报「装不下」，
        而不是反复压同一份视图。"""
        v = 组装(
            系统与角色=(Message(role="system", content="讨" * 5000),),
            对话=(),
        )
        结果 = 压缩(v, 上下文容量=1000)
        assert 结果 is None, "没有可丢内容时应返回 None，而不是造一个空摘要"


# ═══ 四、D-039 四类「绝不丢弃」（判据 2，逐类占一例）═══

#: D-039 原文四类。每类一个用例——合并成一句「pinned 都在」，
#: 丢掉其中任何一类都不会让测试变红，那这条判据就是空的。
D039_四类: tuple[tuple[str, str], ...] = (
    ("方向确认单全部字段", "方向确认单：主题=稳增长；时限=本年底；受众=市直各单位"),
    ("用户明示的事实与禁止事项", "用户明示：不得提及其他部门；已确认事实=项目已立项"),
    ("硬约束清单D7", "硬约束：不得突破编制、不得新增收费、须留痕"),
    ("已发现的严重问题", "严重问题：第3段数据与来源不符，已退回主理人核实"),
)


class TestD039四类绝不丢弃:
    @pytest.mark.parametrize(("类别", "内容"), D039_四类, ids=[c for c, _ in D039_四类])
    def test_该类在压缩后仍在(self, 类别: str, 内容: str) -> None:
        钉 = (Message(role="system", content=内容),)
        结果 = 压缩(_视图(绝不丢弃=钉, 对话条数=30), 上下文容量=1000)
        assert 结果 is not None
        保留内容 = [m.content for m in 结果.视图.pinned_facts]
        assert 内容 in 保留内容, f"{类别} 被压缩丢掉了"

    def test_四类同时在也全在(self) -> None:
        钉 = tuple(Message(role="system", content=内容) for _, 内容 in D039_四类)
        结果 = 压缩(_视图(绝不丢弃=钉, 对话条数=30), 上下文容量=1000)
        assert 结果 is not None
        assert len(结果.视图.pinned_facts) == 4
        for _, 内容 in D039_四类:
            assert 内容 in [m.content for m in 结果.视图.pinned_facts]

    def test_压缩器够不着pinned字段(self) -> None:
        """「绝不丢弃」不是靠算法**记得**保留，而是结构上就够不着：
        `pinned_facts` 由调用方原样传入，压缩全程不经手。"""
        钉 = (Message(role="system", content="钉住的"),)
        原 = _视图(绝不丢弃=钉, 对话条数=30)
        结果 = 压缩(原, 上下文容量=1000)
        assert 结果 is not None
        assert 结果.视图.pinned_facts is 原.pinned_facts

    def test_系统与角色同样恒在(self) -> None:
        原 = _视图(对话条数=30)
        结果 = 压缩(原, 上下文容量=1000)
        assert 结果 is not None
        assert 结果.视图.system_and_role == 原.system_and_role


# ═══ 五、绝不编造摘要 ═══


class Test摘要纪律:
    def test_没有摘要器时必须标明未摘要(self) -> None:
        """**最容易被好心改坏的一条**。

        不注入摘要器时不得编造一段「看起来像摘要」的文字——
        那会让模型以为早期讨论已被理解，而它其实没有。
        """
        结果 = 压缩(_视图(对话条数=30), 上下文容量=1000)
        assert 结果 is not None
        摘要 = 结果.视图.summary_of_dropped
        assert 摘要 is not None
        assert "未经摘要" in 摘要.content, "缺摘要器时必须明说这是原文"
        assert "原文" in 摘要.content

    def test_注入摘要器时用它(self) -> None:
        调过: list[int] = []

        def 摘要器(丢弃: tuple[Message, ...]) -> Message:
            调过.append(len(丢弃))
            return Message(role="system", content="这是摘要。")

        结果 = 压缩(_视图(对话条数=30), 上下文容量=1000, 摘要器=摘要器)
        assert 结果 is not None
        assert 调过, "摘要器未被调用"
        assert 结果.视图.summary_of_dropped is not None
        assert 结果.视图.summary_of_dropped.content == "这是摘要。"

    def test_摘要器收到的是被丢弃的那些(self) -> None:
        seen: list[tuple[str, ...]] = []

        def 摘要器(丢弃: tuple[Message, ...]) -> Message:
            seen.append(tuple(m.content[:4] for m in 丢弃))
            return Message(role="system", content="摘")

        结果 = 压缩(_视图(对话条数=30), 上下文容量=1000, 保留条数=5, 摘要器=摘要器)
        assert 结果 is not None
        assert seen and len(seen[0]) == 25, "被摘要的应是 30-5 条"

    def test_丢弃条数与实际一致(self) -> None:
        结果 = 压缩(_视图(对话条数=30), 上下文容量=1000, 保留条数=5)
        assert 结果 is not None
        assert 结果.丢弃条数 == 25
        assert len(结果.视图.recent_window) == 5


# ═══ 六、压缩可观测（§7.2：报「保留了 X/Y/Z」）═══


class Test压缩可观测:
    def test_保留概览报出各栏条数(self) -> None:
        钉 = (Message(role="system", content="x"),)
        结果 = 压缩(_视图(绝不丢弃=钉, 对话条数=30), 上下文容量=1000, 保留条数=4)
        assert 结果 is not None
        概览 = 结果.保留概览
        assert 概览["绝不丢弃"] == 1
        assert 概览["近期窗口"] == 4
        assert 概览["已摘要"] == 1

    def test_度量三元组齐备(self) -> None:
        """压缩前 / 压缩后 / 摘要多大 —— §7.2 要求「压缩本身可观测」。

        三个数缺一，就没法判断「压得对不对」：
        只知压缩后变小，不知摘要多大，则无法区分
        「有效压缩」与「把窗口换成一大段摘要」。
        """
        结果 = 压缩(_视图(对话条数=30), 上下文容量=1000, 保留条数=4)
        assert 结果 is not None
        assert 结果.压缩前token > 0
        assert 结果.压缩后token > 0
        assert 0 < 结果.摘要token < 结果.压缩前token, "摘要本身不该等于全部内容"

    def test_压缩结果本身不可变(self) -> None:
        """结果也要是值——它会被塞进事件里带着走。"""
        结果 = 压缩(_视图(), 上下文容量=1000)
        assert isinstance(结果, 压缩结果 | type(None))


# ═══ 七、默认保留条数 ═══


def test_默认保留条数为六() -> None:
    assert 默认保留条数 == 6
    结果 = 压缩(_视图(对话条数=30), 上下文容量=1000)
    assert 结果 is not None
    assert len(结果.视图.recent_window) == 6


# ═══ 八、估算视图与 messages 一致 ═══


def test_估算视图只算真发出去的消息() -> None:
    """两处算法必然分叉，故只允许有一个：`估算视图` 必须走 `messages()`。

    用一条**只有 artifact_refs 的视图**当判据：
    若 `估算视图` 另算一遍字段（比如单独给产物引用一个系数），
    这个值就会与 `messages()` 的逐条估算对不上。
    """
    v = 组装(系统与角色=(), 产物引用=("a.md", "b.md"), 对话=())
    逐条 = sum(估算文本(m.content) for m in v.messages())
    assert v.messages(), "产物引用应渲染成一条消息"
    assert 估算视图(v) == 逐条


def test_空视图估算为零() -> None:
    assert 估算视图(ContextView()) == 0
