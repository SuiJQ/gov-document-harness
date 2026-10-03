"""审计与压缩的实测数字必须进事件流（D-266）。

## 背景：三个事件定义了但从未发射

`domain/event.py` 里 `ROLE_TOOL_CALL` / `TOOL_DENIED` /
`CONTEXT_COMPACTED` **都已定义**，注释也写清了语义，
但全仓**没有任何发射点**。投影层也只消费 6 种事件。

故 D-258（审计零消费）与 D-261（压缩数字零消费）
**不是两个孤立缺陷，是同一件事**：事件已经现成，
缺的是在正确位置发出来。

## 为什么不加新接口

审计器有 `条目`/`该角色的条目`/`拒绝数` 三个导出接口，生产零调用。
「加一个消费方」也能让它非零消费——但那要新写读取代码 + 落盘代码。

发事件则**复用已有机制**（事件 → journal → 时间线回放），
且事件枚举已经写好了语义。**是实现没跟上，不是设计缺一块。**

## 测什么

**测事件流**：跑一次编排，从它发出的事件里断言事件存在且载荷有内容。
**不断**：工具是否放行、压缩是否触发——那些另有判据管。
一个判据只锁一件事，才知道红的是哪条。
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tests")
sys.path.insert(0, "src")


@pytest.fixture(autouse=True)
def _路径() -> None:
    pass


def _Pro():
    from zhengtong.domain.tier import TierId

    return TierId.PRO


def _长文编排(字数: int):
    """让模型回复很长、从而把上下文顶过 85% 的编排器。

    ## 为什么要控制回复长度

    压缩只在 `需要压缩(视图, 容量)` 为真时触发。
    `FakeLlm.default_text` 是可注入的——用它顶高体量，
    比改容量阈值更贴近真实情形（真实公文写作回复本来就长）。
    """
    from test_step14_orchestrate_gate import _编排
    from zhengtong.adapters.fakes import FakeContentPack, FakeLlm

    # **两个注入口缺一不可**（踩过一次）：
    # - 内容包：无它则走降级分支，提示词只有一句话，视图体量极小
    # - 容量token：默认 128_000，85% 阈值是 108_800，
    #   而单次请求实测仅数百~数千 token，**永远触发不了压缩**
    return _编排(
        内容包=FakeContentPack(),
        llm=FakeLlm(default_text="据现有材料就该项工作作出如下部署。" * 字数),
        容量token=800,
    )


class Test工具调用事件:
    def test_工具调用要发事件(self) -> None:
        """**本条是核心**：工具调用过一次，就该有 `role.tool_call` 事件。

        事件枚举已定义（`domain/event.py`），注释写明
        「工具调用必属某角色的派发，故用 role.tool_call」——
        但从未发射过。架构 §7.4 要求审计「全部进 journal」。

        ## 为什么要用「会请求工具的编排器」

        首版这里用普通编排器 + 断言「有工具调用事件」——
        **它永远红**：普通编排器下模型不请求工具，
        事件自然没有。红的原因不是缺陷，而是**测的不是那条路**。
        （与 D-266 记的两次判据踩坑同族。）

        故本组改用 `test_step19` 的带工具编排器：先让模型请求工具，
        再断言事件。**先有调用，再有事件**——顺序不能反。
        """
        from test_step19_tool_execution import _编排, _要工具的llm

        llm, _ = _要工具的llm()
        编 = _编排(llm)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")

        调用 = [e for e in 编.事件 if e.kind.value == "role.tool_call"]
        assert 调用, (
            "模型请求了工具，但没有 role.tool_call 事件——"
            "审计条目只在内存，journal 里查不到（架构 §7.4 未兑现）"
        )

    def test_事件要带工具名与角色(self) -> None:
        """**只发空事件等于没发**。

        事件是为了「事后查得到」存在的，空的 `role.tool_call` 等于没发。
        """
        from test_step19_tool_execution import _编排, _要工具的llm

        llm, _ = _要工具的llm()
        编 = _编排(llm)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")

        调用 = [e for e in 编.事件 if e.kind.value == "role.tool_call"]
        assert 调用, "没有 role.tool_call 事件"
        首 = 调用[0]
        载荷 = {str(k): str(v) for k, v in 首.data.items()}
        assert 载荷, f"role.tool_call 事件 data 为空：{首}"
        assert "kb.search" in 载荷.values(), f"载荷里没有工具名：{首.data}"
        # **按键查，不按值猜**（首版写了角色枚举的白名单，
        # 而首次工具调用发生在 intake/direction 阶段、角色是主理人，
        # 于是白名单没覆盖到——判据自己写错了）。
        assert 首.data.get("角色"), f"载荷里没有角色：{首.data}"
        assert 首.data.get("裁决"), f"载荷里没有裁决：{首.data}"

    def test_被拒绝的工具调用要发拒绝事件(self) -> None:
        """**拒绝比允许更该留痕**——守卫是安全边界（D-076）。

        `tool.denied` 已定义且注释明写它是「守卫拦截」，
        与角色派发无关，故独立成事件。

        ## 走真实链路而不是直接调 runner

        直接调 `runner.调用()` 测的是「单件能发事件」；
        本条要测的是**整条链路通**——工具请求 → 白名单拒绝 → 事件出。
        故让模型请求一个非白名单角色的工具。
        """
        from test_step19_tool_execution import _编排, _要工具的llm
        from zhengtong.domain.role import RoleId

        # 用一个不在 Pro 编制内的角色会直接被拒——
        # 故改为让模型请求一个**不存在的工具**，同样走 `_审计并返回`
        llm, _ = _要工具的llm(工具名="fs.write_file")
        编 = _编排(llm)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")

        拒绝 = [e for e in 编.事件 if e.kind.value == "tool.denied"]
        assert 拒绝, (
            "模型请求了已砍除的 fs.write_file，却没有任何 tool.denied 事件——守卫拦截不留痕（D-076）"
        )
        _ = RoleId


def _运行器():
    """造一个工具运行器（端口全用假实现）。"""
    from zhengtong.adapters.fakes import (
        FakeContentPack,
        FakeStorage,
        FakeUserFiles,
        FakeValidators,
    )
    from zhengtong.domain.errors import Result
    from zhengtong.domain.ids import RunId
    from zhengtong.services.tools.factory import 建工具运行器

    class 空检索:
        def build(self, docs: tuple[tuple[str, str], ...]) -> Result[None]:
            return Result.ok(None)

        def search(self, query: str, top_k: int = 8) -> Result[tuple[()]]:
            return Result.ok(())

    return 建工具运行器(
        storage=FakeStorage(),
        user_files=FakeUserFiles(),
        retriever=空检索(),
        content_pack=FakeContentPack(),
        validators=FakeValidators(),
        run_id=RunId("r-audit"),
    )


class Test压缩事件:
    def test_压缩发生时要发事件(self) -> None:
        """**本条是核心**：压缩发生了，journal 里查得到。

        `orchestrate.py:734` 只取 `压后.视图`，
        把 `丢弃条数`/`摘要token` 一起丢掉了。
        """
        编 = _长文编排(400)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")
        压缩 = [e for e in 编.事件 if e.kind.value == "context.compacted"]
        assert 压缩, (
            "长上下文跑完却无 context.compacted 事件——压缩省了多少、丢了什么，journal 里查不到"
        )

    def test_压缩事件要带丢弃条数(self) -> None:
        """**数字不进事件等于没算**。

        D-039 要求「被丢的必须标明构成」——
        那是**注入摘要器时**的要求；结果层的数字此前从未进过任何输出。
        """
        编 = _长文编排(400)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")
        压缩 = [e for e in 编.事件 if e.kind.value == "context.compacted"]
        assert 压缩, "无压缩事件"
        文本 = " ".join(str(v) for v in 压缩[0].data.values())
        assert any(c.isdigit() for c in 文本), f"压缩事件里没有任何数字：{压缩[0].data}"

    def test_压不动时也要发事件说明(self) -> None:
        """**「压不动」必须说出来**。

        `orchestrate.py:735` 的注释写着「压不动就照发」——
        那是诚实的选择，但**用户无从知道本地没压**。
        不发事件的话，超限与「没压」在 journal 里长得一样。
        """
        编 = _长文编排(20000)
        编.跑到终(档位=_Pro(), 需求="就旱情写个通知")
        压缩 = [e for e in 编.事件 if e.kind.value == "context.compacted"]
        assert 压缩, (
            "压不动时无事件——用户看到超限却不知道本地没压，「超限」与「没压」在 journal 里长得一样"
        )


_ = pytest
