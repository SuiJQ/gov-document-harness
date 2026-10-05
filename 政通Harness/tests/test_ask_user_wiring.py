"""`flow.ask_user` 的提问回调真的接上了（补第 0 步，D-286）。

## 此前为什么是坏的

`flow.ask_user` 的 handler 是：

```python
字段 = p.get("fields")
return Result.ok(d.问用户(字段 if isinstance(字段, tuple) else ()))
```

而工具参数来自 **`json.loads(原始参数)`**（`http_llm.py:220`）。
**JSON 数组解析出来是 `list`，永远不可能是 `tuple`。**

于是 `isinstance(字段, tuple)` 恒为假 → 回调**永远收到空元组**。

**今天没发作**，因为 `问用户` 回调在生产代码里零接线，
每次都在这一行之前就 `_未接线(...)` 返回了。
**一旦把回调接上，这个缺陷立刻激活**——
模型问了一个问题，收到的却是「空问题」的答案。

> 这是本项目的第 N 次同型：**被前置的失败挡住、因而从未执行的代码，
> 里面藏着第二个缺陷**。接缝不接，下面的一切都验不到。

## 本文件验两件事

1. 回调**接得上**（不是 `_未接线`）
2. 字段**原样送到**——`list` 不能被吞成空元组

## 为什么第 2 条必须单独验

它与第 1 条**完全解耦**：回调可以接上、返回成功，
而字段照样被吞掉。**只看「有没有接线」会漏掉它。**
"""

from __future__ import annotations

import json

import pytest

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
from zhengtong.services.tools.handlers import flow_ask_user, 工具依赖, 问用户回调


def _依赖(*, 问用户: 问用户回调 | None = None) -> 工具依赖:
    """造一个只关心「问用户」回调的工具依赖。端口一律给假实现。

    **形参写死这一个**，不用 `**覆盖: object` ——
    那样 unpacking 出来的类型是 `object`，13 个回调字段会一起报类型错误，
    而要治它就得写 `# type: ignore`（D-018 禁止）。
    """
    return 工具依赖(
        storage=FakeStorage(),
        user_files=FakeUserFiles(),
        retriever=FakeRetriever(),
        content_pack=FakeContentPack(),
        validators=FakeValidators(),
        run_id=RunId("r-0001"),
        问用户=问用户,
    )


class Test字段不能被吞成空元组:
    """**D-286 的核心。** JSON 数组是 `list`，不是 `tuple`。"""

    def test_list字段原样送到回调(self) -> None:
        收到: list[tuple[dict[str, object], ...]] = []

        def 问(字段: tuple[dict[str, object], ...]) -> str:
            收到.append(字段)
            return "用户答：已确认"

        依赖 = _依赖(问用户=问)
        # **模拟真实解析路径**：json.loads 出来的就是 list
        参数 = json.loads('{"fields": [{"key": "D1", "question": "写作事由是什么"}]}')
        逐个 = 参数["fields"]
        assert isinstance(逐个, list), "前置条件：JSON 数组解析成 list"

        got = flow_ask_user(依赖, RoleId.TEAM_LEAD, 参数)
        assert got.is_ok, got
        assert len(收到) == 1, "回调没被调用"
        assert len(收到[0]) == 1, f"字段被吞成空了：收到 {收到[0]!r}"
        assert 收到[0][0]["key"] == "D1", f"字段内容不对：{收到[0][0]}"

    def test_空fields与缺fields都传空(self) -> None:
        """字段缺失不该崩，但也别凭空造出字段。"""
        收到: list[tuple[dict[str, object], ...]] = []
        依赖 = _依赖(问用户=lambda f: (收到.append(f), "好")[1])
        _ = flow_ask_user(依赖, RoleId.TEAM_LEAD, {"fields": []})
        _ = flow_ask_user(依赖, RoleId.TEAM_LEAD, {})
        assert 收到 == [(), ()], f"空字段处理不对：{收到!r}"

    def test_非list字段不当字段用(self) -> None:
        """模型传了字符串等畸形结构，不该当成字段数组。"""
        收到: list[tuple[dict[str, object], ...]] = []
        依赖 = _依赖(问用户=lambda f: (收到.append(f), "好")[1])
        got = flow_ask_user(依赖, RoleId.TEAM_LEAD, {"fields": "不是数组"})
        assert got.is_ok
        assert 收到 == [()], f"畸形字段被当真了：{收到!r}"


