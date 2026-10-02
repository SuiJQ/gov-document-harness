"""第 11 步判据：提示词装配（架构 §7.1）。

## 装配要拼的五块

```
角色提示词 + 按需装载的规则条目 + D-Card 事实块
+ 产物契约（引用，不是全文）+ 工具清单（本次实际可调子集）
```

## 怎么写才算数

**每条判据都要能杀死对应的偷懒实现**，而不是断言"输出非空"：

| 偷懒实现 | 哪条判据抓它 |
|---|---|
| 全量装载所有 references | `test_条件不满足的规则不进 prompt` |
| 产物全文进 prompt | `test_产物只传引用不带全文` |
| 工具清单给全集 | `test_工具清单只列本次可调的` |
| 不管预算，装到最后再说 | `test_超预算的块被截且标明` |
| 装完不报占用 | `test_占用可测` |
"""

from __future__ import annotations

import pytest

from zhengtong.adapters.fakes import FakeContentPack
from zhengtong.domain.dcard import DCard, DCardField, DCardFieldId, Provenance
from zhengtong.domain.role import RoleId
from zhengtong.ports.llm import Message
from zhengtong.services.prompting import (
    装载条件,
    装配提示词,
    装配结果,
    装配请求,
    预算闸门,
    默认装载条件,
)
from zhengtong.services.tools.registry import TOOL_SPECS, 取工具
from zhengtong.services.tools.spec import Effect, ToolSpec


def _卡(**填: str) -> DCard:
    """一张最小 D-Card。默认 D7 填好（D7 缺则不得动笔）。"""
    默认 = {"D7": "不新建章"}
    return DCard(
        by_id={
            DCardFieldId(键): DCardField(
                id=DCardFieldId(键), label=键, value=值, provenance=Provenance.USER
            )
            for 键, 值 in {**默认, **填}.items()
        }
    )


# ═══ 纪律 1：规则按需装载，不常驻 ═══


class Test按需装载:
    def test_条件不满足的规则不进prompt(self) -> None:
        """D-025 分层装载：主体排序规则**仅在主体 ≥3 时**才装。

        2 个主体时装了它 = 白花上下文预算，
        而预算正是第 11 步要守的东西（D-189）。
        """
        装配 = 装配提示词(
            请求=装配请求(
                角色=RoleId.TEAM_LEAD,
                卡=_卡(),
                主体数=2,
            ),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert "admin-ordering-rules" not in 装配.提示词正文, "2 个主体时装了主体排序规则"

    def test_条件满足的规则进prompt(self) -> None:
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=_卡(), 主体数=3),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert "admin-ordering-rules" in 装配.提示词正文, "3 个主体时主体排序规则没装"

    def test_装载的是真实条目不是占位(self) -> None:
        """装出来的必须带**条目出处**，否则模型无法分辨哪段是规则。

        且出处必须来自内容包——代码硬编码条目清单就违反 D-055。
        """
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=_卡(), 主体数=3),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert 装配.已装条目, "没报告装了哪些条目——不可审计"
        assert "admin-ordering-rules" in 装配.已装条目

    def test_角色限定条目只给该角色(self) -> None:
        """`限角色` 是按需的第二个维度。

        只测主体数这一个维度是不够的：把 `_该装` 的角色判断删掉，
        主体数那条判据照样绿——成员会拿到合规专用的版式规范，
        白占预算，且可能照着不适用的规范改稿。
        """
        语言 = 装配提示词(
            请求=装配请求(角色=RoleId.LANGUAGE, 卡=_卡()),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert "gbt9704-format" not in 语言.已装条目, "语言角色拿到了合规专用规范"
        合规 = 装配提示词(
            请求=装配请求(角色=RoleId.COMPLIANCE, 卡=_卡()),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert "gbt9704-format" in 合规.已装条目, "合规角色没拿到版式规范"

    def test_已装条目恰为该装的那些(self) -> None:
        """**不多不少**：多装是白花预算，少装是丢规则。

        分别断言「无冗余」与「无遗漏」，而不是只看数量——
        数量对了但装错条目（多一条少的补上）是最坏的情况，
        单看 `len()` 判不出来。
        """
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.COMPLIANCE, 卡=_卡(), 主体数=3),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert set(装配.已装条目) == {
            "error-memory",
            "admin-ordering-rules",
            "gbt9704-format",
            "punctuation",
        }
        # 条件表里没有的条目一条都不该出现
        表 = {t.ref for t in 默认装载条件()}
        assert set(装配.已装条目) <= 表, "装了条件表之外的东西"

    def test_条件表可替换(self) -> None:
        """条件表是**唯一策略入口**——换内容包即换写作标准（D-055）。

        代码硬编码条件就做不到这件事。
        """
        自定义 = (装载条件(ref="language-dna", 描述="语言 DNA", 最少主体数=99),)
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.LANGUAGE, 卡=_卡(), 主体数=1),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
            条件表=自定义,
        )
        assert 装配.已装条目 == (), "自定义条件表没生效，仍按默认表装"
        assert "error-memory" not in 装配.提示词正文, "默认表条目混了进来"

    def test_每块都能单独关掉(self) -> None:
        """按需的另一面：不需要的块能整个不装。

        否则「按需」只是换了个说法——条件里恒为真就等于常驻。

        **只查工具块与事实块**：规则条目里有 `error-memory` 是**无条件装载**的
        （红线 E1-E14 无条件执行），传参关不掉它——
        第一版把 `工具=()` 当成「什么都不装」，实际只关掉了工具块。
        """
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.LANGUAGE, 卡=None, 主体数=0, 工具=()),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert 装配.提示词正文.strip(), "角色提示词本身总该有"
        assert "工具清单" not in 装配.提示词正文, "传空工具却还列了工具"
        assert "D-Card 事实块" not in 装配.提示词正文, "无 D-Card 却有事实块"
        assert 装配.事实块占用 == 0


