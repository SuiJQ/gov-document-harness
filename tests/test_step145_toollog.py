"""工具动作入序列、结果不入序列 + `tool.result` 查回（第 14.5 步，第 6 组判据）。

## 为什么只记动作不记结果

用户的原话：「工具调用要进对话序列，但是只记录调用这个动作，不记录结果，
模型可以通过一个工具查询某一次工具调用的结果」。

这是 token 与能力的正确取舍：**记「我做过什么」，结果按需查回**。
全记则上下文迅速被材料正文撑爆（D-197 已记录「工具返回先丢」）；
全不记则模型不知道自己查过什么，会重复查询。

## 三个存储必须分开（否则违反 D-035）

| | 读者 | 内容 | 寿命 |
|---|---|---|---|
| 审计条目 | 人 / 排障 | **只有摘要** | 写 journal |
| 调用存档 | **模型查回** | 完整结果 | 只在内存 |
| 对话序列 | 模型 | **只有动作** | 本轮运行 |

把调用存档并进审计条目，就等于「把材料正文抄进日志」，违反 D-035；
用户也已定：只放内存，不落盘。
"""

from __future__ import annotations

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.orchestration import 编排器
from zhengtong.ports.llm import Message, ToolCall
from zhengtong.services.tools.calllog import 调用存档


class Test调用存档:
    def test_记了能取回(self) -> None:
        存档 = 调用存档()
        存档.记("call-1", "材料全文：XX 市政府关于老旧小区改造的通知……")
        assert 存档.取("call-1") == "材料全文：XX 市政府关于老旧小区改造的通知……"

    def test_未知调用id不编造(self) -> None:
        """查不到就说查不到——**不得返回空串冒充「无结果」**。

        返回空串的话，模型会以为「我查过了，确实没有」，
        于是不再重查。这是编造证据的另一种形态。
        """
        存档 = 调用存档()
        assert 存档.取("不存在的id") is None

    def test_存档不落盘(self) -> None:
        """只放内存：随进程消失（用户决定）。"""
        存档 = 调用存档()
        存档.记("call-1", "x")
        assert not hasattr(存档, "路径"), "调用存档不该有任何落盘接口"
        assert not hasattr(存档, "保存"), "调用存档不该有任何落盘接口"


class Test动作入序列结果不入:
    def test_动作消息带tool_calls(self) -> None:
        """动作消息用 assistant + tool_calls 表示。"""
        动 = Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(id="c1", name="shared.read", arguments={"name": "a.md"}),),
        )
        assert 动.tool_calls, "动作消息必须带 tool_calls"

    def test_记入历史只接受模型回复(self) -> None:
        """记入路径产出的应是 **assistant 回复**，不是 `role="tool"` 的结果。

        用行为而非源码断言：`_记入历史` 被调用后，历史里新增的消息
        角色必须是 assistant。查源码（`inspect.getsource`）会被注释
        措辞牵着走——那是另一种形式的脆弱判据。
        """
        from zhengtong.domain.phase import PhaseId
        from zhengtong.domain.role import RoleId

        编 = 编排器(
            llm=FakeLlm(),
            storage=FakeStorage(),
            模型名="m",
            门禁取证=lambda _请求: {},
        )
        过程 = 编._建过程()
        前 = len(过程.历史.get(RoleId.LANGUAGE, []))
        过程._记入历史(RoleId.LANGUAGE, PhaseId.DRAFTING, "模型写了一段正文")
        后 = 过程.历史.get(RoleId.LANGUAGE, [])
        assert len(后) == 前 + 2, "记入历史应成对追加（提问 + 回复）"
        assert 后[-1].role == "assistant"
        assert 后[-2].role == "user"
        assert all(m.role != "tool" for m in 后), "记入路径产出了 tool 消息"

    def test_工具调用可在历史中被查到(self) -> None:
        """动作消息里带 tool_calls，可据此查回结果。

        这是「只记动作」的前提：模型得知道**自己查过什么**，
        否则会重复查询。
        """
        编 = 编排器(
            llm=FakeLlm(),
            storage=FakeStorage(),
            模型名="m",
            门禁取证=lambda _请求: {},
        )
        过程 = 编._建过程()
        from zhengtong.domain.role import RoleId

        过程.记工具动作(
            RoleId.LANGUAGE,
            ToolCall(id="c1", name="shared.read", arguments={"name": "a.md"}),
        )
        历史 = 过程.历史.get(RoleId.LANGUAGE, [])
        动作 = [m for m in 历史 if m.tool_calls]
        assert 动作, "工具动作没进历史——模型不知道自己查过什么"
        assert 动作[-1].tool_calls[0].id == "c1"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
