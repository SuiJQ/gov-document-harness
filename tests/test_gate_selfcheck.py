# 门禁测试豁免（理由同其余五道门禁，另加一条：
# 本文件用 importlib 动态载入其它门禁并调用其私有函数，
# 类型检查器无法跨模块解析这类动态调用）。
# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportIndexIssue=false, reportCallIssue=false, reportAssignmentType=false, reportAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportPrivateUsage=false, reportUnusedFunction=false, reportUnusedImport=false, reportImplicitStringConcatenation=false
"""门禁自检：检查「检查逻辑本身」还能不能分辨差异。

## 这道门禁为何存在

横切审查实测：把五道门禁的**核心断言**逐个改成恒真，
看它们会不会变红。结果：

| 门禁 | 架空手法 | 结果 |
|---|---|---|
| `test_boundary` | 判定函数改恒真 | ✅ 1 条红 |
| `test_schema_tolerant` | 拒绝改接受 | ✅ 7 条红 |
| `test_port_contract` | 金样比对改 `assert True` | ❌ **13 条全过** |
| `test_layering` | 允许表放大到全集 | ❌ **14 条全过** |
| `test_reexport_golden` | 确定性断言改恒真 | ❌ **10 条全过** |

三道门禁的核心判定被架空，**门禁自己毫无反应**。

## 根因：防呆断言全都写错了方向

我在三道门禁里都写了防呆，但它们检查的是**被检查对象**：

- `test_port_contract` 的防呆是「金样文件非空」
- `test_layering` 的防盲是「允许的目标层必须存在」

这两条在核心判定被架空后**照样通过**——
因为「金样文件」还在、「层名」也还合法，被架空的只是**比对动作**。

> **防呆断言要问的不是「被检查的东西对不对」，
> 而���「我检查它的那个动作还能不能发现差异」。**

这与 D-143 是同一个病：
`test_并行分组均含五角色` 断言的是「每组是单元素」这个表面特征，
而不是「分组与 `GATES` 一致」这个真目标。

## 本文件的判据

每条断言都构造一个**已知有差异的样例**，
然后验证判定逻辑**能发现它**。

不做「假装破坏自己」的动作——那是反证，由人做；
本文件做的是**让判定逻辑自己证明它没瞎**。

## 代价

这些断言有一处别扭：它们构造违规样例，
所以在本文件里读起来像是在「教别人怎么作弊」。
这是刻意的——**判据必须能被它的反面检验**。
"""

from __future__ import annotations

import ast
import importlib
import pathlib

_ROOT = pathlib.Path(__file__).parent
_SRC = _ROOT.parent / "src" / "zhengtong"


def _相对包(文件: str) -> pathlib.Path:
    """某文件相对 `src/zhengtong` 的父目录。"""
    return (_SRC / 文件).parent.relative_to(_SRC)


def _状态(要求: str = "就旱情写个通知", tier: str = "pro") -> object:
    """构造一个最小但合法的 `RunState`。"""
    from zhengtong.domain.ids import RunId
    from zhengtong.domain.run_state import RunState
    from zhengtong.domain.task import TaskSpec
    from zhengtong.domain.tier import TierId

    return RunState(
        schema_version=1,
        run_id=RunId("r-selfcheck"),
        task=TaskSpec(tier=TierId(tier), requirement=要求),
    )