class Test未接线时必须失败:
    """**不能静默成功**——D-165 的形态：零件都测过，真跑什么也没做。"""

    def test_回调为None时失败(self) -> None:
        got = flow_ask_user(_依赖(), RoleId.TEAM_LEAD, {"fields": [{"key": "D1"}]})
        assert got.is_error, "没接线却报成功"

    def test_失败信息指向缺口(self) -> None:
        got = flow_ask_user(_依赖(), RoleId.TEAM_LEAD, {"fields": []})
        assert got.is_error, "没接线却报成功"
        错 = got.error
        assert 错 is not None, "is_error 为真却没有 error"
        assert "问用户回调" in 错.detail


class Test工厂能传入回调:
    """**第 0 步的实质：接线不只是 handler 的事，装配层也得能传。**"""

    def test_建工具运行器接受问用户(self) -> None:
        import inspect

        形参 = inspect.signature(建工具运行器).parameters
        assert "问用户" in 形参, f"工厂没有「问用户」参数，实际有：{list(形参)}"

    def test_回调真的落到工具层(self) -> None:
        收到: list[tuple[dict[str, object], ...]] = []
        运行器 = 建工具运行器(
            storage=FakeStorage(),
            user_files=FakeUserFiles(),
            retriever=FakeRetriever(),
            content_pack=FakeContentPack(),
            validators=FakeValidators(),
            run_id=RunId("r-0001"),
            问用户=lambda f: (收到.append(f), "用户答：已确认")[1],
        )
        assert 运行器._d.问用户 is not None, "回调没落到工具依赖上"

        got = 运行器.调用(
            "flow.ask_user",
            角色=RoleId.TEAM_LEAD,
            参数={"fields": [{"key": "D1", "question": "事由"}]},
        )
        assert got.is_ok, f"调用失败：{got.error if got.is_error else ''}"
        assert len(收到) == 1 and len(收到[0]) == 1, f"字段被吞：{收到!r}"


class Test权限:
    def test_非主理人不得提问(self) -> None:
        """`flow.ask_user` 仅主理人可用——权限判据不许因接线而放松。"""
        运行器 = 建工具运行器(
            storage=FakeStorage(),
            user_files=FakeUserFiles(),
            retriever=FakeRetriever(),
            content_pack=FakeContentPack(),
            validators=FakeValidators(),
            run_id=RunId("r-0001"),
            问用户=lambda _f: "好",
        )
        got = 运行器.调用(
            "flow.ask_user",
            角色=RoleId.DOC_ADVISOR,  # 文择体，不是主理人
            参数={"fields": [{"key": "D1"}]},
        )
        assert got.is_error, "非主理人提问竟然成功了"


@pytest.mark.parametrize("角色", [RoleId.TEAM_LEAD])
def test_主理人可提问(角色: RoleId) -> None:
    运行器 = 建工具运行器(
        storage=FakeStorage(),
        user_files=FakeUserFiles(),
        retriever=FakeRetriever(),
        content_pack=FakeContentPack(),
        validators=FakeValidators(),
        run_id=RunId("r-0001"),
        问用户=lambda _f: "用户答：已确认",
    )
    got = 运行器.调用("flow.ask_user", 角色=角色, 参数={"fields": [{"key": "D1"}]})
    assert got.is_ok


