"""第二轮复核 · B：矛盾检测。

## ⚠ 先读这段：一个已知测不准的脚本

本脚本**第一版判据有缺陷，证伪时实测到了**：
用「两文件数值集合无交集」判定矛盾，会导致
**同值也误报、单文件也误报**——因为概念关键词在一份文件里常命中多处，
各自的邻近数值天然不相交。

已尝试收紧窗口与取数规则，**仍不可靠**。
所以本脚本**降级为「关键词定位器」**，不再声称能自动判定矛盾：

- 它做的是：**把可能描述同一事实的文件对列出来**，供人眼看；
- 它不做的是：**判断这些描述是否矛盾**。

**结论必须人判。** 若某天有人拿它的输出当「矛盾已排除」的证据，
那是误用——它排除不了任何东西。

## 真正有效的做法（本轮实际用的）

**逐个关键数字，grep 全仓看全部表述，人工比对口径。**
本轮就是这么做的，覆盖 12 个关键数字
（句长、顿号密度、强制词密度、党建族字数、零百分比、层级深度等），
结果：无真矛盾，一处口径说明缺失（已补）。

## 本脚本保留的价值

给出一张「同一概念在哪些文件被提及」的对照表，
提醒复核者**这些地方必须一起看**——
这正是人工比对时最容易漏掉的部分。

跑法：
    python3 scripts/recheck_consistency.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "政通Harness"
NEW = ROOT / "content"


@dataclass(frozen=True)
class Concept:
    """一个可能在多处被表述的事实概念。"""

    name: str
    keywords: tuple[str, ...]


CONCEPTS: tuple[Concept, ...] = (
    Concept("零百分比篇数", ("零百分比", "完全不用百分比", "不用百分比")),
    Concept("平均句长", ("平均句长", "均句长")),
    Concept("强制词密度", ("强制词密度",)),
    Concept("一级标题字数", ("一级标题·", "一级标题（分析", "一级标题（业务")),
    Concept("党建族字数", ("1,119", "1,000-1,200")),
    Concept("版心与行数", ("每面 22 行", "每行 28 字", "版心")),
    Concept("门禁数量", ("12 道", "G0–G13", "G0-G13", "G1–G13")),
    Concept("红线数量", ("E1-E14", "E1–E14", "十四条红线", "14 条红线")),
    Concept("事实三态", ("已核·来源", "假设·待确认")),
    Concept("顿号密度", ("顿号密度", "顿号‰")),
    Concept("收尾模式", ("自然收束",)),
    Concept("挂接模式", ("融合式", "优势转化", "挂接")),
)


def main() -> int:
    files = {
        str(p.relative_to(NEW)): p.read_text(encoding="utf-8") for p in sorted(NEW.rglob("*.md"))
    }
    print("=" * 72)
    print("第二轮复核 · B：矛盾定位器（⚠ 不可自动判定矛盾，须人判）")
    print("=" * 72)
    print(f"内容文件：{len(files)} 份\n")

    # ── 概念表自检 ──
    # 第六轮检查实测：本表的关键字会随内容包改版而**腐烂**，
    # 而本脚本 return 0 恒绿，腐烂了没人知道。
    # 实测到「挂接模式」的关键字全部失效、该概念整条不报；
    # 「版心与行数」的「版本心」等 3 个关键字已死。
    # 故：**关键字全失效 = 报错**，逼人回来更新。
    全文本 = "\n".join(files.values())
    死关键字表: list[str] = []
    for c in CONCEPTS:
        活 = [k for k in c.keywords if k in 全文本]
        if not 活:
            死关键字表.append(f"{c.name}：{list(c.keywords)} 全部失效")
    if 死关键字表:
        print("✗ 概念表已腐烂——下列概念的关键字在内容包里一个都找不到：")
        for d in 死关键字表:
            print(f"    · {d}")
        print("    → 更新 CONCEPTS，或删掉已无关的概念。\n")

    multi = 0
    for concept in CONCEPTS:
        hits = [f for f, t in files.items() if any(k in t for k in concept.keywords)]
        if not hits:
            continue
        if len(hits) == 1:
            continue
        multi += 1
        print(f"■ {concept.name}（{len(hits)} 处提及）")
        for f in hits:
            # 打出该概念附近的实际表述，便于人眼比对
            for kw in concept.keywords:
                for m in re.finditer(re.escape(kw), files[f]):
                    seg = files[f][max(0, m.start() - 30) : m.end() + 40]
                    print(f"    {f}: …{seg.strip()}…")
                    break
        print()

    print("=" * 72)
    print(f"共 {multi} 个概念在多处被提及 —— **这些地方必须一起看**。")
    print("本脚本不判定矛盾。逐条比对口径后，人工确认。")
    return 1 if 死关键字表 else 0


if __name__ == "__main__":
    sys.exit(main())
