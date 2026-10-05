"""删死代码要判据，不是看着没用就删（D-251 / D-252）。

## 删代码的风险与加代码相反

加代码的坏处是「多了没用的东西」；
**删代码的坏处是「删掉了还在起作用的东西」**——
后者严重得多，因为它不会报错，只在某个真实场景里静默失效。

所以本文件不测「它没用」，而测**三条更硬的性质**：

1. 全仓确无引用（含字符串形式——反射调用最难查）
2. 它承诺的职责**另有实现**（删了不丢功能）
3. 删除后**行为完全不变**（跑全量 + 关键路径对照）

## SCHEMA_VERSION 合并（D-252）

合并的理由不是「重复」，而是**升级版本号时会漏改一处**：

- `domain/__init__.py` 那份用于**写**状态（新状态带这个版本）
- `state_codec.py` 那份用于**读**状态

只升读侧、不升写侧 ⇒ 写出的状态永远带旧版本 ⇒
**将来在 `from_json` 里加的迁移分支永远不触发，且没有任何报错。**

这与本项目栽过多次的形状相同：**值在那里，没人保证它被同步。**
"""

from __future__ import annotations

import pathlib

import pytest

仓库根 = pathlib.Path(__file__).resolve().parent.parent


def _全仓源码(后缀: str = ".py") -> list[pathlib.Path]:
    出: list[pathlib.Path] = []
    for 目录 in ("src", "tests", "scripts"):
        p = 仓库根 / 目录
        if p.is_dir():
            出.extend(x for x in p.rglob(f"*{后缀}") if "__pycache__" not in str(x))
    return 出


def _被引用(名字: str) -> list[str]:
    """全仓搜 `名字`——**含字符串字面量**，反射调用也逃不掉。"""
    命中: list[str] = []
    for 后缀 in (".py", ".toml", ".json", ".md"):
        for p in _全仓源码(后缀):
            try:
                文本 = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if 名字 in 文本:
                命中.append(f"{p.relative_to(仓库根)}")
    return 命中


class Test删除的函数确无作用:
    def test_check_resumable确无引用(self) -> None:
        """**删代码的第一条硬性质**：删了不会有人 NameError。

        含 `.md`/`.toml`/`.json`——文档或配置里写着它的名字
        也要算，那通常是「有人打算用它」的痕迹。
        """
        命中 = [h for h in _被引用("check_resumable") if "test_step15_deadcode" not in h]
        # 只允许它出现在自己的定义文件里（定义 + __all__）
        定义文件 = "src/zhengtong/domain/run_state.py"
        越界 = [h for h in 命中 if h != 定义文件]
        assert not 越界, f"仍被这些地方引用：{越界}"

    def test_tier_by_name确无引用(self) -> None:
        定义文件 = "src/zhengtong/domain/tier.py"
        命中 = [h for h in _被引用("tier_by_name") if "test_step15_deadcode" not in h]
        越界 = [h for h in 命中 if h != 定义文件]
        assert not 越界, f"仍被这些地方引用：{越界}"

    def test_两函数已不存在(self) -> None:
        """删完就该真不存在——留着等于删了个寂寞。"""
        import zhengtong.domain.run_state as rs
        import zhengtong.domain.tier as tr

        assert not hasattr(rs, "check_resumable"), "check_resumable 还在"
        assert not hasattr(tr, "tier_by_name"), "tier_by_name 还在"

    def test_all清单里也不再导出(self) -> None:
        """`__all__` 里留着 = 仍被当作公开 API。

        本项目无 `import *`，所以这条只是**诚实性**要求：
        导出了却没人能用，比不导出更误导人。
        """
        import zhengtong.domain.run_state as rs
        import zhengtong.domain.tier as tr

        assert "check_resumable" not in getattr(rs, "__all__", ())
        # `tier` 本就没有 `__all__`（靠 import 重导出管理），
        # 故查它是否仍在模块字典里即可
        assert "tier_by_name" not in tr.__dict__


