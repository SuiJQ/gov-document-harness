"""校验结论要能定位：命中哪条规则、在哪个位置（D-260、D-271）。

## 缺陷是什么

`ValidationIssue` 三个字段里，`rule`（命中哪条规则）与 `at`（在哪儿）
**解析器早就填好了**——`adapters/validators.py` 产出
`rule="D040-必填"` / `at="dcard.D1"`——但两个出口**各自只拼 `i.message`**：

| 出口 | 给谁看 | 此前给的 |
|---|---|---|
| `orchestrate.py` 提问前校验失败 | **用户** | 「D1 为空，缺则不得动笔」 |
| `handlers.py` `validate.run` | **模型** | 「D1 为空，缺则不得动笔」 |

于是两边都知道「有问题」，但**既不知道命中哪条规则，也不知道去哪儿改**。
模型那一侧更糟：它正是要去补齐字段的那个角色，却拿不到字段路径。

## 为什么格式化收敛成一个属性

两个出口各写一份拼法，**改一处忘一处就会漂移**——
那正是本缺陷本身的成因。故 `ValidationIssue.定位` 是唯一定义处。

## 与 D-034「无证据不通过」的关系

`ValidationResult.blocking` 把**无锚点**的问题按阻断处理
（无法定位即无法核实）。本条不改变那条语义，
只让「有锚点」这件事**真的被说出来**。
"""

from __future__ import annotations

import pytest

from zhengtong.adapters.validators import 硬约束校验器
from zhengtong.domain.role import RoleId
from zhengtong.ports.validators import ValidationIssue


class Test定位属性:
    def test_规则与锚点都在(self) -> None:
        i = ValidationIssue(rule="D040-必填", message="D1 为空", at="dcard.D1")
        文 = i.定位
        assert "D040-必填" in 文, f"丢了规则号：{文}"
        assert "dcard.D1" in 文, f"丢了锚点：{文}"
        assert "D1 为空" in 文, f"丢了正文：{文}"

    def test_无锚点时不显示空标记(self) -> None:
        """**不能显示成 `[规则] → 问题` 或 `[规则] [] → 问题`**。

        空标记会让读者以为「定位过了，只是没查出来」——
        与 D-053「只给内容不给标记」是同一类陷阱。
        """
        文 = ValidationIssue(rule="D034-无证据", message="无取证证据").定位
        assert "[]" not in 文, f"显示了空锚点标记：{文}"
        assert "→" not in 文, f"无锚点却显示了箭头：{文}"
        assert "D034-无证据" in 文

    def test_缺规则时也不留空括号(self) -> None:
        文 = ValidationIssue(rule="", message="有问题", at="x.y").定位
        assert "[]" not in 文, f"显示了空规则括号：{文}"
        assert "x.y" in 文

    def test_两段之间不粘连(self) -> None:
        """`[规则]` 与锚点之间要有分隔——粘连成 `[D040]dcard.D1` 读不出来。"""
        文 = ValidationIssue(rule="D040-必填", message="为空", at="dcard.D1").定位
        assert "]dcard" not in 文, f"规则号与锚点粘连：{文}"

    def test_不篡改原文(self) -> None:
        """`定位` 是给原文**加前缀**，不是改写它。"""
        原 = "D1 为空，缺则不得动笔"
        assert 原 in ValidationIssue(rule="R", message=原, at="a").定位


class Test真校验器产出的字段真的进得去:
    def test_空字段带规则与锚点(self) -> None:
        """**判据断言的是真实校验器的产出**，不是手搓的 `ValidationIssue`。

        手搓一个对象只能证明属性能拼字符串，证明不了
        「解析器真的填了 `at`」。这是 D-233「两层各自绿、没人对账」的同类。
        """
        校 = 硬约束校验器()
        结 = 校.run_hard_constraint({"phase": "DIRECTION", "dcard": {"D1": ""}}).unwrap()
        assert not 结.passed
        问题 = 结.issues[0]
        assert 问题.rule, "真实校验器没填规则号"
        assert 问题.at, "真实校验器没填锚点"
        assert 问题.at in 问题.定位

    def test_锚点确实指向那个字段(self) -> None:
        校 = 硬约束校验器()
        结 = 校.run_hard_constraint(
            {"phase": "DIRECTION", "dcard": {"D1": "有值", "D3": ""}}
        ).unwrap()
        锚 = [i.at for i in 结.issues]
        assert 锚, "没有问题"
        assert all(a and a.startswith("dcard.") for a in 锚), f"锚点不像字段路径：{锚}"
        assert any("D3" in a for a in 锚), f"锚点没指向出问题的字段：{锚}"


