"""doctor 健康自检（D-024 四项 + D-048 推理探测 + D-045 字体 + 内容包完整性）。

## doctor 不跑编排

这一点必须先说清，否则整个自检设计的方向就错了。

doctor 做的是**探测**：能不能连上网、能不能鉴权、模型能不能调、
支不支持工具调用、流式能不能用、终端字体行不行、内容包完整吗。
它**不产生编排结果**，所以没有「跑出来一样不一样」可言。

把它当成第四个驱动去比对结果，是范畴错误——
就像拿体检报告去和病历比对。

## 「不提供独立测速按钮」

用户明确要求：不设测速按钮。
故本模块**只探测不测速**：探测是「能不能用」的二元判断，
不是「有多快」的连续指标——后者会诱使用户拿它当性能承诺。

## 流式只探测不使用

用户明确：「流式能力只检测、不使用」。
所以自检里有这一项，但编排路径上**没有**任何流式调用。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class 自检项:
    """一项自检结论。"""

    名称: str
    通过: bool
    详情: str = ""
    #: 用户可以怎么做（D-027 要求持续失败时明确告知三件事：
    #: 失败在哪一步、报了什么错、用户可以怎么处理）。
    处置: str = ""

    def 一行(self) -> str:
        记 = "通过" if self.通过 else "未通过"
        补 = f"（{self.详情}）" if self.详情 else ""
        return f"{self.名称}：{记}{补}"


@dataclass(frozen=True, slots=True)
class 自检结果:
    项: tuple[自检项, ...]

    @property
    def 全通过(self) -> bool:
        return all(项.通过 for 项 in self.项)

    @property
    def 失败项(self) -> tuple[自检项, ...]:
        return tuple(项 for 项 in self.项 if not 项.通过)

    def 行(self) -> tuple[str, ...]:
        return tuple(项.一行() for 项 in self.项)

    def 报告(self) -> str:
        抬头 = "自检结果"
        正文 = "\n".join(self.行())
        if self.全通过:
            return f"{抬头}\n{正文}\n全部通过。"
        处置 = "\n".join(f"- {项.名称}：{项.处置}" for 项 in self.失败项 if 项.处置)
        尾 = f"\n请按下列处置处理：\n{处置}" if 处置 else ""
        return f"{抬头}\n{正文}\n共 {len(self.失败项)} 项未通过。{尾}"


#: 终端字体与等宽显示（D-045）。
#:
#: **不合格只提示、不阻止运行**——用户明确要求。
#: 故它永远是「提示」而非「失败」，`通过` 恒为 True，
#: 详情里说明结果。把「提示」和「失败」分成两列，
#: 是为了让界面上一个走黄色、一个走红色，而不是靠调用方判断。
字体提示: Final[str] = ""


def 跑自检(
    *,
    探测: Sequence[自检项] = (),
    字体: str = "",
) -> 自检结果:
    """汇总各探测项。

    `字体` 是 D-045 的终端检查结果，**只进详情、不影响通过与否**——
    字体不合格时程序仍能跑，只是显示可能错位（用户明确：仅提示不阻止）。
    """
    项 = list(探测)
    if 字体:
        项.append(
            自检项(
                名称="终端字体与等宽显示",
                通过=True,  # 仅提示，不阻止运行（用户明确）
                详情=字体,
            )
        )
    return 自检结果(项=tuple(项))


def 打印自检(结果: 自检结果, *, 流=None) -> None:
    """写到标准输出。

    **默认流式不加任何装饰**——用户要求界面用标准严谨中文，
    不生造符号与框线。
    """
    目标 = 流 or sys.stdout
    目标.write(结果.报告() + "\n")
