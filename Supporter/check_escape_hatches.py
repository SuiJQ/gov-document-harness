#!/usr/bin/env python3
"""接线专项检查：查「**合法但失真**」的三种逃逸口。

## 查什么

`check_wiring.py` 查的是「值存进 `self.X` 后无人读」，
`check_field_consumption.py` 查的是「字段声明了全仓无人读」。
两者都盯「**有没有人在读**」。

本脚本盯的是另一类——**读取发生了，但读取方式是假的**：

| 逃逸口 | 形状 | 为什么假 |
| --- | --- | --- |
| `object` 标注 | `def f(self, 编排器: object)` | 传任何东西都不报错。TUI→编排的接线断了，界面全静默 |
| `# type: ignore` / `# pyright: ignore` | 挂在某行上 | 编译期能抓的问题被推迟到运行时，而运行时正是「不报错」的形态 |
| `getattr(x, "字段", 默认)` | 界面读投影/配置字段 | 取不到就静默返回默认值：成果卡全空、权重恒 0 |

## 为什么这三类要单独查

它们有一个共同特征：**每一种都是合法 Python**。
所以类型检查不报、测试不报、代码读起来完全正常。

实测代价（本轮）：`presentation/tui.py` 的两个 `object` 标注
逼出了三处 `pyright: ignore` 与一处 `getattr` 兜底；
`adapters/tui.py` 的 `getattr(投影, "artifacts", ())`
在字段名写错时**行为判据都不会红**（已实测：证伪实验 B）。

## 为什么 `getattr` 只查「界面读投影字段」这一处

全仓 `getattr` 合理用途很多（反射、跨版本兼容、探测可选字段）。
一刀切禁掉会误报到没法用。

所以只查**一个具体形状**：界面或桥接层读投影字段时的 `getattr`。
那个形状没有任何合理用途——协议已经逐字段声明了结构化字段，
直接属性访问既可读又受类型检查保护。

## 白名单

每条豁免必须写明**为什么合理**，不允许「先放着」。
本轮豁免的 `工具运行器.__getattr__` 属于**反射转发**
（按工具名动态派发），与本脚本要查的「掩盖字段缺失」不是一回事。

## 脚本返回 0 时仍要注入验证

塞一个明知违规的写进去，看它是否真报出来。
不做这一步，就重演 D-247 第一版「任何东西都算合规，
检查永远抓不到东西」。

```bash
printf 'def f(self, x: object):\n    return x\n' >> src/zhengtong/presentation/_注入.py
python3 check_escape_hatches.py   # 必须报出来
rm src/zhengtong/presentation/_注入.py
```
"""

from __future__ import annotations

import tokenize

import pathlib
import re
import sys

#: 项目根。与 `check_wiring.py` 同款写法：`Supporter/../政通Harness`。
#:
#: **不要写成 `parents[1]`**——那指的是 `Harness/`，
#: 于是 `rglob` 扫了个空目录、恒返回 0。
#: 第一版就是这么错的，而症状是「无违规」，看起来完全正常。
#: 它是被**注入验证**抓出来的：塞了明知违规的代码进去，脚本仍报 0。
根 = pathlib.Path(__file__).resolve().parent.parent / "政通Harness"
仓库根 = 根
源码根 = 仓库根 / "src" / "zhengtong"

if not 源码根.is_dir():
    print(f"源码根不存在：{源码根}")
    print("这说明本脚本的根路径算错了——而症状是「恒返回 0」，看起来完全正常。")
    sys.exit(2)

#: `object` 标注与类型忽略**只查界面层**（`presentation/` 与 `adapters/tui.py`）。
#:
#: ## 为什么不全仓查——第一版试过，43 处误报
#:
#: 绝大多数是 `def _as_int(value: object, default: int) -> int` 这类
#: **解析外部输入的正确写法**：TOML / JSON 反序列化后值类型本来就不定，
#: 标 `object` 是标准做法（与 D-389「投影层不能因为发射端写错类型就崩」同源）。
#:
#: 另有两处是**注释里提到** `# type: ignore`（`#:` 块注释，不是 docstring），
#: 朴素行扫描分不出来。
#:
#: 43 处误报的检查等于没有检查——它会被整体豁免，然后谁也不再信任它。
#:
#: 界面层则是**高危区**：`object` 标注在那里会让「TUI→编排」的接线断了
#: 也照样通过（传任何东西都不报错），而界面直接全静默。
#: 本轮实测：界面层清零，所以这个范围既有意义又零误报。
重点目录 = ("presentation", "adapters/tui.py")

#: `getattr` 兜底的检查范围是**全仓**：那个形状没有任何合理用途。
全仓检查 = True

#: 合法豁免：`（文件相对路径, 匹配片段, 理由）`。
豁免: tuple[tuple[str, str, str], ...] = (
    (
        "services/tools/runner.py",
        "def __getattr__",
        "反射转发：按工具名动态派发，不是掩盖字段缺失",
    ),
    (
        "adapters/tui.py",
        "def 收(答案: object)",
        "**框架回调签名逼的**：Textual 的 dismiss 回调在用户取消时传 `None`，"
        "所以参数不能标 `str`。函数体里 `str(答案)` 已做归一——"
        "这与「解析外部输入标 object」同类，是正确写法而非逃避。",
    ),
)

_忽略片段 = ("# noqa",)


def _豁免命中(相对路径: str, 行文本: str) -> str | None:
    """若该行被豁免，返回理由；否则 `None`。"""
    for 路径片, 片段, 理由 in 豁免:
        if 路径片 in 相对路径 and 片段 in 行文本:
            return 理由
    return None