# ═══ 纪律 2：产物只传引用不传全文 ═══


class Test产物只传引用:
    def test_产物只传引用不带全文(self) -> None:
        装配 = 装配提示词(
            请求=装配请求(
                角色=RoleId.STRUCTURE,
                卡=_卡(),
                产物=("大纲初稿.md",),
                产物全文={"大纲初稿.md": "超长正文" * 500},
            ),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert "超长正文" not in 装配.提示词正文, "产物全文漏进 prompt 了"
        assert "大纲初稿.md" in 装配.提示词正文, "连引用都没给"

    def test_引用里带读取方式(self) -> None:
        """光给个文件名，模型不知道该怎么取内容。"""
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.STRUCTURE, 卡=_卡(), 产物=("大纲.md",)),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert "shared.read" in 装配.提示词正文, "没告诉模型用什么工具读"


# ═══ 纪律 3：工具清单是本次可调子集 ═══


class Test工具清单:
    def test_只列本次可调的(self) -> None:
        装配 = 装配提示词(
            请求=装配请求(
                角色=RoleId.LANGUAGE,
                卡=_卡(),
                工具=("shared.read", "draft.submit"),
            ),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert "shared.read" in 装配.提示词正文
        assert "draft.submit" in 装配.提示词正文
        assert "flow.rollback" not in 装配.提示词正文, "列了本角色调不到的工具"

    def test_未知工具名报出来不崩(self) -> None:
        """工具名可能来自配置。**报错而非静默跳过**——
        静默跳过会让模型发现工具凭空消失，而用户查不出原因。"""
        with pytest.raises(ValueError, match="不存在的工具"):
            装配提示词(
                请求=装配请求(角色=RoleId.LANGUAGE, 卡=_卡(), 工具=("fs.delete",)),
                内容包=FakeContentPack(),
                闸门=预算闸门(上限=100_000),
            )

    def test_默认取该角色实际可调子集(self) -> None:
        """不显式给工具时，按注册表的角色白名单算——**不是全集**。

        这是双重白名单（D-042）在提示词侧的落地：
        模型看不到调不到的工具，就不会有无效尝试。
        """
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.LANGUAGE, 卡=_卡()),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        提示词 = 装配.提示词正文
        assert "draft.submit" in 提示词
        assert "flow.rollback" not in 提示词, "成员不该看到仅主理人可调的工具"


# ═══ D-Card 事实块 ═══


