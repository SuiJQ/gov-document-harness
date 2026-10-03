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
