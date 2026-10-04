#!/usr/bin/env python3
"""决策号一致性检查（D-241）。

## 这条判据补的是什么缺口

D-151 那次撞号处理时，判据在 `check_content_pack.py` 里，
**只查内容包的 `D-xxx` 引用**。而那次真正抓到的两处悬空引用
恰恰在 `.py` 文件里——靠一次性人工排查发现的。

代码里的 `D-xxx` 引用**至今无判据常驻**。
下次有人写一个不存在的号会安静通过，就像撞号那次溜过去。

## 三条纪律（D-157 撞号那次定的）

1. **不重排既有号**——既有号已被代码与测试引用，重排会连带改一片
2. 撞号就**往后顺延**
3. 引用必须指向**存在的**决策条

第 3 条此前只对内容包生效，本脚本把它扩到代码。

## 允许的例外

- **架构文档的 D-001~D-060**：那些决策登记在 `docs/ARCHITECTURE.md` §14，
  不在 `DECISIONS.md` 里。故 `ARCH_ONLY` 不算悬空。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

根 = Path(__file__).resolve().parent.parent
#: **决策文档不止一份**。原先只有 `DECISIONS.md`，
#: 后来按性质拆成规则 / 经验 / 经历 / 待办四份——
#: 检查必须认全部，否则搬走的正文里的决策号一律判成「不存在」。
#:
#: D-151 就是这么炸的：它只出现在 D-156 的正文里（讲第五轮撞号那段），
#: 正文一搬走，这个号就「消失」了，于是 `scripts/` 里引用它的三处全红。
决策文件 = 根 / "DECISIONS.md"
决策文件集 = (
    决策文件,
    根 / "RULES.md",
    根 / "EXPERIENCE.md",
    根 / "TODO.md",
)
架构文件 = 根 / "docs" / "ARCHITECTURE.md"

号格式 = re.compile(r"D-(\d{3})")


def _已定义(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {f"D-{m.group(1)}" for m in 号格式.finditer(path.read_text(encoding="utf-8"))}


def _是存根(块: str) -> bool:
    """这一块是不是「只留一行、正文在别处」的存根。

    判据看**除标题外还有没有实质内容**：
    只剩 `→ 全文见 …` 这类指路的话，它就不是一份正文。
    """
    体 = [行.strip() for 行 in 块.split("\n")[1:] if 行.strip()]
    if not 体:
        return True
    return all(行.startswith(("→", ">")) for 行 in 体)


def _被引用处() -> list[tuple[Path, int, str]]:
    出: list[tuple[Path, int, str]] = []
    目录 = [根 / "src", 根 / "tests", 根 / "content", 根 / "scripts"]
    for 目 in 目录:
        if not 目.exists():
            continue
        for p in sorted(目.rglob("*")):
            if p.is_dir() or "__pycache__" in str(p) or p.suffix in (".pyc", ".so"):
                continue
            if p.suffix not in (".py", ".md", ".toml"):
                continue
            try:
                文本 = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for i, 行 in enumerate(文本.splitlines(), 1):
                for m in 号格式.finditer(行):
                    出.append((p, i, f"D-{m.group(1)}"))
    return 出


def main() -> int:
    已有 = set().union(*(_已定义(f) for f in 决策文件集 if f.is_file()))
    架构有 = _已定义(架构文件)

    错误: list[str] = []

    # 1) 决策文档内部不得重号——**跨文件也算重号**。
    #    拆成四份之后，同一个号出现在两个文件里同样是重号，
    #    只查单文件会漏掉「搬过去忘了搬回来」。
    #
    #    **但「台账存根 + 目标文件正文」不算重号**，那是拆分后的正常形状：
    #    存根只有一行 `→ 全文见 X.md`，正文只在目标文件里存一份。
    #    不跳过它就会报出 366 条假重号——判据一旦必红就等于没有判据。
    标题: list[str] = []
    for f in 决策文件集:
        if not f.is_file():
            continue
        for 块 in re.split(r"^### ", f.read_text(encoding="utf-8"), flags=re.M)[1:]:
            m = re.match(r"(D-\d{3})", 块)
            if not m:
                continue
            if _是存根(块):
                continue
            标题.append(m.group(1))
    重号 = {d for d in 标题 if 标题.count(d) > 1}
    for d in sorted(重号):
        错误.append(f"决策文档里 {d} 出现多次")

    # 2) 代码/内容包引用的号必须存在
    for p, 行, d in _被引用处():
        if d in 已有 or d in 架构有:
            continue
        # 决策文档自身的行内引用不算（它们就是定义处）
        if p in 决策文件集:
            continue
        错误.append(f"{p}:{行} 引用了不存在的决策号 {d}")

    if 错误:
        print("决策号检查未通过：")
        for e in 错误:
            print(f"  - {e}")
        return 1

    print(
        f"决策号检查通过：{len(标题)} 条决策正文，无重号，"
        f"代码与内容包引用全部有定义。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())