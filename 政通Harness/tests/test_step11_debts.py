"""第 11 步随带落地的两笔欠账的判据。

- **D-116**：G3 在 Flash 降级给主理人时，成果里**必须带**
  「文种由主理人研判」这一行，且要向用户展示。
  架构明文：「要写成代码里的显式分支，不能靠提示词记得」。
  当前全仓**没有任何代码产生这个标注**——本文件补上。

- **D-196**：真实校验器。原先 `build_app_from_workspace` 里的
  `validators` **故意仍接假实现**，注释写着「第 11 步须改掉」。

## 判据怎么写才算数

**D-116 特别容易写假**：断言「某处存在这行字」能被
把字符串硬编码进提示词模板的实现骗过。
所以判据查的是**「降级这个事实是否被算出来」**——
即降级判定与标注生成是否绑定，而不是查某个文件里有几个字。
"""

from __future__ import annotations

from zhengtong.adapters.validators import 硬约束校验器
from zhengtong.domain.gate import GateId
from zhengtong.domain.role import RoleId
from zhengtong.domain.tier import TIER_BY_ID, TierId
from zhengtong.domain.validation import g3降级标注
from zhengtong.ports.validators import ValidatorPort

# ═══ D-116 ═══


class TestG3降级标注:
    def test_咨询档无门禁故无标注(self) -> None:
        """咨询档 `gates_apply=False`，压根不过 G3。"""
        assert g3降级标注(TierId.CONSULT) == ()

    def test_Pro档文择体在编制内故无标注(self) -> None:
        """Pro 含文择体，G3 正常取证，不该有降级标注。"""
        assert RoleId.DOC_ADVISOR in TIER_BY_ID[TierId.PRO].roster
        assert g3降级标注(TierId.PRO) == ()

    def test_Flash档必出标注(self) -> None:
        标注 = g3降级标注(TierId.FLASH)
        assert 标注, "Flash 档 G3 降级却没产标注——正是 D-116 记的那个缺口"
        assert all("文种由主理人研判" in 行 for 行 in 标注)

    def test_标注是显式分支不是提示词里的字(self) -> None:
        """**判据查的是「降级事实被算出来」，不是「某处有这行字」**。

        否则把字面量塞进提示词模板就能骗过判据——
        而那正是 D-116 明令禁止的实现方式
        （不能靠提示词记得）。
        """
        assert g3降级标注(TierId.FLASH) != g3降级标注(TierId.PRO)
        assert g3降级标注(TierId.PRO) == ()

    def test_编排用得上这个判定(self) -> None:
        """配平校验里那条 `continue` 放行了降级——**两处必须一致**。

        放行但不给标注，等于悄悄降级；
        给标注但不放行，门禁会卡死。两个方向都要查。
        """
        from zhengtong.domain.validation import validate_gate_balance

        assert validate_gate_balance(TierId.FLASH).is_ok, "Flash 档应放行"
        assert g3降级标注(TierId.FLASH), "放行了却没标注"


# ═══ D-196 真实校验器 ═══


class Test真实校验器:
    def test_满足端口协议(self) -> None:
        """结构化判据：跑起来得能被 `isinstance` 认成端口实现。

        否则 `PortRegistry` 装它时会当场炸，而接线判据在另一条里。
        """
        assert isinstance(硬约束校验器(), ValidatorPort)

    def test_D7硬约束零沉降(self) -> None:
        """D7 是「缺则不得动笔」的硬约束，**沉降必须报错**。"""
        校 = 硬约束校验器()
        got = 校.run_hard_constraint({"dcard": {"D7": None}})
        assert got.is_ok
        结论 = got.unwrap()
        assert not 结论.passed
        assert any("D7" in i.rule or "D7" in i.message for i in 结论.issues)

    def test_全部必填齐备则通过(self) -> None:
        """**必须八项全给**——第一版只给 D7 就断言通过，红了。

        实现是对的（D1–D7、D9 缺一不可，D-040），错的是测试：
        只填一个字段就期望过，是把「零沉降」当成了「查单个字段」。
        """
        校 = 硬约束校验器()
        齐 = {k: f"{k} 的值" for k in ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D9")}
        got = 校.run_hard_constraint({"dcard": 齐})
        assert got.unwrap().passed
        # 单独缺 D7 必红
        少 = {k: v for k, v in 齐.items() if k != "D7"}
        红 = 校.run_hard_constraint({"dcard": 少}).unwrap()
        assert not 红.passed
        assert [i.at for i in 红.issues] == ["dcard.D7"]

    def test_无锚点问题按阻断处理(self) -> None:
        """无法定位即无法核实（D-069）。"""
        校 = 硬约束校验器()
        got = 校.run_hard_constraint({"dcard": {"D7": None}})
        结论 = got.unwrap()
        assert all(i.has_anchor or i in 结论.blocking for i in 结论.issues)

    def test_结构校验查门禁证据(self) -> None:
        """G3 无证据不得通过（E1-E14 红线之一）。"""
        校 = 硬约束校验器()
        got = 校.run_structure({"gate": str(GateId.G3), "evidence": []})
        assert got.is_ok
        assert not got.unwrap().passed

    def test_结果类型不是门禁结论(self) -> None:
        """**刻意的类型隔离（D-069）**：脚本输出不得当作门禁放行依据。"""
        校 = 硬约束校验器()
        结论 = 校.run_structure({"gate": str(GateId.G3), "evidence": ["有"]}).unwrap()
        assert not hasattr(结论, "gate"), "校验结果被做成了门禁结论"

    def test_不放宽判据来凑绿(self) -> None:
        """留一条自查：上面每条都是**行为**断言，没有占位。

        第一版这里留了个假占位（`assert ws is None` + 注释说「见别的文件」）——
        那是永远绿的空壳，看着像有测试，实际什么都没验。
        """
        import inspect

        本类 = Test真实校验器
        for 名, 方法 in vars(本类).items():
            if 名.startswith("test_"):
                assert 方法.__doc__, f"{名} 没写判据依据"
                assert "assert" in inspect.getsource(方法), f"{名} 没有断言"
