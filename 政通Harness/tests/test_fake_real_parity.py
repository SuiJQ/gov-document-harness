"""替身 → 真件对照判据：**每个替身都必须有对应的真件判据**。

## 这组判据在防什么

体检（`docs/WIRING_AUDIT.md` 阶段 3）查出一件事：替身判据与真件判据
是**分开两批**，没有任何机制保证「每条替身都有对应的真件判据」。
于是出现过这个数：

| 替身 | 替身判据 | 真件判据 |
| --- | --- | --- |
| `FakeLlm` | 53 处 | `HttpLlm` 2 处 |
| `FakeStorage` | 75 处 | `FsStorage` 24 处 |
| `FakeValidators` | 20 处 | `硬约束校验器` 10 处 |
| `FakeSecrets` | 3 处 | `LocalSecrets` **0 处** ❌ |
| `FakeContentPack` | 49 处 | `本地内容包` 17 处 |
| `FakeClock` | 3 处 | `SystemClock` 1 处 |
| `FakeRetriever` | 14 处 | `中文检索` 1 处 |
| `FakeUserFiles` | 17 处 | `LocalUserFiles` 28 处 |

`LocalSecrets` 是**真凭据适配器**——真运行路径上，用户填的 API key
就是经它读的。而它零判据：**真凭据从磁盘读出来这一段，
从来没被执行过**。

假实现的价值在于行为与真实现一致（D-112）。只有替身判据时，
「一致」这件事无人验证——替身与真件可以差很远，而两边都绿。

## 判据本身也做过一次证伪

统计口径是「非 import 行里直接实例化真件名」。这个口径会**误判**：
类名出现在字符串里、或只被 `isinstance` 检查，都会被算成「有判据」。

所以本判据刻意保持宽松——**宁可漏报也不误报**：它只回答
「真件有没有被真正构造过并调用过」，不回答「行为是否一致」。
后者由各真件自己的判据负责。

## 为什么不用「类定义存在」当判据

那是最容易写成、也最没用的判据：真件类存在 ≠ 真件被用过。
D-165 的九次里，真件类一直都在，坏的是**接线**。
"""

from __future__ import annotations

import pathlib
import re

import pytest

_仓库根 = pathlib.Path(__file__).resolve().parents[1]

#: `(替身名, 真件名)` 对照表。
#:
#: 新增替身时**必须**在这里登记，并同时补真件判据——
#: 参数化判据会把漏掉的那一行当场报出来。
#:
#: 这是刻意的仪式成本：登记本身不要钱，
#: 而「想不起来真件判据」正是历史上发生过的那个失败。
_对照表: tuple[tuple[str, str], ...] = (
    ("FakeLlm", "HttpLlm"),
    ("FakeStorage", "FsStorage"),
    ("FakeValidators", "硬约束校验器"),
    ("FakeSecrets", "LocalSecrets"),
    ("FakeContentPack", "本地内容包"),
    ("FakeClock", "SystemClock"),
    ("FakeRetriever", "中文检索"),
    ("FakeUserFiles", "LocalUserFiles"),
)


def _判据源码() -> str:
    """`tests/` 下全部 Python 源码拼成一个大字符串。"""
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((_仓库根 / "tests").rglob("*.py"))
    )


def _出现次数(类名: str) -> int:
    """非 import 行里**直接实例化**该类名的次数。

    排除 import 行的理由：`from ... import FsStorage` 只是把名字搬进来，
    不代表有任何判据构造过它。
    """
    # **类方法调用也算**（`LocalSecrets.from_workspace(...)`）：
    # 第一版只匹配 `类(`，于是真件全是类方法构造的判据全被算成零——
    # 统计口径本身漏报，判据就成了摆设。
    模式 = re.compile(rf"(?<![.\w]){re.escape(类名)}\s*[\(\.]")
    命中 = 0
    for 行 in _判据源码().splitlines():
        if 行.strip().startswith(("from ", "import ")):
            continue
        if 模式.search(行):
            命中 += 1
    return 命中


@pytest.mark.parametrize(("替身", "真件"), _对照表, ids=[真 for _, 真 in _对照表])
def test_每个替身都有对应的真件判据(替身: str, 真件: str) -> None:
    """真件必须被至少一处判据**真正构造并调用**。

    零处时的含义不是「不重要」，而是：**真运行路径上这一段从未被执行**。
    真凭据、真内容包、真存储各有实打实的失败模式
    （路径解析、软链接越权、编码、JSON 解析失败），
    替身全都测不出来。
    """
    次数 = _出现次数(真件)
    assert 次数 > 0, (
        f"{真件}（{替身} 的真件）零判据：真运行路径上这一段从未被执行过。\n"
        f"补一条直接构造 {真件} 并断言行为的判据——"
        f"替身再全也替代不了它（D-112）。"
    )


def test_对照表没有漏登记的替身() -> None:
    """`fakes.py` 里每个 `Fake*` 都必须在对照表里。

    没有这条，往 `_对照表` 里加替身就可能忘了登记，
    于是新替身又变成「有替身判据、无真件判据」——
    **恰好是本组判据要防的那个失败模式自己**。
    """
    源码 = (_仓库根 / "src" / "zhengtong" / "adapters" / "fakes.py").read_text(encoding="utf-8")
    实际 = set(re.findall(r"^class (Fake\w+)", 源码, re.MULTILINE))
    登记 = {替 for 替, _ in _对照表}
    未登记 = 实际 - 登记
    assert not 未登记, f"fakes.py 里有未登记的替身 {sorted(未登记)}：它们没有真件判据对照"