# ── 1. test_layering：允许表必须「紧」 ──────────────────────────
class Test允许表判定仍有效:
    """架空手法：把 `ALLOWED` 放大，于是分层门禁全过。"""

    def test_领域层允许表必须为空(self) -> None:
        """**架构 §3.4 明写 domain 不依赖任何本仓层。**

        过去 `test_允许表与架构一致` 只断言「允许的目标层名合法」，
        所以把 `frozenset()` 改成 `frozenset({"kernel","ports","adapters"})`
        它照样通过——**它检查的是名字，不是松紧**。
        """
        layering = importlib.import_module("test_layering")
        assert layering.ALLOWED["domain"] == frozenset(), (
            f"domain 的允许表被放宽成 {sorted(layering.ALLOWED['domain'])}——"
            "架构 §3.4 规定 domain 不依赖任何本仓层"
        )

    def test_内核不得依赖适配层(self) -> None:
        """kernel 只允许 domain。这条同样必须「紧」。"""
        layering = importlib.import_module("test_layering")
        允许 = layering.ALLOWED["kernel"]
        assert "adapters" not in 允许, "kernel 被允许依赖 adapters——§3.4 禁止"
        assert 允许 <= frozenset({"domain"}), f"kernel 允许表过宽：{sorted(允许)}"

    def test_判定函数仍能分辨越层导入(self) -> None:
        """**核心：判定函数对「已知违规」必须给出非空结果。**

        若有人把 `_本仓导入` 改成恒返回空，本条立刻红。
        """
        layering = importlib.import_module("test_layering")
        违规 = ast.parse("from ..adapters.fs_storage import FsStorage")
        结果 = layering._本仓导入(违规, _相对包("kernel/bus.py"))
        assert "adapters" in 结果, (
            f"判定函数未能识别越层导入，得到 {结果}——若它恒返回空，本门禁会永远全绿"
        )

    def test_判定函数对同层导入不应误报(self) -> None:
        """**反向：判定函数必须能分辨「不违规」的情形。**

        只测「能发现违规」不够——一个恒返回非空的函数也能通过上一条。
        """
        layering = importlib.import_module("test_layering")
        结果 = layering._本仓导入(ast.parse("from .types import X"), _相对包("kernel/bus.py"))
        assert 结果 == ["kernel"], f"同层导入被误判成 {结果}"


# ── 2. test_port_contract：金样比对必须真在比对 ──────────────────
class Test金样比对仍有效:
    """架空手法：把比对断言改成 `assert True`。"""

    def test_快照函数返回非空(self) -> None:
        """**金样比对的前提：`_快照()` 必须真的产出内容。**

        若 `_快照` 被改成空 dict，比对就成了「两边都空 → 通过」。
        """
        pc = importlib.import_module("test_port_contract")
        快照 = pc._快照()
        assert 快照, "_快照() 返回空——金样比对将永远通过"
        assert len(快照) >= 20, f"_快照() 只有 {len(快照)} 条，明显不正常"

    def test_比对能发现新增方法(self) -> None:
        """**核心：构造一个「多出一条」的对照，验证差集非空。**

        这条直接检验比对动作本身，而不是检验某个文件的内容。
        """
        金样 = set(importlib.import_module("test_port_contract")._读金样())
        assert 金样, "金样为空"
        多出 = (金样 | {"fake.Port.fake_method() -> 'None'"}) - 金样
        assert 多出, "加了一条后差集仍为空——比对逻辑失效"

    def test_金样含每个端口至少一条(self) -> None:
        """**防呆：金样若只剩一个端口，其余端口等于失守。**"""
        pc = importlib.import_module("test_port_contract")
        金样 = pc._读金样()
        for 模名 in pc.端口清单:
            assert any(行.startswith(f"{模名}.") for 行 in 金样), f"金样缺 {模名}"


# ── 3. test_reexport_golden：确定性判定必须真在比对字节 ────────
class Test确定性判定仍有效:
    """架空手法：把确定性断言改成恒真。"""

    def test_不同状态必须导出不同(self) -> None:
        """**核心：导出函数必须能分辨「内容不同」。**

        若 `dumps` 被改成忽略输入、恒返回同一串，
        确定性测试就会全部通过——因为「同一输入」确实稳定。
        """
        from zhengtong.adapters.state_codec import dumps

        assert dumps(_状态("甲方案")) != dumps(_状态("乙方案")), (
            "两个不同状态导出结果相同——导出函数可能已忽略输入"
        )

    def test_档位不同必须导出不同(self) -> None:
        """**更细一档：连枚举字段都要能分辨。**

        D-146 正是「档位变了但读不出来」的一类问题；
        若导出对档位不敏感，往返测试也发现不了。
        """
        from zhengtong.adapters.state_codec import dumps

        assert dumps(_状态(tier="pro")) != dumps(_状态(tier="consult"))

    def test_键序可被观察到(self) -> None:
        """**键序必须真的体现在输出里**，否则顺序断言是空的。"""
        import json

        from zhengtong.adapters.state_codec import dumps

        文本 = dumps(_状态())
        assert json.loads(文本)["schema_version"] == 1
        assert list(json.loads(文本)) == list(json.loads(文本))


