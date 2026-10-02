"""字节码陈旧检查（本轮吃过一次大亏）。

## 为什么要有这个脚本

2026-10-02 修第一轮接缝时，我改了 `orchestrate.py` 的源码，
`inspect.getsource` / `linecache` / `grep` / `sed` **读出来的全是新代码**，
而 Python **运行的却是旧字节码**。

表现：`_构造请求` 里明明有 `if self.编排.内容包 is not None:`，
真跑却每次进 else 分支。我为此连续验了对象身份、`isinstance`、
`__bool__`、类定义行号、文件路径——**全部「正常」**。

最后靠 `dis.get_instructions` 打印字节码才看穿：
那条 `LOAD_ATTR '内容包'` **压根没被编译进代码对象**。

## 为什么四种常规手段全都失效

| 手段 | 为什么看不出问题 |
|---|---|
| `sed`/`grep` 看文件 | 文件确实是新的——**文件没骗人** |
| `inspect.getsource` | 它读**文件**，不读运行时的代码对象 |
| `linecache` | 同上，读的是磁盘文本 |
| 打印对象属性 | 属性值全都对——**骗的是代码路径，不是数据** |

## 本脚本查什么

`.pyc` 的头部记录了**它编译自哪个源文件、当时该文件的 mtime 与大小**。
Python 只在两者都匹配时才复用缓存。所以只要查：
**存不存在「比它对应的 .py 更新」的 `.pyc`**，就足够了。

## 什么时候跑

- 发现「源码明明这么写、行为却不这么走」时（**先跑这个，再怀疑别的**）
- 提交前（`check.sh` 已接入）
"""

from __future__ import annotations

import sys
from pathlib import Path


def 找出陈旧缓存(根: Path) -> list[Path]:
    """返回所有**比源文件旧**的 `.pyc`。

    ## 方向写反过一次

    第一版写的是 `pyc.mtime > 源.mtime` 判陈旧——
    而正常情况恰恰是 **pyc 比源新**（它编译自源，时间戳随编译时刻走）。
    结果每次跑都报几十个「陈旧」，**第三次就会被当成例行噪音忽略**，
    那这个脚本就白写了。

    真正的陈旧是**源比 pyc 新**：源改了而缓存没重新生成，
    Python 会继续用旧的。

    ## 只按 mtime 判够不够

    够：`.pyc` 头部本来就记录了编译时的源 mtime 与大小，
    Python 据此决定是否重编。我们这里**只用来给人看**，
    判准一点不要紧——**宁可漏报也不误报**，理由见上。
    """
    陈旧: list[Path] = []
    for pyc in 根.rglob("*.pyc"):
        源 = pyc.parent.parent / (pyc.name.split(".")[0] + ".py")
        if not 源.is_file():
            陈旧.append(pyc)  # 源都没了，缓存纯垃圾
        elif pyc.stat().st_mtime < 源.stat().st_mtime:
            陈旧.append(pyc)
    return 陈旧


def 主() -> int:
    import argparse

    解析 = argparse.ArgumentParser(description="查陈旧 .pyc")
    解析.add_argument(
        "--清理",
        action="store_true",
        help="删掉陈旧 .pyc（可再生，故不需 trash）",
    )
    args = 解析.parse_args()

    根 = Path(__file__).parent.parent / "src"
    陈旧 = 找出陈旧缓存(根)
    if args.清理:
        for p in 陈旧:
            p.unlink()
        print(f"已清理 {len(陈旧)} 个陈旧 .pyc")
        return 0
    if not 陈旧:
        print("→ 字节码缓存全部新鲜")
        return 0
    print(f"发现 {len(陈旧)} 个陈旧 .pyc（源码比缓存新，运行时会跑旧代码）：")
    for p in 陈旧[:10]:
        源 = p.parent.parent / (p.name.split(".")[0] + ".py")
        print(
            f"  {p.relative_to(根)}"
            + (f"  ← 源 {源.stat().st_mtime:.0f}" if 源.is_file() else "  ← 源已不存在")
        )
    print("处理办法：删除 .pyc（可再生），或用 python -B 运行。")
    return 1


if __name__ == "__main__":
    sys.exit(主())
