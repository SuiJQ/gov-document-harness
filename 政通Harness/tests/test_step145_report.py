"""汇报进主理人上下文 + 工具动作入序列（第 14.5 步，第 4 组判据）。

## 汇报为什么必须有 sink

`msg.report_to_lead` 的回调签名是 `(内容, 依据) -> str`，
主理人的答复**即返即弃**——不进任何历史。

于是成员汇报完就「失忆」了：主理人下一轮看不到谁报过什么、报了什么结论。
这正是用户要求的「主理人接受汇报的历史」。

## 与产物的分工（不建第二事实源）

- **汇报** = 主理人上下文里的一条记忆（「谁说了什么」）
- **共享区产物** = 跨角色传递正文的载体

两者不重复：产物存正文，汇报存「发生过这次沟通」这个事实。
"""

from __future__ import annotations

import pytest

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.domain.run_state import GateRecord
from zhengtong.orchestration import 编排器


def _编(**kw: object) -> 编排器:
    return 编排器(
        llm=FakeLlm(),
        storage=FakeStorage(),
        模型名="test-model",
        门禁取证=lambda 请求: {g.value: GateRecord(gate=g.value, passed=True) for g in 请求.门禁},
        **kw,  # pyright: ignore[reportArgumentType]
    )


class Test汇报回调接线:
    def test_编排器暴露汇报回调(self) -> None:
        """编排器必须提供汇报回调，否则工具层走「未接线」失败路径。"""
        编 = _编()
        assert getattr(编, "工具回调", None) is not None, (
            "编排器没暴露工具回调——工具层会走 D-192「入口未接线」"
        )

    def test_汇报回调把内容写进主理人历史(self) -> None:
        """汇报内容必须落到**主理人**的历史里（D-041 每角色一份）。"""
        编 = _编()
        from zhengtong.domain.role import RoleId

        回调 = 编.工具回调
        assert 回调 is not None
        回 = 回调.汇报("【陈纲举】结构已改，第三段重排。", {"phase": "drafting"})
        assert isinstance(回, str), "汇报回调必须返回主理人答复"

        过程 = 编._建过程()
        历史 = 过程.历史.get(RoleId.TEAM_LEAD, [])
        assert 历史, "汇报没进主理人历史"
        全 = "\n".join(m.content for m in 历史)
        assert "陈纲举" in 全, f"汇报内容不在主理人历史里：{全[:100]}"

    def test_汇报答复也进历史(self) -> None:
        """主理人的**答复**也要进历史——否则半截对话记不下来。"""
        编 = _编()
        from zhengtong.domain.role import RoleId

        编.工具回调.汇报("【辞达意】请确认文种。", {})
        过程 = 编._建过程()
        历史 = 过程.历史.get(RoleId.TEAM_LEAD, [])
        角色 = [m.role for m in 历史]
        assert "assistant" in 角色, "主理人答复没进历史"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
