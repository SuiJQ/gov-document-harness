"""终端显示能力自检（D-045）。

## 这件事真正能测什么，不能测什么

D-045 原来的说法是「终端汉字字形和等宽显示能力」，但**这两样有一半测不了**：

| 想测的 | 能测吗 | 为什么 |
|---|---|---|
| 输出编码能不能表示汉字 | **能** | 直接 encode/decode 一遍就知道，且在 Windows 上会真出问题 |
| 输出是不是交互终端 | **能** | `isatty()` |
| 终端尺寸 | **能** | `get_terminal_size()` |
| **字体有没有汉字字形** | **不能** | 程序没法问终端「你能不能画『政』字」。问出来的答案是「能」，然后显示成豆腐块 □□□ |
| 中文是不是双宽 | 不用测 | Textual 自己按 `wcwidth` 算，程序去重复测一遍只会得出「以 Textual 为准」 |

所以本模块**不去假装能测字形**，只测可测的那部分。

## 为什么测「往返」而不是测「编码名」

问「你的编码是不是 UTF-8」得到的是名字；
问「你能不能把『政』这个字原样写出去再读回来」得到的是**事实**。

后者在 Windows 上尤其重要：控制台代码页可能是 GBK，
而 GBK **能编码汉字**——所以只看编码名会误判为「没问题」，
实际上某些字符（如「▣」「⚠」）根本不在 GBK 里，一写就抛异常。

判据用一组**故意挑过的字符**：常用汉字 + 界面里真会用的符号。

## 仅提示、不阻止

用户明确要求：字形不合格也**照常运行**。
所以本模块只产出 `提示` 文本，由调用方决定怎么显示，
**不抛异常、不返回失败**。

界面里本模块不直接调用——`adapters` 与 `presentation` 之间没有依赖方向
（分层判据），故由装配根算好后注入（与 `字体` 参数同一个设计）。
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: 检测用的字符。**故意包含界面里真会出现的符号**——
#: 纯汉字的 GBK 往返是过得去的，真正卡住的是那些符号
#: （`⚠ ▣ ✓ ✗` 有的不在 GBK 里）。只测汉字会漏掉最常见的那个坑。
检测字符: str = "政通公文⚠▣✓✗"

#: 宽度低于此值就提示。中文是双宽字符，窄终端里框线与表格会错位。
最小可用宽度: int = 60


@runtime_checkable
class 类终端流(Protocol):
    """本模块对「输出流」的全部要求，**就这两样**。

    ## 为什么不写 `TextIO`

    第一版签名是 `流: TextIO`，而实际上只用 `encoding` 和 `isatty()`。
    后果是**测试没法传假流**——要构造一个真 `TextIO` 得继承 `io.TextIOBase`，
    而那个基类的 `encoding` 是**只读属性**，赋不了值。

    写 Protocol 把真实契约讲清楚，测试就能按消费方真正用到的东西来伪造。
    `sys.stdout` 天然满足它。
    """

    @property
    def encoding(self) -> str: ...

    def isatty(self) -> bool: ...


@dataclass(frozen=True)
class 终端能力:
    """一次终端显示能力探测的结果。**`提示` 为空即一切正常。**"""

    编码: str
    中文可显示: bool
    是终端: bool
    宽度: int
    高度: int
    提示: str = ""

    @property
    def 正常(self) -> bool:
        return not self.提示


def 中文能否往返(流: 类终端流) -> tuple[bool, str]:
    """把这几个字 encode 出去再 decode 回来，看是不是原样。

    ## 为什么直接试而不是查编码名

    GBK 能编码绝大多数汉字，只看编码名会误判为「没问题」；
    而界面里那些符号（`⚠ ▣ ✓ ✗`）有些不在 GBK 里，一写就炸。
    往返测的是事实，不是名字。
    """
    编码 = getattr(流, "encoding", None) or ""
    if not 编码:
        return False, f"输出流没有编码信息（读到 {type(流).__name__}.encoding 为空）"
    try:
        数据 = 检测字符.encode(编码, errors="strict")
    except UnicodeEncodeError as 错:
        缺 = 检测字符[错.start]
        return False, f"当前输出编码「{编码}」写不出字符「{缺}」"
    except LookupError:
        return False, f"未知的输出编码「{编码}」"
    try:
        回来 = 数据.decode(编码, errors="strict")
    except UnicodeDecodeError as 错:  # 分支各走各的，不是循环
        return False, f"当前输出编码「{编码}」回读失败（第 {错.start} 字节）"
    if 回来 != 检测字符:
        return False, f"当前输出编码「{编码}」往返后内容变了（可能是替换字符）"
    return True, ""


def 查终端显示能力(流: 类终端流 | None = None, *, 流名: str = "stdout") -> 终端能力:
    """探测一次。**永不抛异常**——检测失败本身就是一条提示，不是崩溃。"""
    目标 = 流 if 流 is not None else sys.stdout
    宽高 = shutil.get_terminal_size(fallback=(80, 24))
    提示们: list[str] = []

    可显示, 编码提示 = 中文能否往返(目标)
    if not 可显示:
        提示们.append(
            f"{编码提示}。界面可能出现乱码或方块。"
            "建议改用 Windows Terminal，并在系统区域设置里勾选"
            "「使用 Unicode UTF-8 提供全球语言支持」。"
        )

    # **`isatty` 要防抛**。这不是洁癖：探测代码崩在 `getattr` 上，
    # 用户看到的是「程序起不来」，而这条路径存在的意义恰恰是
    # 「起不来之前先说清楚」。**探测失败自己变成提示**，
    # 而不是把「提示」这个功能本身变成崩溃源。
    try:
        是终端 = bool(目标.isatty())
    except Exception:  # 这里要兜住一切：探测不得抛
        提示们.append(f"无法判断 {流名} 是否为交互终端（探测本身出错）。界面按非交互方式绘制。")
        是终端 = False
    if not 是终端 and not any("探测本身出错" in 条 for 条 in 提示们):
        提示们.append(
            f"输出不是交互终端（{流名} 被重定向）。界面按非交互方式绘制，显示可能不正常。"
        )

    if 宽高.columns < 最小可用宽度:
        提示们.append(
            f"终端宽度只有 {宽高.columns} 列，少于建议的 {最小可用宽度} 列。"
            "中文是双宽字符，框线与表格容易错位，建议拉宽窗口。"
        )

    return 终端能力(
        编码=getattr(目标, "encoding", "") or "",
        中文可显示=可显示,
        是终端=是终端,
        宽度=宽高.columns,
        高度=宽高.lines,
        提示="　".join(提示们),
    )