def _扫文本类(路径: pathlib.Path) -> list[tuple[str, int, str, str]]:
    """查 `object` 标注与类型忽略。返回 `(相对路径, 行号, 类型, 行文本)`。"""
    相对 = str(路径.relative_to(仓库根))
    行们 = 路径.read_text(encoding="utf-8").splitlines()
    非代码行 = _注释与文档行(路径)

    命中: list[tuple[str, int, str, str]] = []
    for 序, 行 in enumerate(行们, start=1):
        if any(片 in 行 for 片 in _忽略片段):
            continue
        理由 = _豁免命中(相对, 行)
        if 理由 is not None:
            continue

        if 序 in 非代码行:
            # 注释与文档字符串里写「原先这里标的是 object」不算违规。
            continue

        if re.search(r":\s*object\b", 行) and re.search(r"\bdef\b|->\s*object\b", 行):
            命中.append((相对, 序, "object 标注", 行.strip()))
    命中 += _扫类型忽略(路径, 相对)
    return 命中


#: 真正的类型忽略指令：**整个注释就是这个**。
_忽略指令 = re.compile(r"^#\s*(pyright:\s*ignore|type:\s*ignore)")


def _扫类型忽略(路径: pathlib.Path, 相对: str) -> list[tuple[str, int, str, str]]:
    """按**注释内容**判类型忽略，而不是「该行有没有注释」。

    ## 为什么要看内容

    中间试过「把整个注释行排除」，结果**真正的忽略指令也一起排除了**——
    注入验证立刻发现抓不到 `pyright: ignore`。原因是本仓有大量
    **讲解性注释**（「不用 `# type: ignore` 压过去」「D-018 禁后者」），
    按行排除才能滤掉它们。

    两者只能靠**内容**区分：指令的注释**通体就是**忽略指令，
    讲解的注释里那串字只是被引用的词。
    """
    命中: list[tuple[str, int, str, str]] = []
    with 路径.open(encoding="utf-8") as 句柄:
        for tok in tokenize.generate_tokens(句柄.readline):
            if tok.type is not tokenize.COMMENT:
                continue
            if not _忽略指令.match(tok.string):
                continue
            if _豁免命中(相对, tok.string) is not None:
                continue
            命中.append((相对, tok.start[0], "类型忽略", tok.string.strip()))
    return 命中


def _扫_getattr(路径: pathlib.Path) -> list[tuple[str, int, str, str]]:
    """查**界面读投影字段**这一形状的 `getattr` 兜底。"""
    相对 = str(路径.relative_to(仓库根))
    文本 = 路径.read_text(encoding="utf-8")
    命中: list[tuple[str, int, str, str]] = []
    for 序, 片 in _代码行(路径).items():
        if "getattr" not in 片:
            continue
        # 形状限定：读投影对象的字段。
        # 「界面读投影字段」没有任何合理用途——协议已逐字段声明。
        if "投影" not in 片:
            continue
        if _豁免命中(相对, 片) is not None:
            continue
        命中.append((相对, 序, "getattr 兜底", 片))
    return 命中


def _注释与文档行(路径: pathlib.Path) -> set[int]:
    """**注释行 + docstring 行**——两者都不是代码。

    第一版只滤 docstring，漏掉 ``#:`` 块注释：本仓有两处**在注释里**
    讲解「不用 `# type: ignore` 压过去」「D-018 禁后者」，
    朴素行扫描把它们当成了违规代码。判据抓文档字面量这个坑，
    本轮已经踩到过三次。
    """
    行集: set[int] = set()
    with 路径.open(encoding="utf-8") as 句柄:
        for tok in tokenize.generate_tokens(句柄.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                行集.update(range(tok.start[0], tok.end[0] + 1))
    return 行集


def _代码行(路径: pathlib.Path) -> dict[int, str]:
    """行号 → 只由代码构成的文本（注释与字符串字面量已剔除）。

    剔除字符串是必需的：本仓文档字符串里大量出现
    `pyright: ignore` 与 `object` 这两个词作为**反例讲解**。
    不剔除的话，第一版会把文档当成违规代码报出来——
    那正是本轮写判据时踩过的坑。
    """
    逐行: dict[int, list[str]] = {}
    with 路径.open(encoding="utf-8") as 句柄:
        for tok in tokenize.generate_tokens(句柄.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            逐行.setdefault(tok.start[0], []).append(tok.string)
    return {序: " ".join(片) for 序, 片 in 逐行.items()}


def 主() -> int:
    命中总数 = 0
    for 路径 in sorted(源码根.rglob("*.py")):
        相对 = str(路径.relative_to(仓库根))
        界面层 = any(片段 in 相对 for 片段 in 重点目录)
        命中组: list[tuple[str, int, str, str]] = list(_扫_getattr(路径))
        if 界面层:
            命中组 += _扫文本类(路径)
        for 相对路径, 行号, 类型, 行文本 in 命中组:
            print(f"{相对路径}:{行号}: {类型}：{行文本}")
            命中总数 += 1

    if 命中总数 == 0:
        print("接线逃逸口检查：无违规。")
        return 0
    print()
    print(f"共 {命中总数} 处「合法但失真」的写法。")
    print("这三种都不报错，但会让接线断了之后界面全静默（D-231/D-267/D-296 同族）。")
    print("若某处确属合理，请写进脚本顶部的『豁免』表并注明理由——")
    print("不允许「先放着」。")
    return 1


if __name__ == "__main__":
    sys.exit(主())