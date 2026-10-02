"""第二轮复核 · A：伪造检测（反向扫描）。

**为什么必须有这个脚本**

第一轮 `recheck_content_fidelity.py` 是**单向**的：原文 → 新包，问「原子还在吗」。
这只测**流失**。

> **结构性盲区**：改写时若**编造**了一个数字或引文，
> 它不会出现在原文里，第一轮**永远查不出来**。
> 第一轮「176/176 全绿 + 原子保留率回升」**都不构成「没有造假」的证据**。

本脚本反向扫描：**新包 → 原文**，问「这个数字/引文有出处吗」。

## 扫什么

1. **统计数字**——带统计语境单位（% ‰ 篇 字 次 个 条 元）的数值。
   这是最容易编造的一类：一个「60%」看起来无害，但它会变成模型的写作依据。
2. **引号引文**——中文引号里的整句，判断是否为原文真实存在的引文。
3. **法规/国标编号**——GB/T、条例第八条等，确认没编造标准号。

## 关键设计：自造内容的白名单

新包里有大量**故意的自造内容**，不是造假：

- 「〔示意〕」标记的示例标题（原文就是自造的）
- GB/T 9704 的条款号（来自公开国标，不在私有语料里）
- 本包自己的机制编号（D-Card 字段、G 门禁、E 红线）
- 决策编号 D-xxx

所以脚本必须区分「**引用语料的事实**」与「**本包自造的机制**」。
判据：**带统计单位的数字**才进检查（机制编号不带那些单位）。

## 输出是候选，不是结论

数字找不到出处，可能是：
- 真的编造了（必须补）
- 换算/改写自原文（应可追溯，需人判）
- 本包自造的机制参数（正常）
- 原文本身就有笔误（继承下来，正常）

**逐条人判。**

跑法：
    python3 scripts/recheck_no_fabrication.py [--numbers-only]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEW = ROOT / "content"
MATERIALS = ROOT.parent / "default" / "materials" / "党政公文全套资料_v2.1_20260930"

#: 本包自造机制的编号前缀——不参与伪造检查
SELF_MADE_PREFIXES = (
    "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9",
    "G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9",
    "G10", "G11", "G12", "G13",
    "E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9",
    "E10", "E11", "E12", "E13", "E14",
    "R9", "R11", "R13", "R8", "R10",
)

#: 统计语境单位——带这些单位的数值才进检查
STAT_UNITS = ("%", "‰", "篇", "次", "个", "条", "元", "字", "处", "项")

#: 中文引号内的成句（≥6 字）视为候选引文。
#: **必须排除换行**：否则「…」跨行时会把中间的 Markdown 标记
#: （**粗体**、- 列表符）一起吞进来，报出一堆根本不是引文的假阳性
#: ——第一版就栽在这：193 条候选里绝大多数是格式化噪声。
QUOTE_RE = re.compile(r"[「“]([^「」“”\"\n]{6,80})[」”]")


def _norm(t: str) -> str:
    return re.sub(r"[ \t　]+", "", t).replace(" ", " ")


@dataclass(frozen=True)
class Finding:
    """一条候选：某个原子在新包里，但原文找不到出处。"""

    kind: str
    atom: str
    where: str
    context: str


def load_corpus() -> str:
    """原文全部文本（归一化），供反向查找。"""
    if not MATERIALS.is_dir():
        print(f"找不到材料目录：{MATERIALS}")
        print("→ 无法复核。这不是「通过」，是**没跑成**。")
        sys.exit(2)
    parts = []
    for p in sorted(MATERIALS.rglob("*.md")):
        parts.append(_norm(p.read_text(encoding="utf-8")))
    return "\n".join(parts)


def is_self_made(atom: str) -> bool:
    """判断是否为本包自造的机制编号。"""
    return any(atom.startswith(p) for p in SELF_MADE_PREFIXES)


def check_numbers(corpus: str) -> list[Finding]:
    """检查新包中的统计数字能否在原文找到。"""
    out: list[Finding] = []
    for p in sorted(NEW.rglob("*.md")):
        text = p.read_text(encoding="utf-8")
        rel = str(p.relative_to(ROOT))
        ntext = _norm(text)
        # 抓「数字 + 统计单位」，排除纯序号与标准号
        for m in re.finditer(
            r"(?<![\w.])(\d+(?:\.\d+)?)\s*(" + "|".join(map(re.escape, STAT_UNITS)) + r")",
            text,
        ):
            num, unit = m.group(1), m.group(2)
            # 排除年份
            if re.match(r"^(19|20)\d\d$", num):
                continue
            # 排除国标/年份区间（如 2012 年）
            ctx = text[max(0, m.start() - 30) : m.end() + 30]
            if re.search(r"(19|20)\d\d", num):
                continue
            atom = f"{num}{unit}"
            if is_self_made(atom):
                continue
            # 回原文找：数字紧邻该单位（允许空格/换行差异）
            pat = re.escape(num) + r"\s{0,3}" + re.escape(unit)
            if re.search(pat, corpus):
                continue
            # 数字在原文中出现，但单位表达不同 —— 弱匹配。
            # **必须用数字边界**，不能用 `num in corpus`：
            # 否则「8888」会因原文某个电话号码里含「8888」而被判有出处，
            # 伪造的数字就能藏在更长的数字里混过去（证伪时实测到过）。
            if re.search(r"(?<![\d.])" + re.escape(num) + r"(?![\d.])", corpus):
                out.append(Finding("弱", atom, rel, ctx.replace("\n", " ")[:70]))
            else:
                out.append(Finding("强", atom, rel, ctx.replace("\n", " ")[:70]))
    return out


def check_quotes(corpus: str) -> list[Finding]:
    """检查新包中的成句引文能否在原文找到。"""
    out: list[Finding] = []
    for p in sorted(NEW.rglob("*.md")):
        text = p.read_text(encoding="utf-8")
        rel = str(p.relative_to(ROOT))
        for m in QUOTE_RE.finditer(text):
            q = _norm(m.group(1))
            # 含 Markdown 标记的不是引文，是格式化噪声
            if "**" in q or q.startswith(("-", "|", "#")) or "`" in q:
                continue
            # 跳过本包自造示例（原文也是自造的，或标了示意）
            ctx = text[max(0, m.start() - 60) : m.end() + 20]
            if "示意" in ctx or "自造" in ctx:
                continue
            # 跳过本包机制语句（D-Card 字段、红线、门禁用语）
            if any(pre in q for pre in SELF_MADE_PREFIXES):
                continue
            if "…" in q or q.startswith("…"):
                continue
            # 术语性引号（不是整句引文）：如「妥否，请批示」「专此函达」
            # 这些是公文固定用语，原文可能有也可能没有，不构成造假风险。
            # 只保留**看起来像自然语言陈述句**的（≥10 字且含逗号或句号）
            if len(q) < 10 or not re.search(r"[，。；]", q):
                continue
            # 取引文较长的一段做匹配，降低短句误报
            probe = q[:20]
            if probe and probe in corpus:
                continue
            out.append(Finding("引文", q[:40], rel, ctx.replace("\n", " ")[:70]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--numbers-only", action="store_true")
    args = ap.parse_args()

    corpus = load_corpus()
    print("=" * 72)
    print("第二轮复核 · A：伪造检测（新包 → 原文 反向扫描）")
    print("=" * 72)
    print(f"原文语料：{len(corpus):,} 字（归一化后）\n")

    findings = check_numbers(corpus)
    if not args.numbers_only:
        findings += check_quotes(corpus)

    strong = [f for f in findings if f.kind == "强"]
    weak = [f for f in findings if f.kind == "弱"]
    quotes = [f for f in findings if f.kind == "引文"]

    print(f"强缺失（数字在原文完全找不到）：{len(strong)}")
    print(f"弱匹配（数字在但单位表述不同）：{len(weak)}")
    print(f"引文候选（成句引文找不到出处）：{len(quotes)}")

    for title, group in (("强缺失", strong), ("弱匹配", weak), ("引文", quotes)):
        if not group:
            continue
        print(f"\n── {title} ──")
        by_file: dict[str, list[Finding]] = defaultdict(list)
        for f in group:
            by_file[f.where].append(f)
        for where in sorted(by_file):
            print(f"\n■ {where}")
            for f in by_file[where]:
                print(f"    [{f.kind}] {f.atom}")
                print(f"           上下文: …{f.context}…")

    print()
    print("=" * 72)
    print("这些是**候选**。逐条人判时按以下分类：")
    print("  ① 真编造      → 必须改")
    print("  ② 换算/改写    → 可追溯即可（能对上原文意思）")
    print("  ③ 本包自造机制 → 正常（D-Card/门禁/红线的参数是本项目定义的）")
    print("  ④ 原文笔误     → 继承下来，可保留或标注")
    return 0


if __name__ == "__main__":
    sys.exit(main())
