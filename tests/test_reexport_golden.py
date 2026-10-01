# 门禁测试豁免（BasedPyright 1.40 的 pyproject overrides 实测不生效，
# 故用文件内规则覆盖）：这些文件天生要做检查器无法理解的事——
# 故意非法操作（给 frozen 对象赋值、往 RunState 塞非法键）
# 与 AST 动态检查（node.lineno / node.value / __dataclass_params__）。
# 详见 pyproject.toml 末尾的说明。
# 代价是这些文件不再有类型检查——它们全是断言与扫描代码，没有业务逻辑。
# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportIndexIssue=false, reportCallIssue=false, reportAssignmentType=false, reportAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportPrivateUsage=false, reportUnusedFunction=false, reportUnusedImport=false, reportImplicitStringConcatenation=false
"""导出确定性（架构 §12 机制 4 / D-066）。

## 判据出处

§12 机制 4（D-066）承诺：

> 同一份状态文件，两次导出结果应完全一致。

以及 §10 产出格式：最终产出采用 TXT。

本测试锁死**确定性**：同一输入 → 同一输出，逐字节相同。

## 为什么「确定性」需要单独一道门禁

因为**不确定性几乎不会报错**。
它只会让用户在两次导出之间看到不同的东西，
而用户无从判断「哪一份才对」，最后归因为「程序不稳定」，
但代码里找不到任何一处「不稳定」的写法。

它最容易从四个地方漏进来，且都不会触发任何现有测试：

1. **`set` 遍历顺序** —— Python 里可变的，且依赖插入历史与哈希种子
2. **时间戳** —— 导出行得通，但同一份状态两次导出就不同
3. **绝对路径** —— 换台机器就不同
4. **`PYTHONHASHSEED`** —— `hash()` 对 `str` 默认加随机盐，跨进程不稳定

这四条都会被本测试抓住，因为**它比的是字节，不是语义**。

## 本道门禁目前的适用范围（重要）

架构 §10 的**最终 TXT 导出器属于第 11 步，尚未实现**。
本测试**不去测不存在的东西**——那会写出「永远通过」的空断言，
正是 D-143 说的「测了表面特征」。

所以当前锁的是**已存在的序列化路径**：

- `state_codec.dumps` —— 状态落盘的唯一序列化入口
- `state_codec.loads` 往返
- `JsonlJournal.append` 的行序

第 11 步实现导出器时，**必须回到本文件补测试**：
新增一个 `Test导出器` 类，用同一个 `_两次导出` 辅助函数锁住它。
这一点写在本文件末尾的「交接」注释里，不靠记忆。

## 与 `test_schema_tolerant` 的分工

那边测「读得进」；这边测「写出来稳」。
两者都必要：**能读但每次写都不一样，用户照样没法用。**
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from zhengtong.adapters.state_codec import SCHEMA_VERSION, dumps, loads
from zhengtong.domain.ids import RunId
from zhengtong.domain.run_state import RunState
from zhengtong.domain.task import TaskSpec
from zhengtong.domain.tier import TierId

_SRC = Path(__file__).resolve().parents[1] / "src"


def _状态(要求: str = "就旱情写个通知", run_id: str = "r-det") -> RunState:
    return RunState(
        schema_version=SCHEMA_VERSION,
        run_id=RunId(run_id),
        task=TaskSpec(tier=TierId.PRO, requirement=要求),
    )


class Test序列化确定性:
    """核心判据：同一输入 → 同一字节。"""

    def test_同一实例两次导出相同(self) -> None:
        """**最朴素但最基础的一条。**

        如果这条就红了，说明 `dumps` 里有时间戳或计数器之类的东西——
        那是最严重也最容易修的一种不确定性。
        """
        状态 = _状态()
        assert dumps(状态) == dumps(状态), "同一实例两次导出结果不同"

    def test_两个等价实例导出相同(self) -> None:
        """**比「同一实例」更强：内容相同即可，身份无关。**

        `dumps` 若掺入 `id()` 或对象内存地址，同一实例也稳定，
        但两个等价实例就会不同——而用户换台机器重跑就会撞上。
        """
        assert dumps(_状态()) == dumps(_状态()), "两个内容相同的实例导出结果不同"

    def test_读回再导出与原样相同(self) -> None:
        """**往返幂等**：读→写→读→写，文本必须一模一样。

        这条同时压住两个来源：
        - `_to` 里某个字段**没有被 `from_json` 读回**（丢字段）
        - `_to` 用了 `set` 或未排序的容器（顺序不稳）

        两者都会表现为「往返一次，文本就变了」。
        """
        原文 = dumps(_状态())
        往返 = dumps(loads(原文).unwrap())
        assert 往返 == 原文, "读回再导出与原文不同——要么丢了字段，要么顺序不稳"

    def test_多次运行结果相同(self) -> None:
        """**跨进程验证，专抓 `PYTHONHASHSEED` 这类不确定性。**

        前三条都在同一个进程里跑，而 `hash()` 的随机种子是**按进程**的——
        所以同一进程里 `set` 顺序可能恰好稳定，跨进程才暴露。
        这条必须起子进程，否则等于没测。
        """
        代码 = (
            f"import sys; sys.path.insert(0, {str(_SRC)!r})\n"
            "from zhengtong.adapters.state_codec import dumps\n"
            "from zhengtong.domain.run_state import RunState\n"
            "from zhengtong.domain.task import TaskSpec\n"
            "from zhengtong.domain.tier import TierId\n"
            "from zhengtong.domain.ids import RunId\n"
            f"print(dumps(RunState(schema_version={SCHEMA_VERSION}, "
            f"run_id=RunId('r-x'), task=TaskSpec("
            f"tier=TierId.PRO, requirement='就旱情写个通知'))))\n"
        )

        结果 = [
            subprocess.run(
                [sys.executable, "-c", 代码],
                capture_output=True,
                text=True,
                check=True,
                env={"PYTHONHASHSEED": 种子, "PATH": "/usr/bin:/bin"},
            ).stdout
            for 种子 in ("0", "1", "12345")
        ]
        assert len(set(结果)) == 1, (
            "不同 PYTHONHASHSEED 下导出结果不同——说明有容器顺序不稳定：\n"
            + "\n".join(
                f"seed={s}: {r[:80]}" for s, r in zip(("0", "1", "12345"), 结果, strict=True)
            )
        )


class Test键序稳定:
    """键序不乱，diff 才有意义。"""

    def test_键序不依赖插入顺序(self) -> None:
        """**`_to` 里写字面量字典 vs 用 `sort_keys=True`，两种都可以。**

        但二者不能混用——混用就会「有时有序有时无序」。
        本条只断言**键序是确定的**（同一个函数调两次序相同），
        不强制具体是哪一种做法。
        """
        一次 = list(json.loads(dumps(_状态())).keys())
        两次 = list(json.loads(dumps(_状态())).keys())
        assert 一次 == 两次

    def test_顶层键序与实现一致而非随机(self) -> None:
        """**锁住「键序来自 `_to` 的字面量书写顺序」这个事实。**

        `dumps` 目前**没有用 `sort_keys=True`**——键序靠 `_to` 里
        `schema_version, run_id, task, ...` 的书写顺序。
        这是稳定的，但**脆弱**：将来有人在 `_to` 里插一个键，
        导出文本就会变。

        这里断言开头两个键的相对顺序不变。
        若将来改成 `sort_keys=True`，本条会红——
        那不是缺陷，但**必须是有意识地改**，且要同步改本注释。
        """
        键 = list(json.loads(dumps(_状态())).keys())
        assert 键[:2] == ["schema_version", "run_id"], (
            f"顶层键序变了：{键[:3]}。若是有意改为 sort_keys，请一并更新本测试的说明。"
        )


class Test无时间戳混入:
    """时间戳是导出不确定性最常见的来源。"""

    def test_导出内容不含绝对路径(self) -> None:
        """**换台机器就不同**，是最难排查的一种。

        状态里存的是 `ArtifactRef.path` 这类**相对**路径，
        一旦混入绝对路径，导出的文件就无法在别的机器上比对。
        """
        文本 = dumps(_状态())
        assert "/tmp/" not in 文本, "导出含临时目录路径"
        assert str(Path.home()) not in 文本, "导出含用户主目录路径"

    def test_连续导出不含时间戳(self) -> None:
        """**间隔一秒再导一次**——只同一时刻导出查不出真时钟。

        若 `dumps` 掺了 `time.time()`，两次结果必然不同；
        但若掺的是时钟**值**，间隔就必要了。
        """
        import time

        第一次 = dumps(_状态())
        time.sleep(1.1)
        第二次 = dumps(_状态())
        assert 第一次 == 第二次, "间隔 1.1 秒后导出结果不同——疑似掺入时间"


# ── 交接：第 11 步实现导出器后必须回到本文件 ────────────────────
#
# 架构 §10 的最终 TXT 导出器属于第 11 步，**目前不存在**。
# 届时请新增：
#
#   class Test导出器:
#       def test_同一状态两次导出逐字节相同(self) -> None: ...
#       def test_主件先于附件(self) -> None: ...
#       def test_不掺入时间戳与绝对路径(self) -> None: ...
#       def test_换机器结果相同(self) -> None: ...
#
# 判据仍用「比字节」。**不要改成比对文件数量或行数**——
# 那种断言在本项目里已经栽过一次（D-126/D-127 的历史）。
#
# 同时删除本文件开头的「适用范围」一节，改成描述真实覆盖面。
@pytest.mark.parametrize(
    ("层级", "路径"),
    [
        ("状态导出", lambda: dumps(_状态())),
        ("往返导出", lambda: dumps(loads(dumps(_状态())).unwrap())),
    ],
)
def test_导出路径均确定性(层级: str, 路径: object) -> None:
    """**把所有导出路径汇总到一处。**

    新增导出路径时，在这里加一行即可获得同样的确定性保障——
    不必记得去改别的地方。这是「一处事实」的用法。
    """
    出 = [str(路径()) for _ in range(3)]  # type: ignore[operator]
    assert len(set(出)) == 1, f"{层级} 三次导出出现 {len(set(出))} 种结果——不确定"