class Test两个出口都不许丢字段:
    """**这组是本条的核心**——只测属性等于没测接线。

    与 D-233 / D-237 同型：零件测过，衔接没测。
    """

    def test_用户侧出口带规则与锚点(self) -> None:
        from zhengtong.adapters.fakes import FakeLlm, FakeStorage
        from zhengtong.domain.dcard import FIELD_LABELS, DCard, DCardField, DCardFieldId, Provenance
        from zhengtong.domain.ids import RunId
        from zhengtong.domain.tier import TierId
        from zhengtong.orchestration import 编排器

        # **预填部分字段**：D1、D2 有值、D3-D9 空，让硬约束真正跑起来并失败。
        #
        # 此前需求「就旱情写个通知」会被 `_从用户输入更新d卡()` 提取
        # D1=通知，恰好也是「仅 D1 填、D2-D9 空」→ 降级放行，走不到 E040。
        # 而 D-Card 全空时同样降级放行（ask_user 未接通，架构约束）。
        # 故直接预填一张「D1、D2 有值、D3-D9 空」的卡——
        # 已填字段数 > 1，不触发降级分支，硬约束真的拦下来。
        _部分卡 = DCard(
            {
                DCardFieldId.D1: DCardField(
                    DCardFieldId.D1,
                    FIELD_LABELS[DCardFieldId.D1],
                    value="通知：关于旱情",
                    provenance=Provenance.USER,
                ),
                DCardFieldId.D2: DCardField(
                    DCardFieldId.D2,
                    FIELD_LABELS[DCardFieldId.D2],
                    value="无",
                    provenance=Provenance.USER,
                ),
                **{
                    fid: DCardField(fid, label)
                    for fid, label in FIELD_LABELS.items()
                    if fid not in (DCardFieldId.D1, DCardFieldId.D2)
                },
            }
        )
        编 = 编排器(
            llm=FakeLlm(),
            storage=FakeStorage(),
            模型名="m",
            门禁取证=lambda _请求: {},
            运行号=RunId("r-val"),
            validators=硬约束校验器(),
        )
        结果 = 编.跑到终(档位=TierId.PRO, 需求="随便写点东西", 初始dcard=_部分卡)
        assert 结果.is_error, "D-Card 部分填却跑通了——提问前校验没拦"
        详情 = _详情(结果)
        assert "D040" in 详情, f"用户看不到命中哪条规则：{详情}"
        assert "dcard." in 详情, f"用户看不到去哪儿改：{详情}"

    def test_模型侧出口带规则与锚点(self) -> None:
        """给模型的那条也必须带——**它才是要去补字段的那个角色**。"""
        from zhengtong.adapters.fakes import (
            FakeContentPack,
            FakeRetriever,
            FakeStorage,
            FakeUserFiles,
        )
        from zhengtong.domain.ids import RunId
        from zhengtong.services.tools import handlers

        校 = 硬约束校验器()
        依 = handlers.工具依赖(
            storage=FakeStorage(),
            user_files=FakeUserFiles(),
            retriever=FakeRetriever(),
            content_pack=FakeContentPack(),
            validators=校,
            run_id=RunId("r-1"),
        )
        出 = handlers.validate_run(
            依,
            RoleId.TEAM_LEAD,
            {"checker": "structure", "args": {"gate": "G10", "evidence": []}},
        )
        文 = 出.unwrap()
        assert "D034" in 文, f"模型看不到命中哪条规则：{文}"
        assert "G10" in 文, f"模型看不到是哪个门禁：{文}"


# ── 小工具 ──


def _详情(结果: object) -> str:
    错 = getattr(结果, "error", None)
    assert 错 is not None, "没有错误对象"
    return str(getattr(错, "detail", ""))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