class Test走装配根真接上了:
    """**这是本文件真正要守的东西**（D-287）。

    前两组判据验的是「零件能用」——
    工厂能传、handler 不吞字段、闸门会等。

    但本项目反复出现的形态是：
    **零件全对、判据全绿，而真实路径上一次都没走到过**
    （D-195 工具层零引用、D-231 参数没往下传、D-286 回调零接线）。

    故这里验的是**装配根建出来的那个 App**，
    而不是手工构造的工具依赖。
    """

    def test_App持有闸门(self) -> None:
        from zhengtong.app import build_app
        from zhengtong.domain.ids import RunId

        应用 = build_app(RunId("r-0001"))
        assert 应用.问答 is not None, "App 没持有闸门——界面拿不到，提问方永远等不到"

    def test_工具层的问用户真的是闸门(self) -> None:
        """**不许用「没接线」冒充接上了。**
        传进去的必须是闸门的方法本身，不是包装过的空壳。
        """
        from zhengtong.app import build_app
        from zhengtong.domain.ids import RunId

        应用 = build_app(RunId("r-0001"))
        assert 应用.tools._d.问用户 is not None, "工具层的问用户还是 None"
        assert 应用.tools._d.问用户 == 应用.问答.问, "工具层接的不是 App 持有的那个闸"

    def test_通过App提问能真拿到答案(self) -> None:
        """**端到端**：从 App 出发提问，挂上答复方，拿回答案。

        这一条走的是完整接缝：
        `建工具运行器(问用户=闸.问)` → `工具依赖.问用户`
        → handler → 闸门 → 答复方。
        """
        from zhengtong.app import build_app
        from zhengtong.domain.ids import RunId

        应用 = build_app(RunId("r-0001"))
        应用.问答.挂答复(lambda 字段: f"收到 {len(字段)} 个字段")
        got = 应用.tools.调用(
            "flow.ask_user",
            角色=RoleId.TEAM_LEAD,
            参数={"fields": [{"key": "D1"}, {"key": "D2"}]},
        )
        assert got.is_ok, "装配根提问失败"
        assert got.unwrap() == "收到 2 个字段", got.unwrap()


class Test提示词告诉了模型用哪个工具:
    """**第 2 层**（D-287）。

    此前提示词只说「必须一次性发出方向确认询问」，
    **没说通过什么工具发**——模型得自己猜该调什么。

    而 `grep ask_user content/prompts/*.md` **零命中**。

    ## 这条为什么值得单列判据

    提示词是**软数据**：Ruff 看不见、类型检查器看不见、
    运行期也不校验「内容对不对」，只校验哈希。

    故它很容易在改版时被无声改回去，而**没有任何机制会报警**——
    除非专门写一条判据盯着。
    """

    def _提示词(self) -> str:
        from zhengtong.app import 内容包根

        return (内容包根() / "prompts" / "team-lead.md").read_text(encoding="utf-8")

    def test_提示词点名了flow_ask_user(self) -> None:
        assert "flow.ask_user" in self._提示词(), (
            "提示词没告诉模型用哪个工具问——模型不会问，而接线做得多好都没用（D-287）"
        )

    def test_提示词禁止用文字直接问(self) -> None:
        """**只说「要问」不够**，还得说「不能光用嘴问」——
        否则模型会直接在正文里写一句问题，然后自己往下编。"""
        文本 = self._提示词()
        assert "flow.ask_user" in 文本
        assert "编造" in 文本, "提示词没重申「不得编造」（D-007）"

    def test_内容包哈希与提示词一致(self) -> None:
        """改了提示词必须同步 manifest 哈希，否则运行期启动即报错。

        **这条是为了让「改了忘了重算」变成红的**——
        有 `scripts/recalc_content_hash.py` 可修。
        """
        from zhengtong.adapters.content_pack import 本地内容包
        from zhengtong.app import 内容包根

        r = 本地内容包(内容包根()).verify_manifest()
        if r.is_error:
            错 = r.error
            assert 错 is not None
            pytest.fail(f"内容包哈希不符：{错.detail}")
