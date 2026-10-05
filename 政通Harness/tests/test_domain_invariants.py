"""L0 领域层不变式测试。

这些测试守的是**结构性约束**，不是单个函数的行为——
因为这类错误一旦写进代码，排查成本极高（比如门禁少了一道，
要到真跑起来才发现某道红线无人拦截）。
"""

from __future__ import annotations

import pytest

from zhengtong.domain.gate import FORMAL_GATES, GATES, GateId, gate_groups
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
        """**这条断言在 D-143 之前形同虚设。**

        原断言只检查「每组是单元素、成员是已知角色」，
        而 `GATE_GROUPS` 当时是一份**独立声明**的表，
        与 `GATES` 没有任何交叉校验——所以它漏掉 TEAM_LEAD 组
        与 G13 长达整个审计都没人发现。
        """
        for group in gate_groups():
            assert len(group) == 1
            assert group[0] in ROLE_BY_ID

    def test_并行分组须与GATES一致(self) -> None:
        """**D-143 的防漂移断言：分组是推导出来的，不是另建的表。**

        `gate_groups()` 由 `GATES[...].owners` 推导，本测试锁死两者一致。
        今后若改了某道门禁的 `owners` 而推导规则没跟上，这里会红。

        额外锁死两件事（它们都是 D-143 里真实漏掉的）：
        - **TEAM_LEAD 组必须在**：G0/G6/G12 的取证方都是主理人
        - **G13 不得进任何组**：它是 STRUCTURE + COMPLIANCE 双取证方，
          并进去会让双取证的门禁与单取证并行，破坏 D-084 的语义
        """
        groups = gate_groups()

        # 1. 单元素独占组 == 由 owners 推导出的全部组
        推导 = {
            (spec.owners[0],)
            for spec in GATES.values()
            if not spec.dynamic_owners and len(spec.owners) == 1
        }
        assert set(groups) == 推导, "分组与 GATES.owners 不一致"

        # 2. TEAM_LEAD 组（D-143 漏掉的第一个）
        assert (RoleId.TEAM_LEAD,) in groups, "TEAM_LEAD 组缺失（G0/G6/G12 会无人认领）"

        # 3. G13 不得进组（D-143 漏掉的第二个）
        assert all(len(g) == 1 for g in groups), "双取证门禁不得并入并行组"
        assert RoleId.STRUCTURE not in {g[0] for g in groups if len(g) > 1}


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
