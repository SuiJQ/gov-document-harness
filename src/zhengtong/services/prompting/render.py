"""D-008「提示词可 diff」的落点：把装配结果渲染成可 diff 的文本。

## 为什么不直接写文件

写盘是**副作用**，属第 16 步接入层（调试模式）。
本模块只负责**渲染**：纯函数、同样输入必得同样字节。
接盘那一侧（`system/logs/prompts/`）等第 16 步，那时再决定何时写。

## 为什么要可 diff

「改一句提示词能看出装配后变成什么样」（D-008）——
前提是**每次装配输出逐字节稳定**。
故本渲染**不带时间戳、不带轮次号、不带随机 id**：
那些字段会让每次 diff 都是全红，可 diff 就成了空话。

轮次信息由调用方写在**文件名**上，而不是文件内容里。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.tier import TierId
from . import 装配结果


@dataclass(frozen=True, slots=True)
class 可diff文本:
    """一份装配结果的文本形态。**字段顺序固定，不含任何时间/轮次信息。**"""

    文本: str

    def __str__(self) -> str:
        return self.文本


def 渲染(结果: 装配结果, *, 档位: TierId | None = None) -> 可diff文本:
    """渲染成可 diff 文本。

    **头部只放装配事实，不放运行事实**：角色、档位、装了哪些条目、
    占了多少、被截了哪些。够定位「为什么这次不一样」，
    又不含时间戳——含了就毁掉 diff。
    """
    头 = [
        f"角色：{结果.角色.value}",
        f"档位：{档位.value if 档位 is not None else '未指定'}",
        f"已装条目：{'、'.join(结果.已装条目) or '（无）'}",
        f"占用：{结果.占用} token",
        f"被截块：{'、'.join(结果.被截块) or '（无）'}",
        "分块占用：",
    ]
    头.extend(f"  {名} = {量}" for 名, 量 in 结果.分块占用().items())
    return 可diff文本(文本="\n".join((*头, "", "=" * 40, "", 结果.提示词正文)))
