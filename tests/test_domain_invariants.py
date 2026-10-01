"""L0 领域层不变式测试。

这些测试守的是**结构性约束**，不是单个函数的行为——
因为这类错误一旦写进代码，排查成本极高（比如门禁少了一道，
要到真跑起来才发现某道红线无人拦截）。
"""

from __future__ import annotations

import pytest

from zhengtong.domain.gate import FORMAL_GATES, GATE_GROUPS, GATES, GateId
from zhengtong.domain.role import ROLE_BY_ID, ROLES, RoleId, display_name


class Test门禁编号:
    def test_编号共14项(self) -> None:
        assert len(GateId) == 14

    def test_正式门禁12道(self) -> None:
        assert len(FORMAL_GATES) == 12

    def test_自检项不占序号(self) -> None:
        assert GateId.G0.is_self_check
        assert GateId.G6.is_self_check
        assert not GateId.G1.is_self_check
        assert len(FORMAL_GATES) == len(GateId) - 2

    def test_每道门禁都有定义(self) -> None:
        for gate in GateId:
            assert gate in GATES, f"{gate} 缺定义"

    def test_每道门禁都有中文名(self) -> None:
        for gate in GateId:
            assert GATES[gate].name, f"{gate} 缺名称"


class Test取证方映射:  # D-084
    def test_静态取证方均非空且在五角色内(self) -> None:
        for gate in FORMAL_GATES:
            spec = GATES[gate]
            if spec.dynamic_owners:
                continue
            assert spec.owners, f"{gate} 无取证方"
            for owner in spec.owners:
                assert owner in ROLE_BY_ID

    def test_G9取证方动态(self) -> None:
        """G9 按所触发的红线归属退，运行期才定，故静态为空。"""
        assert GATES[GateId.G9].dynamic_owners
        assert GATES[GateId.G9].owners == ()

    def test_G13是跨组门禁(self) -> None:
        """D-084：G13 跨组，写成单值就丢了一半取证方。"""
        owners = GATES[GateId.G13].owners
        assert len(owners) == 2
        assert RoleId.STRUCTURE in owners
        assert RoleId.COMPLIANCE in owners

    def test_每道静态门禁都有责任方_除G9(self) -> None:
        """这是 D-084 补漏的核心断言：曾漏掉 G9/G11/G13 的归属。"""
        for gate in FORMAL_GATES:
            if gate is GateId.G9:
                continue
            assert GATES[gate].owners, f"{gate} 漏了取证方"

    def test_并行分组均含五角色(self) -> None:
        for group in GATE_GROUPS:
            assert len(group) == 1
            assert group[0] in ROLE_BY_ID


class Test角色定义:  # D-080
    def test_恰好五个角色(self) -> None:
        assert len(ROLES) == 5

    def test_内部标识与界面文案分离(self) -> None:
        """英文标识绝不等于中文文案——否则就是一套东西，不是两层。"""
        for role in ROLES:
            assert role.id.value.isascii()
            assert not role.display_name.isascii()

    def test_display_name全为中文(self) -> None:
        assert display_name(RoleId.TEAM_LEAD) == "范正章"
        assert display_name(RoleId.STRUCTURE) == "陈纲举"
        assert display_name(RoleId.LANGUAGE) == "辞达意"
        assert display_name(RoleId.COMPLIANCE) == "骆准绳"
        assert display_name(RoleId.DOC_ADVISOR) == "文择体"

    def test_str返回中文而非英文(self) -> None:
        """f"{role}" 出现在界面上时必须是中文。"""
        for role in ROLES:
            assert str(role) == role.display_name

    def test_每个角色都有职责描述(self) -> None:
        for role in ROLES:
            assert role.duty


@pytest.mark.parametrize("gate", list(GateId))
class Test门禁与角色均不可变:
    def test_门禁定义不可变(self, gate: GateId) -> None:
        spec = GATES[gate]
        with pytest.raises(AttributeError):
            spec.name = "改名"  # pyright: ignore[reportAttributeAccessIssue]  # 故意写只读属性以验证 frozen

    def test_角色定义不可变(self, gate: GateId) -> None:
        del gate
        role = ROLES[0]
        with pytest.raises(AttributeError):
            role.display_name = "改名"  # pyright: ignore[reportAttributeAccessIssue]  # 故意违反 frozen 以验证
