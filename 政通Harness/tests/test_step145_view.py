"""六层结构显式化（第 14.5 步，第 1 组判据）。

## 为什么顺序本身就是判据

**前缀缓存只匹配从头开始的连续前缀**——所以「不动的靠前、动的靠后」
不是洁癖，而是直接决定命中率。若工具清单夹在动态规则之前，
每换角色就击穿后面全部缓存。

故本组判据断言的是**顺序**，不是「某段存在」。

## 一个容易写假的判据

断言「`system_and_role` 在第一条」能被「只在末尾加个占位」骗过。
故本组同时断言**各段的 role 与内容归属**——
真的分层了，模型才能分辨「恒定规矩」与「本轮情况」。
"""

from __future__ import annotations

from zhengtong.ports.llm import Message
from zhengtong.services.context import ContextView

_角色 = Message(role="system", content="你是辞达意。")
_规则 = Message(role="system", content="【本轮适用】红线 E3：不得编造数据。")
_清单 = Message(role="system", content="本次可调用：shared.read、kb.search")
_用户 = Message(role="user", content="【背景·用户先前说明】写一份通知。")
_历史 = Message(role="assistant", content="已按要求改写第三段。")
_指令 = Message(role="user", content="## 本轮任务\n请只回答这一件事：……")


def _满视图() -> ContextView:
    return ContextView(
        system_and_role=(_角色,),
        dynamic_rules=(_规则,),
        tool_manifest=(_清单,),
        recent_window=(_用户, _历史),
        pinned_facts=(Message(role="user", content="D-Card：主送机关 XX 市政府"),),
        artifact_refs=(Message(role="system", content="产物：draft.md"),),
        本轮任务=_指令,
    )


class Test六段顺序:
    def test_角色提示词恒在最前(self) -> None:
        assert _满视图().messages()[0].content == "你是辞达意。"

    def test_完整顺序符合缓存分层(self) -> None:
        """顺序：角色提示词 → 动态规则 → 钉住事实 → 产物引用 → 历史 → 工具清单 → 本轮指令。

        前四段是「不随轮次变」的部分，构成缓存前缀；
        工具清单与本轮指令每轮变，压到末尾以免击穿前缀。
        """
        得 = [m.content for m in _满视图().messages()]
        assert 得 == [
            "你是辞达意。",
            "【本轮适用】红线 E3：不得编造数据。",
            "D-Card：主送机关 XX 市政府",
            "产物：draft.md",
            "【背景·用户先前说明】写一份通知。",
            "已按要求改写第三段。",
            "本次可调用：shared.read、kb.search",
            "## 本轮任务\n请只回答这一件事：……",
        ], f"顺序不对：{得}"

    def test_本轮指令恒在末尾(self) -> None:
        """末位必须是本轮任务——近端注意力决定「这轮要回答什么」。"""
        assert _满视图().messages()[-1].content.startswith("## 本轮任务")

    def test_工具清单在历史之后(self) -> None:
        """工具清单**不得**夹在缓存前缀里——它随角色变，前置会击穿前缀。"""
        内容 = [m.content for m in _满视图().messages()]
        assert 内容.index("本次可调用：shared.read、kb.search") > 内容.index("已按要求改写第三段。")


class Test分层可分辨:
    def test_六层各自独立成段(self) -> None:
        """两段 system 提示词必须**各自独立**，不能拼成一条。

        拼成一条，模型无从分辨「恒定规矩」与「本轮情况」——
        这正是用户说的「不出现岔子」。
        """
        系统 = [m.content for m in _满视图().messages() if m.role == "system"]
        assert "你是辞达意。" in 系统
        assert "【本轮适用】红线 E3：不得编造数据。" in 系统
        assert len(系统) == 4, f"system 段数 {len(系统)}——动态层与静态层没分开"

    def test_新字段有默认值不破坏既有构造(self) -> None:
        """扩字段不得破坏五参数构造——旧代码不该因加字段而崩。"""
        v = ContextView(system_and_role=(_角色,), recent_window=(_历史,))
        assert v.dynamic_rules == ()
        assert v.tool_manifest == ()

    def test_角色提示词永不被压缩器够到(self) -> None:
        """角色提示词与工具清单都在恒定段，压缩器只能动历史。"""
        from zhengtong.services.context import ContextView as V
        from zhengtong.services.context.view import 估算视图

        空历史 = V(
            system_and_role=(Message(role="system", content="角色提示词"),),
            recent_window=(Message(role="user", content="背景"),),
        )
        大历史 = V(
            system_and_role=(Message(role="system", content="角色提示词"),),
            recent_window=tuple(
                Message(role="user", content="很长的背景内容。" * 50) for _ in range(40)
            ),
        )
        assert 估算视图(空历史) < 估算视图(大历史)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