class Test事实块:
    def test_已填字段进块(self) -> None:
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=_卡(D7="不得新建章", D8="庄重")),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert "不得新建章" in 装配.提示词正文
        assert "庄重" in 装配.提示词正文

    def test_未填字段标待填而非省略(self) -> None:
        """**省略等于隐瞒**：模型看不到 D2 缺了，就可能自己编一个配套件。

        D-034「严禁编造」在这里落地——缺就是缺，写出来。
        """
        卡 = DCard(by_id={DCardFieldId.D7: DCardField(id=DCardFieldId.D7, label="D7", value="x")})
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=卡),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert "待填" in 装配.提示词正文, "缺字段没标出来"

    def test_主理人研判字段标出处(self) -> None:
        """D8 允许 LEAD_INFERRED，**必须标明是研判而非用户提供**。"""
        卡 = DCard(
            by_id={
                DCardFieldId.D7: DCardField(id=DCardFieldId.D7, label="D7", value="x"),
                DCardFieldId.D8: DCardField(
                    id=DCardFieldId.D8,
                    label="D8",
                    value="庄重",
                    provenance=Provenance.LEAD_INFERRED,
                ),
            }
        )
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=卡),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        assert "研判" in 装配.提示词正文, "没标出这条是研判来的"


# ═══ D-189：装载时就有预算 ═══


class Test预算闸门:
    def test_占用可测(self) -> None:
        """ "上下文占用可测"就是第 11 步的判据本身。"""
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=_卡()),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )
        from zhengtong.services.context.estimate import 估算文本

        assert 装配.占用 > 0
        # 占用 = 装配后正文的估算 token（不是分块之和——拼接有额外开销）
        assert 装配.占用 == 估算文本(装配.提示词正文)
        assert 装配.占用 >= sum(装配.分块占用().values())

    def test_超预算的块被截且标明(self) -> None:
        """预算不够时**截并标明**，不是静默丢弃、也不是硬塞。

        静默丢弃：模型不知道内容缺了。
        硬塞：预算形同虚设（第 9 步的教训）。
        """
        闸门 = 预算闸门(上限=200)
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=_卡()),
            内容包=FakeContentPack(),
            闸门=闸门,
        )
        assert 装配.占用 <= 200, f"超预算了：{装配.占用}"
        assert "已截断" in 装配.提示词正文, "截了却没标明"

    def test_预算够时原样装载(self) -> None:
        闸门 = 预算闸门(上限=100_000)
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=_卡()),
            内容包=FakeContentPack(),
            闸门=闸门,
        )
        assert "已截断" not in 装配.提示词正文
        assert not 装配.被截块

    def test_被截时报告哪些块(self) -> None:
        """占用可测的另一半：**要能说出是谁超了**。"""
        装配 = 装配提示词(
            请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=_卡()),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=200),
        )
        assert 装配.被截块, "没报告哪些块被截"


# ═══ 不变量 ═══


def test_提示词确定性() -> None:
    """同样输入必得同样输出（D-008 可 diff 的前提）。"""

    def 装一次() -> 装配结果:
        return 装配提示词(
            请求=装配请求(角色=RoleId.STRUCTURE, 卡=_卡(), 主体数=3, 产物=("a.md",)),
            内容包=FakeContentPack(),
            闸门=预算闸门(上限=100_000),
        )

    assert 装一次().提示词正文 == 装一次().提示词正文


def test_装配结果是消息序列() -> None:
    """装配产物必须能直接当 messages 发出去。"""
    装配 = 装配提示词(
        请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=_卡()),
        内容包=FakeContentPack(),
        闸门=预算闸门(上限=100_000),
    )
    assert 装配.消息, "没产出消息"
    assert all(isinstance(m, Message) for m in 装配.消息)


def test_工具规格来自注册表() -> None:
    """描述取自 `TOOL_SPECS` 单一权威，不另建一份清单。"""
    装配 = 装配提示词(
        请求=装配请求(角色=RoleId.LANGUAGE, 卡=_卡()),
        内容包=FakeContentPack(),
        闸门=预算闸门(上限=100_000),
    )
    名字 = {t.name for t in TOOL_SPECS}
    assert "draft.submit" in 名字
    规 = 取工具("draft.submit")
    assert 规 is not None
    assert isinstance(规, ToolSpec)
    assert 规.effect is Effect.PUBLISH
    assert 规.description in 装配.提示词正文, "工具描述没取自注册表"


def test_角色提示词正文进最终prompt() -> None:
    """角色提示词是五块里最大的一块，**装配了别的把它丢了就是本末倒置**。"""
    装配 = 装配提示词(
        请求=装配请求(角色=RoleId.TEAM_LEAD, 卡=_卡()),
        内容包=FakeContentPack(),
        闸门=预算闸门(上限=100_000),
    )
    assert "team-lead 的角色提示词" in 装配.提示词正文
