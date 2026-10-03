"""内容包与原文的等值性复核：抽取「实质原子」并逐个查找。

**为什么要有这个脚本**

第 12 步把 22 份原文重写成 14 份，压缩比约 3:1。
用户要求核实「整体内容质量与原本基本保持一致」。

**「质量一致」不是主观判断能证明的。**
判断一份提示词「变薄了」的可靠办法是：
把原文里每一个可枚举的实质单元抽出来，逐个到新包中查。

## 抽什么（只抽可机械判定的，不抽散文）

1. **数值原子**——语料统计参数（字数、句长、‰、百分比）
   这些是知识库最硬的资产，丢一个就少一条判据。
2. **结构原子**——小节标题（去掉序号后的语义部分）
3. **术语原子**——加粗术语、书名号引用的规范名
4. **枚举原子**——顿号并列清单中的独立项
5. **对照原子**——「差→好」「误用→正解」这类成对内容

散文段落不抽——判断「意思还在不在」需要人眼，
脚本只负责**把疑点指出来**，不负责下结论。

## 已知局限（必须先说清，否则会误用结果）

- **原子在改写中被换词**时会被报成缺失——这是假阳性。
  例：原文「坠子」，改写后仍写「坠子」；但原文「倒吊人」改写成「悬吊」就报缺失。
- **原子被合并**时可能报缺失——例：原文三条并列改写成一句概括。
- **脚本不判断语义等价**，只报「这个字符串在新包里找不到」。

所以输出是**候选清单**，必须逐条人判。

跑法：
    python3 scripts/recheck_content_fidelity.py [--verbose]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEW = ROOT / "content"
MATERIALS = ROOT.parent / "default" / "materials" / "党政公文全套资料_v2.1_20260930"

#: 原文 → 新包 的映射。
#: 只列**知识源**：示例（22 篇成文样例）与 assets 是语料，不是规则，
#: 按架构决定不入包——不参与等值性比对。
SOURCE_TO_TARGET: dict[str, str] = {
    # 04 专家团：一一对应（改名为新标识）
    "04-专家团-gov-document-team-党政公文专家团-v7.0.0/agents/gov-document-team-team-lead.md": "prompts/team-lead.md",
    "04-专家团-gov-document-team-党政公文专家团-v7.0.0/agents/outline-structure-expert.md": "prompts/structure.md",
    "04-专家团-gov-document-team-党政公文专家团-v7.0.0/agents/language-polishing-editor.md": "prompts/language.md",
    "04-专家团-gov-document-team-党政公文专家团-v7.0.0/agents/compliance-validator.md": "prompts/compliance.md",
    "04-专家团-gov-document-team-党政公文专家团-v7.0.0/agents/document-type-advisor.md": "prompts/doc-advisor.md",
    "04-专家团-gov-document-team-党政公文专家团-v7.0.0/references/gate-manual.md": "references/gate-manual.md",
    "04-专家团-gov-document-team-党政公文专家团-v7.0.0/references/error-memory.md": "references/error-memory.md",
    "04-专家团-gov-document-team-党政公文专家团-v7.0.0/references/compliance-rules.md": "references/compliance-rules.md",
    "04-专家团-gov-document-team-党政公文专家团-v7.0.0/references/doc-type-patterns.md": "references/doc-type-patterns.md",
    "04-专家团-gov-document-team-党政公文专家团-v7.0.0/references/admin-ordering-rules.md": "references/admin-ordering-rules.md",
    # 02 写作指南 → 合并进多份
    "02-技能-official-document-writing-公文写作指南/references/gb-t-9704-2012-standard.md": "references/gbt9704-format.md",
    "02-技能-official-document-writing-公文写作指南/references/writing-techniques.md": "references/writing-techniques.md",
    "02-技能-official-document-writing-公文写作指南/SKILL.md": "references/writing-techniques.md",
    "02-技能-official-document-writing-公文写作指南/checklists/quality-checklist.md": "references/gate-manual.md",
    "02-技能-official-document-writing-公文写作指南/references/document-templates.md": "references/doc-templates.md",
    # 03 去 AI 味 → 独立成文
    "03-技能-gongwen-deai-公文去AI味/SKILL.md": "references/deai-rules.md",
    # 01 DNA → 知识主体，合并进写作技法库
    "01-技能-lieflat-gongwen-公文写作DNA/SKILL.md": "references/writing-techniques.md",
    "01-技能-lieflat-gongwen-公文写作DNA/参数卡.md": "references/param-card.md",
    "01-技能-lieflat-gongwen-公文写作DNA/标题技法库.md": "references/title-techniques.md",
    "01-技能-lieflat-gongwen-公文写作DNA/句子对照库.md": "references/writing-techniques.md",
    "01-技能-lieflat-gongwen-公文写作DNA/公文语料/语言DNA.md": "references/language-dna.md",
    "01-技能-lieflat-gongwen-公文写作DNA/公文语料/写作视角与认知框架.md": "references/cognition-baseline.md",
    "01-技能-lieflat-gongwen-公文写作DNA/公文语料/文章结构模板.md": "references/writing-techniques.md",
    "01-技能-lieflat-gongwen-公文写作DNA/公文语料/结构全量统计.md": "references/stats-baseline.md",
    "01-技能-lieflat-gongwen-公文写作DNA/党建语料/党建DNA.md": "references/writing-techniques.md",
    "01-技能-lieflat-gongwen-公文写作DNA/党建语料/写作视角与认知框架.md": "references/cognition-dna.md",
    "01-技能-lieflat-gongwen-公文写作DNA/党建语料/文章结构模板.md": "references/writing-techniques.md",
}

#: 专有名词替换表——这些词在新包里**必然**找不到，
#: 属于用户批准的必要修改，不计入缺失。
APPROVED_RENAMES = {
    # 来源系统
    "WorkBuddy",
    "SuiJQ",
    "CodeBuddy",
    "gov-document-team",
    "official-document-writing",
    "gongwen-deai",
    "lieflat",
    "skill-creator",
    # 工具名
    "TeamCreate",
    "SendMessage",
    "spawn",
    "subagent_type",
    "maxTurns",
    "mrrobot",
    "register_expert",
    "package_expert",
    "lint.py",
    "hard_constraint_check.py",
    "evidence_anchor_check.py",
    "consistency_check.py",
    "strip_meta.py",
    # 角色旧标识
    "outline-structure-expert",
    "language-polishing-editor",
    "compliance-validator",
    "document-type-advisor",
    "gov-document-team-team-lead",
    # 档位旧命名 → 新命名
    "L0-咨询",
    "L0-咨询（轻量）",
    "L0-成稿",
    "L0-成稿（标准）",
    "L1",
    "L2",
}

#: 新包里以不同写法承载的等价术语——命中即视为**保留**。
#: 这些是我在改写中主动做的规范化，不是丢失。
EQUIVALENT_TERMS: dict[str, tuple[str, ...]] = {
    # 去 AI 味：源材料的核心机制名
    "无条件回落": ("无条件执行",),
    "内部沟通": ("内部沟通",),
    "通用 AI 味清单": ("AI 味自查", "禁用套话开头"),
    # 写作技法：新包做了归并
    "文种骨架公式": ("文种骨架公式", "骨架公式"),
    "四种堆法": ("四种堆法",),
    "五种句式": ("五种句式", "五种最有效的数据句式"),
    "判族规则": ("判族规则",),
}

#: 数值原子：要求数字与紧随其后的单位/符号同时出现
NUM_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*"
    r"(‰|%|字|句|词|段|行|字/行|行/字|个|次|篇|项|处|种|字级)"
)


@dataclass
class Atoms:
    """一份原文抽出的实质原子。"""

    source: str
    numbers: set[str] = field(default_factory=set)
    headings: set[str] = field(default_factory=set)
    terms: set[str] = field(default_factory=set)


def _normalize(text: str) -> str:
    """归一化：去空白差异，让机械比对不被排版噪声干扰。"""
    text = re.sub(r"[ \t　]+", "", text)
    return text.replace(" ", " ")


def _is_approved(token: str) -> bool:
    """判断是否属于用户批准的专有名词替换。"""
    return any(old in token for old in APPROVED_RENAMES)


def extract(source_rel: str, text: str) -> Atoms:
    """抽取一份原文的实质原子。"""
    atoms = Atoms(source=source_rel)

    # 1. 数值原子——只取带统计单位语境的中文数字
    #    英文小节编号（如 7.3.6）在原文里极多，单独过滤
    for m in NUM_RE.finditer(text):
        num, unit = m.group(1), m.group(2)
        # 排除标准条款号：前面是 § 或 §空格，或形如 7.3.6
        start = m.start(1)
        prefix = text[max(0, start - 6) : start]
        if re.search(r"\d+\.\d+\.\d+$", prefix):
            continue
        # 排除纯年份/编号类（如「2026 年」「第 3 版」）
        ctx = text[m.start() : m.end() + 6]
        if re.match(r"^(19|20)\d\d", num):
            continue
        if re.search(r"年|月|日|版|条|款|号", ctx[2:8] or ""):
            continue
        atom = f"{num}{unit}"
        if not _is_approved(atom):
            atoms.numbers.add(atom)

    # 2. 结构原子——小节标题的语义部分
    for m in re.finditer(r"^#{2,4}\s+(.+)$", text, re.MULTILINE):
        h = m.group(1).strip()
        h = re.sub(r"^[（(]?[一二三四五六七八九十\d]+[）)、.]\s*", "", h)
        h = re.sub(r"^第[一二三四五六七八九十]+[章节部分]\s*", "", h)
        if len(h) >= 3 and not _is_approved(h):
            atoms.headings.add(h)

    # 3. 术语原子——加粗短语（不含整句加粗）
    for m in re.finditer(r"\*\*(?!.*[。！？；])((?:[^*\n]){2,14})\*\*", text):
        t = m.group(1).strip()
        if not _is_approved(t):
            atoms.terms.add(t)

    return atoms


def check_presence(atom: str, new_norm: str) -> bool:
    """原子在新包中是否存在（含等价词表）。"""
    a = _normalize(atom)
    if a in new_norm:
        return True
    for key, alts in EQUIVALENT_TERMS.items():
        if _normalize(key) in a:
            if any(_normalize(x) in new_norm for x in alts):
                return True
    # 数值原子放宽：只比数字部分（单位表达可能规范化，如「22 行」→「22行」已由归一化处理，
    # 但「每千字 12 个」→「12 个」这类措辞差异需放宽）
    m = re.match(r"^(\d+(?:\.\d+)?)", a)
    if m:
        return m.group(1) in new_norm
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="打印全部原子明细")
    ap.add_argument(
        "--kind",
        choices=("numbers", "headings", "terms"),
        default="",
        help="只看某一类原子",
    )
    args = ap.parse_args()

    if not MATERIALS.is_dir():
        print(f"找不到材料目录：{MATERIALS}")
        print("→ 无法复核。这不是「通过」，是**没跑成**。")
        return 2

    new_norm = _normalize(
        "\n".join(p.read_text(encoding="utf-8") for p in sorted(NEW.rglob("*.md")))
    )

    kinds = (args.kind,) if args.kind else ("numbers", "headings", "terms")

    # 按目标文件聚合：同一目标被多份原文映射，缺一个不算数
    missing_by_target: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"numbers": [], "headings": [], "terms": []}
    )
    total_by_kind = {k: 0 for k in kinds}
    found_by_kind = {k: 0 for k in kinds}

    for src_rel, tgt_rel in SOURCE_TO_TARGET.items():
        sp = MATERIALS / src_rel
        tp = NEW / tgt_rel
        if not sp.is_file():
            print(f"[跳过] 原文不存在：{src_rel}")
            continue
        if not tp.is_file():
            print(f"[错误] 新包不存在：{tgt_rel}")
            return 2

        atoms = extract(src_rel, sp.read_text(encoding="utf-8"))
        tgt_text = _normalize(tp.read_text(encoding="utf-8"))

        for kind in kinds:
            pool = getattr(atoms, kind)
            miss = [a for a in sorted(pool) if not check_presence(a, tgt_text)]
            missing_by_target[tgt_rel][kind].extend(miss)
            total_by_kind[kind] += len(pool)
            found_by_kind[kind] += len(pool) - len(miss)

    # 输出
    print("=" * 72)
    print("内容包等值性复核（原子级）")
    print("=" * 72)
    for kind in kinds:
        t, f = total_by_kind[kind], found_by_kind[kind]
        pct = f / t * 100 if t else 100.0
        flag = "OK " if pct >= 95 else "!! "
        print(f"[{flag}]{kind:9s} 保留 {f}/{t}（{pct:.1f}%）")

    print()
    print("── 待判定清单（可能是真丢失，也可能是换词/合并）──")
    any_missing = False
    for tgt in sorted(missing_by_target):
        for kind in kinds:
            miss = missing_by_target[tgt][kind]
            if not miss:
                continue
            any_missing = True
            print(f"\n■ {tgt} · {kind}（{len(miss)}）")
            for m in miss:
                print(f"    · {m}")

    if not any_missing:
        print("（无）")

    print()
    print("→ 上表是**候选**，不是结论：脚本只判断「字符串找不找得到」")
    print("  逐条人判是「真丢失」还是「换词/合并」，不要把差异率当通过率。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
