"""`policy_retrieval` 必须被真正执行（D-255）。

## 缺口不是「读漏了一个字段」

查证链条（全部来自代码与内容包，非推测）：

1. 架构 §8.3 有独立阶段 **Phase 0.5 政策取证**——骆准绳，
   「Flash 简单检索 / Pro 强制逐条留出处」。
2. `orchestration/orchestrate.py` 的 `OUTLINE` 阶段
   **只派 `STRUCTURE`**（陈纲举）。骆准绳在 `OUTLINE` 拿不到派发。
3. 内容包 `content/prompts/compliance.md` 第 11 条明写
   「**政策主动检索（Phase 0.5 · 起草前）**」，要求产出
   《政策依据适配表》。
4. 全仓搜「政策依据适配表」——**`src/` 零命中**，只在内容包里。

**即：内容包要求骆准绳做政策检索，代码从不派它做，
产物名在代码里根本不存在。**

## 为什么不能接成「咨询档不调 retriever.search」

`policy_retrieval` 管的是**政策取证**这一件事，不是「所有检索」。
`kb.search` 是通用检索工具；咨询档虽不做政策取证，
**仍要读用户材料**。把字段接成「不调 search」会把
「不做政策取证」实现成「不许查任何资料」——那是错的，且更难发现。

## 修法：并入 `OUTLINE`，按档位分流

不拆独立 `PhaseId`。理由写在实现里：一格代码走完 Phase 0 与 0.5
是**有意设计**（`phase.py` 的注释：「假装是一对一，等于把
代码少一次交接藏起来」）；拆枚举会波及续跑、投影与事件序列。

## 测什么

**测行为**：该派发的档位派了不该派的角色吗？产物名对吗？
**不断**：提示词正文内容——那是内容包的地界（第 12 步已验收）。
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tests")
sys.path.insert(0, "src")


@pytest.fixture(autouse=True)
def _路径() -> None:
    pass


def _该阶段角色(档位, 阶段):
    """真名是 `_该段角色(档, 阶段)`，收 `TierId` 而非编制集合。

    （首版按 `_该阶段该动的人(阶段, 编制)` 写，那是猜的签名——
     靠查定义而非猜，才没让判据自己报假红。）
    """
    from zhengtong.orchestration.orchestrate import _该段角色

    return _该段角色(档位, 阶段)


class TestOUTLINE阶段的角色分流:
    def test_成文档位OUTLINE要派骆准绳做政策取证(self) -> None:
        """**本条是核心**：Flash/Pro 的 `policy_retrieval=True`，
        故 `OUTLINE` 阶段必须派骆准绳。

        架构 §8.3 的 Phase 0.5 是独立角色交接，陈纲举（因果推演）
        拿不走这一步——内容包要求它据《因果备忘录》做政策检索。
        """
        from zhengtong.domain.role import RoleId
        from zhengtong.domain.tier import TierId

        for 档 in (TierId.FLASH, TierId.PRO):
            from zhengtong.domain.phase import PhaseId

            角色 = _该阶段角色(档, PhaseId.OUTLINE)
            assert RoleId.COMPLIANCE in 角色, (
                f"{档.value} 档 OUTLINE 未派骆准绳——"
                "内容包要求他在 Phase 0.5 做政策取证，但代码从不派他"
            )

    def test_咨询档OUTLINE不派骆准绳(self) -> None:
        """**反向对照**：`policy_retrieval=False` → 不派。

        防的是「无脑全派」——那等于字段没被执行。
        """
        from zhengtong.domain.phase import PhaseId
        from zhengtong.domain.role import RoleId
        from zhengtong.domain.tier import TierId

        角色 = _该阶段角色(TierId.CONSULT, PhaseId.OUTLINE)
        assert RoleId.COMPLIANCE not in 角色, "咨询档不该做政策取证（架构 §8.3 明列三项豁免）"

    def test_咨询档OUTLINE派空是因为编制内无人而非被门禁豁免(self) -> None:
        """**本条防「一刀切」，但断言对象换了个。**

        ## 首版这条写错了

        我原本断言「咨询档 `OUTLINE` 仍派陈纲举」。实测它派空——
        因为**咨询档编制只有主理人**，陈纲举根本不在编制内。
        **不是实现错，是我把前提写错了**（与 D-254 同族）。

        故改查真正要防的东西：**成文档位的因果推演有没有被误删**。

        接错的人容易犯的错是「豁免政策取证 → 干脆 OUTLINE 什么都不派」，
        那会把**因果推演也一起干掉**，而它对成文档位是必做的。
        """
        from zhengtong.domain.phase import PhaseId
        from zhengtong.domain.role import RoleId
        from zhengtong.domain.tier import TierId

        for 档 in (TierId.FLASH, TierId.PRO):
            角色 = _该阶段角色(档, PhaseId.OUTLINE)
            assert 角色[:1] == (RoleId.STRUCTURE,), (
                f"{档.value} 档 OUTLINE 首位应是陈纲举（因果推演），"
                f"实际 {[r.value for r in 角色]}——豁免政策取证时把因果推演也砍了"
            )


class Test政策取证产物存在:
    def test_政策取证产物名在代码里存在(self) -> None:
        """**《政策依据适配表》在 `src/` 里必须有落点**。

        理由：内容包要求产出它，而全仓只有内容包提到它——
        **产物名不存在，等于模型产出了也没地方放**，
        下游（起草、G7 依据门禁）取不到，等于没做。
        """
        from pathlib import Path

        根 = Path(__file__).resolve().parent.parent
        命中 = [
            p
            for p in (根 / "src").rglob("*.py")
            if "政策依据适配表" in p.read_text(encoding="utf-8") and "__pycache__" not in str(p)
        ]
        assert 命中, "《政策依据适配表》只在内容包里出现，代码里无落点——产物无处可放"

    def test_阶段产物名一致(self) -> None:
        """产物名必须与内容包一致，否则模型产出了对不上名字。"""
        from pathlib import Path

        根 = Path(__file__).resolve().parent.parent
        包 = (根 / "content/prompts/compliance.md").read_text(encoding="utf-8")
        assert "政策依据适配表" in 包, "内容包里也没有这个名字，判据前提不成立"

        源 = [
            p
            for p in (根 / "src").rglob("*.py")
            if "政策依据适配表" in p.read_text(encoding="utf-8") and "__pycache__" not in str(p)
        ]
        assert 源, "代码侧无落点"


class Test政策取证产物真落盘:
    """**本组走真实存储适配器**，不只验「在产物列表里」。

    D-247 的教训：装配链中途丢弃的缺陷，**既有测试全绿**。
    只断言「产物在列表里」证明不了它进了文件系统——
    而 G7 门禁与起草阶段要按名**去磁盘上取**。
    """

    def test_产物真的写进文件系统(self, tmp_path) -> None:
        from test_step14_orchestrate_gate import _编排
        from zhengtong.adapters.fs_storage import FsStorage
        from zhengtong.adapters.paths import Workspace
        from zhengtong.domain.tier import TierId

        ws = Workspace(tmp_path)
        ws.exists_ok()
        # **每次运行一个独立 run 目录**（D-038：一任务一目录）
        (ws.dir("data") / "r-0001").mkdir(parents=True, exist_ok=True)
        编 = _编排(storage=FsStorage(ws=ws))
        结果 = 编.跑到终(档位=TierId.PRO, 需求="就旱情写个通知")
        assert not 结果.is_error, f"编排失败：{结果.error}"

        落 = list(tmp_path.rglob("政策依据适配表.md"))
        assert 落, (
            "《政策依据适配表》不在产物列表里也没落盘——G7 门禁与起草阶段按名去磁盘取时会找不到"
        )
        assert 落[0].read_text(encoding="utf-8").strip(), "落盘了但是空文件"

    def test_咨询档不该有这个文件(self, tmp_path) -> None:
        from test_step14_orchestrate_gate import _编排
        from zhengtong.adapters.fs_storage import FsStorage
        from zhengtong.adapters.paths import Workspace
        from zhengtong.domain.tier import TierId

        ws = Workspace(tmp_path)
        ws.exists_ok()
        # **每次运行一个独立 run 目录**（D-038：一任务一目录）
        (ws.dir("data") / "r-0001").mkdir(parents=True, exist_ok=True)
        编 = _编排(storage=FsStorage(ws=ws))
        结果 = 编.跑到终(档位=TierId.CONSULT, 需求="就旱情写个通知")
        assert not 结果.is_error, f"编排失败：{结果.error}"
        assert not list(tmp_path.rglob("政策依据适配表.md")), (
            "咨询档豁免政策取证，却产出了《政策依据适配表》"
        )


class Test咨询档检索未被误伤:
    def test_咨询档仍能读用户材料(self) -> None:
        """**防「接成不许查任何资料」**。

        上一版错误想法是把 `policy_retrieval=False` 实现成
        「不调 `retriever.search`」——那会让咨询档连用户材料都读不到。
        故本条明确：检索能力本身不受该字段影响。
        """
        from pathlib import Path

        根 = Path(__file__).resolve().parent.parent
        检索适配 = [
            p
            for p in (根 / "src").rglob("*.py")
            if "retriever.search" in p.read_text(encoding="utf-8") and "__pycache__" not in str(p)
        ]
        误伤 = []
        for p in 检索适配:
            文本 = p.read_text(encoding="utf-8")
            for 行 in 文本.splitlines():
                if "retriever.search" in 行 and (
                    "policy_retrieval" in 行 or "allows_rewrite" in 行
                ):
                    误伤.append(f"{p.name}: {行.strip()}")
        assert not 误伤, "检索被档位字段拦下了——「不做政策取证」不等于「不许检索」：\n" + "\n".join(
            误伤
        )


_ = pytest