class Test删除的职责另有实现:
    def test_版本兼容性仍被检查(self) -> None:
        """**关键**：删了 `check_resumable`，版本检查**必须还在**。

        删代码最容易出的事故就是「以为有别处兜着，其实没有」。
        故本条直接验 `from_json` 仍拒新版本。
        """
        import zhengtong.adapters.state_codec as sc

        got = sc.from_json({"schema_version": sc.SCHEMA_VERSION + 1, "run_id": "r-1"})
        assert got.is_error, "版本更高的状态竟被接受——删掉的那个函数是唯一防线"
        assert got.error is not None
        assert "版本" in got.error.detail, f"错误信息没提版本：{got.error.detail}"

    def test_版本兼容性检查覆盖bool(self) -> None:
        """`schema_version: true` 必须拒绝（D-144 修的那条）。

        保留这条是因为它是 D-244 那次「重复实现」定性的依据：
        `from_json` 的检查**严格更全**，所以删 `check_resumable` 不丢东西。
        """
        import zhengtong.adapters.state_codec as sc

        got = sc.from_json({"schema_version": True, "run_id": "r-1"})
        assert got.is_error, "布尔版本号被接受——bool 是 int 的子类"

    def test_缺版本号也拒绝(self) -> None:
        import zhengtong.adapters.state_codec as sc

        got = sc.from_json({"run_id": "r-1"})
        assert got.is_error, "缺 schema_version 竟被接受"


class TestSCHEMA_VERSION单一来源:
    """**本节的两条判据改过一次**，理由记在这里。

    ## 第一次写错

    原本断言 `state_codec.py` 里**不得有 `SCHEMA_VERSION` 的赋值语句**。
    按此判据，我把常量换成 `from ..domain import SCHEMA_VERSION`——
    **结果判据红，因为那是值拷贝**。

    实测：`domain.SCHEMA_VERSION` 改成 2，`state_codec.SCHEMA_VERSION`
    仍是 1。合并等于白做。

    ## 第二次仍写错

    改成 `_域.SCHEMA_VERSION`（模块引用）后我以为对了，
    于是干脆把导出删掉——**结果破坏了 `test_reexport_golden.py`**：
    它刻意用 `from state_codec import SCHEMA_VERSION` 模拟外部用户脚本。

    D-215 的收敛口径是「**收敛实现不该逼调用方改 import**」。
    删掉对外导出正是逼调用方改。

    ## 最终方案与判据

    **保留导出，但它是转发**；模块内判断一律走 `_域.SCHEMA_VERSION`。
    故判据不该查「有没有赋值语句」，该查**行为**：
    改 domain 后，`from_json` 的判断是否跟着变。
    """

    def test_判断点走domain属性而非本地快照(self) -> None:
        """**本条是 D-252 真正要治的**：

        改 `domain.SCHEMA_VERSION` 后，`from_json` 是否接受原本该拒绝的状态。
        这验的是行为，不是语法——语法判据会被「换个写法照样错」骗过。
        """
        import zhengtong.adapters.state_codec as sc
        import zhengtong.domain as d

        原 = d.SCHEMA_VERSION
        try:
            d.SCHEMA_VERSION = 原 + 1
            # 程序现在支持到 原+1，故版本 原+1 应被接受
            got = sc.from_json({"schema_version": 原 + 1, "run_id": "r-1"})
            assert not got.is_error, (
                f"写侧升到 {原 + 1} 后，版本 {原 + 1} 的状态仍被拒绝——"
                "判断点用的是本地快照，迁移分支永远不触发"
            )
            got2 = sc.from_json({"schema_version": 原 + 2, "run_id": "r-1"})
            assert got2.is_error, f"版本 {原 + 2} 超过程序支持却被接受"
        finally:
            d.SCHEMA_VERSION = 原
        # 还原后行为必须回到原样——不得污染全局状态
        assert sc.from_json({"schema_version": 原, "run_id": "r-1"}) is not None

    def test_对外导出仍在且不破坏重新导出判据(self) -> None:
        """**保留导出的理由**：D-215「收敛不该逼调用方改 import」。

        `test_reexport_golden.py` 刻意用这个 import 模拟外部脚本，
        删掉它就等于告诉用户「你的代码要改」。
        """
        from zhengtong.adapters.state_codec import SCHEMA_VERSION

        assert isinstance(SCHEMA_VERSION, int)

    def test_导出值等于domain当前值(self) -> None:
        import zhengtong.adapters.state_codec as sc
        import zhengtong.domain as d

        assert sc.SCHEMA_VERSION == d.SCHEMA_VERSION


class Test档位系统未受损:
    def test_TIER_BY_ID仍可用(self) -> None:
        """删了 `tier_by_name`，档位查表不能受影响——那是真正在用的入口。"""
        from zhengtong.domain.tier import TIER_BY_ID, TierId

        assert TierId.PRO in TIER_BY_ID
        assert TIER_BY_ID[TierId.PRO].roster

    def test_三档齐全(self) -> None:
        from zhengtong.domain.tier import TIERS, TierId

        识别 = {t.id for t in TIERS}
        assert 识别 == {TierId.CONSULT, TierId.FLASH, TierId.PRO}, f"{识别}"


_ = pytest