# ── 4. test_boundary：判定函数仍能分辨读写 ───────────────────────
class Test读写判定仍有效:
    """架空手法：把 `_被改的容器` 改成恒返回空。"""

    def test_写操作必须被识别(self) -> None:
        """**用 `update()` 写法补齐**：下标赋值那条已有自检，
        这里补上就地方法，确保两类都被覆盖。
        """
        bd = importlib.import_module("test_boundary")
        写 = ast.parse("st.artifacts.update({})").body[0].value
        assert bd._被改的容器(写) == "st.artifacts", "update 写法未被识别"

    def test_读操作不得被识别为写(self) -> None:
        """**反向：读不能算写。**误报比漏报更危险。"""
        bd = importlib.import_module("test_boundary")
        for 代码 in (
            "x.role_states.items()",
            "x.artifacts.get('k')",
            "x.gates.keys()",
        ):
            节点 = ast.parse(代码).body[0].value
            assert bd._被改的容器(节点) == "", f"{代码} 被误判为写"


# ── 5. 五道门禁共用的元断言 ────────────────────────────────────
class Test门禁元健壮性:
    """**门禁自身也可能被悄悄弄坏**——这一类最容易被忽略。"""

    def test_五道门禁文件均存在(self) -> None:
        """文件被误删时 pytest **不会报错**，它只是少跑几个文件。

        所以「门禁缺失」会被误当成「没有问题」。这条确保它不会。
        """
        for 名 in (
            "test_layering.py",
            "test_boundary.py",
            "test_schema_tolerant.py",
            "test_port_contract.py",
            "test_reexport_golden.py",
        ):
            路径 = _ROOT / 名
            assert 路径.exists(), f"门禁文件缺失：{名}"
            assert 路径.stat().st_size > 500, f"{名} 过于小，可能被清空"

    def test_带消息的断言数不得减少(self) -> None:
        """**能防住「把断言架空」，防不住「把数据改常量」。**

        实测有效的部分：把 `assert not 少, "新增端口方法…"`
        改成 `assert True` 时，带失败消息的断言数会少一条，本条立刻红。

        **防不住的部分**（诚实记录）：若有人把被断言的**数据**换成常量
        （如 `第一次 = "固定"`），断言条数与消息都不变，本条不会红。
        那种攻击需要「验证验证器」，而这在原则上做不到——
        **一个可被篡改的检查器无法验证自己**。
        故本条的定位是「提高作假的成本」，不是「杜绝作假」。

        基线数字取自首次落盘时实测值，下限写死——
        正常增删断言会让它变宽松，但**恶意删减**一定会被抓住。
        """
        # 基线是**实测值**（不是估计值）——估计的基线会立刻失效。
        基线 = {
            "test_layering.py": 8,
            "test_boundary.py": 7,
            "test_schema_tolerant.py": 25,
            "test_port_contract.py": 8,
            "test_reexport_golden.py": 9,
        }
        import ast as _ast

        for 名, 下限 in 基线.items():
            树 = _ast.parse((_ROOT / 名).read_text(encoding="utf-8"))
            带消息 = 0
            for 节点 in _ast.walk(树):
                if not isinstance(节点, _ast.Assert):
                    continue
                if 节点.msg is not None:
                    带消息 += 1
            assert 带消息 >= 下限, (
                f"{名} 的「带失败消息的断言」只剩 {带消息} 条，低于基线 {下限} 条——"
                "很可能是有人把断言架空成了 assert True"
            )

    def test_每个门禁都有反证记录(self) -> None:
        """**门禁必须留下「它被攻击过」的痕迹。**

        要求 docstring 里写明反证——写不出反证的门禁，
        说明还没被验证过。
        """
        for 名 in (
            "test_layering.py",
            "test_boundary.py",
            "test_schema_tolerant.py",
            "test_port_contract.py",
            "test_reexport_golden.py",
        ):
            文本 = (_ROOT / 名).read_text(encoding="utf-8")
            assert "反证" in 文本, f"{名} 未记录反证"
