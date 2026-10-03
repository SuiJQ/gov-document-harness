"""内容包 · 信息架构检查：谁读了什么、什么时候读、有没有死文件。

> ## ⚠️ 先读：A 项可信，B/C/D 项是**提示器不是判据**
>
> **A（死文件检测）可信**——字符串级的「有没有人提到这个文件」不会误判，
> 实测也确实抓到了真问题（error-memory 曾是死文件）。
>
> **B（读取时机）不可靠，已连续三轮返工**：
> ① 窗口把共用开头的列表项切出触发词；
> ② 整段 vague 连坐（同段 A 的「详见」连累 B、C）；
> ③ 改成模式自动发现后又换了 7 处——**改的是脚本不是内容**，
> 而内容当时已是对的。
>
> **教训**：判据若需要靠调参来「让输出好看」，它就不是判据。
> 故 B/C/D **不接入 check.sh**，只作人工排查线索。
> **真正靠得住的只有 A：有没有人引用。**

## 为什么要有这个脚本

前两轮校验全是「**内容在不在**」——
红线齐不齐、门禁编号有没有、哈希对不对。
**没有任何一项检查「这些文件模型到底读不读」**。

> 一个写了三千行、却**没有任何角色在提示词里提到**的 references，
> 在产出时就是**死文件**：校验全绿，实际 0 作用。

这是前两轮的结构性盲区。本脚本补上。

## 检查什么

### A. 孤儿检测（死文件）
每份 references 被哪些 prompt 提到？没人提到 = 死文件。

### B. 读取时机缺失
提示词提到某文件时，**有没有说明什么时候读**？
只写「详见 X」而不写何时读，模型可能跳过。

### C. 职责越界
角色是否引用了**不属于自己职责**的文件？
例：陈纲举（结构）去读 deai-rules（语言域）= 越界，
可能导致两个角色都改语言，与「单一归属」冲突。

### D. 呼应闭环
references 之间互相引用是否成环？
A 指向 B、B 又指向 A，且内容不冲突 = 好的呼应。
A 指向 B、B 不认识 A = 断链。

## 断言的是什么（必须说清）

本脚本断言的是「**字符串层面的引用关系**」：
`prompts/structure.md` 里是否出现 `references/param-card.md`。

**它断言不了**：
- 模型是否真的读了这个文件（那是运行期行为）
- 文件内容是否有用（那是人的判断）
- 职责划分是否合理（需要业务判断）

跑法：
    python3 scripts/check_content_topology.py
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "content" / "prompts"
REFERENCES = ROOT / "content" / "references"

#: 各角色职责（用于越界检查）
ROLE_DOMAIN = {
    "team-lead": {"职责": "编排/终审/整合", "域": "全域（但不做专业产出）"},
    "structure": {"职责": "因果与结构", "域": "结构"},
    "language": {"职责": "语言与起草", "域": "语言"},
    "compliance": {"职责": "合规与格式", "域": "合规/格式"},
    "doc-advisor": {"职责": "文种判定", "域": "前端研判"},
}

#: 文件的「本域归属」——一个文件原则上只服务一个域。
#: 跨域引用是允许的（专业文件会跨），但须是**该角色确实要用的**。
FILE_DOMAIN = {
    "gate-manual.md": "全域",
    "error-memory.md": "全域",
    "compliance-rules.md": "合规",
    "admin-ordering-rules.md": "前端研判",
    "doc-type-patterns.md": "前端研判",
    "deai-rules.md": "语言",
    "writing-techniques.md": "结构+语言",
    "gbt9704-format.md": "合规",
    "punctuation.md": "语言",
    "stats-baseline.md": "结构",
    "language-dna.md": "语言",
    "param-card.md": "结构+语言",
    "title-techniques.md": "结构+语言",
    "cognition-dna.md": "结构",
    "cognition-baseline.md": "结构",
    "doc-templates.md": "结构",
}

#: 明显越界的组合（角色 × 文件）——这些是**需要人判断的可疑项**，不是硬错误。
#: 例：结构角色读去AI味规则 = 两个角色都在管语言，与单一归属冲突。
SUSPECT_CROSS_DOMAIN = {
    ("structure", "deai-rules.md"): "去 AI 味属语言域，结构角色读它易越界改语言",
    ("structure", "punctuation.md"): "标点属语言域",
    ("structure", "language-dna.md"): "语言 DNA 属语言域",
    ("doc-advisor", "deai-rules.md"): "去 AI 味属语言域",
    ("doc-advisor", "writing-techniques.md"): "写作技法属结构/语言域，非前端研判",
    ("doc-advisor", "param-card.md"): "参数卡属结构/语言域",
}


@dataclass
class Ref:
    src: str
    dst: str
    has_timing: bool


#: 触发词的**识别模式**（不是词表——词表会与内容脱节）。
#: 命中任一即认为该段声明了「何时读」。
TIMING_PATTERNS = (
    r"前(?!往)",            # 起草前 / 落笔前 / 写结构前 / 终稿前 / 定稿前
    r"(?<![一-鿿])[时当](?:读|查|看)",  # 时读 / 时查 / 当时 / 仅当
    r"遇到|遇该|遇此|遇争议",
    r"仅在|仅遇|按需|查阅|参见时",
    r"首次|全过程|全程|自检时|争议|质询|存疑",
    r"必读|必看|必查|须读",
    r"无条件|不得跳过|每次|一律执行",
    r"起草前|落笔|写完|成稿前|交付前|收尾时",
    r"拟用|拟标题|拟定|判定时|出稿前",
)


def has_timing_word(ctx: str) -> bool:
    """判断段落是否声明了读取时机。

    **用模式而非词表**：手工维护的词表必然与内容脱节——
    实测补内容时新加了「排序前必读」「定层级深度时」，
    词表没同步，脚本报出**过期的「缺时机」**，差点据此去改本来正确的内容。
    """
    return any(re.search(p, ctx) for p in TIMING_PATTERNS)


def extract_refs(prompt_text: str) -> list[tuple[str, bool]]:
    """抽取引用，返回 (文件名, 是否声明了读取时机)。

    「声明时机」= 引用前后 80 字内出现
    触发词（起草前/首次/遇/时/前读/仅在/按需…）。
    """
    out = []
    for m in re.finditer(r"`references/([A-Za-z0-9_.-]+\.md)`", prompt_text):
        name = m.group(1)
        # 窗口取「所在段落」而非固定 ±80 字——
        # 证伪时实测：列表里多项共用一个开头（「写结构前先看 A、B、C」），
        # 固定窗口会把 B、C 切出触发词，误报成「没声明时机」。
        para_start = prompt_text.rfind("\n\n", 0, m.start()) + 2
        para_end = prompt_text.find("\n\n", m.end())
        para_end = para_end if para_end > 0 else len(prompt_text)
        ctx = prompt_text[para_start:para_end]
        # 触发判定：**自动发现**，不手工维护词表。
        #
        # 手工词表与内容各自演化，实测踩了两次：
        # 补内容时加了「排序前必读 / 定层级深度时」等触发词，
        # 词表没同步，脚本报出一堆**过期的「缺时机」**——
        # 差点据此去改本来正确的内容。
        has_timing = has_timing_word(ctx)
        # 「详见 / 参见」只说去哪看，**不说何时看**——
        # 这是最典型的「有引用但模型会跳过」。
        # 证伪时实测到首版把「详见 X」误判为已声明时机，故单列排除。
        #
        # **只看引用前 30 字，不看整段**：
        # 证伪时又实测到整段判定会连坐——一段里 A 写了「详见 X」，
        # 会把同段的 B、C 也判成 vague，尽管 B、C 自己带了「时查」。
        near = prompt_text[max(para_start, m.start() - 30) : m.start()]
        vague = any(w in near for w in ("详见", "参见"))
        out.append((name, has_timing and not vague))
    return out


def main() -> int:
    print("=" * 72)
    print("内容包 · 信息架构检查（谁读了什么 / 何时读 / 有无死文件）")
    print("=" * 72)

    ref_names = {p.name for p in REFERENCES.glob("*.md")}
    prompts = {p.stem: p.read_text(encoding="utf-8") for p in sorted(PROMPTS.glob("*.md"))}

    # 收集所有引用
    graph: dict[str, list[Ref]] = defaultdict(list)
    refs: list[Ref] = []
    for role, text in prompts.items():
        for name, timing in extract_refs(text):
            refs.append(Ref(role, name, timing))
            graph[name].append(Ref(role, name, timing))

    print(f"\n角色 {len(prompts)} 份，references {len(ref_names)} 份，"
          f"引用边 {len(refs)} 条\n")

    # ── A. 孤儿检测 ──
    print("── A. 孤儿检测（无人引用的死文件）──")
    orphans = sorted(ref_names - set(graph))
    if orphans:
        for o in orphans:
            print(f"  ✗ 死文件：{o} —— 没有任何角色在提示词里提到它")
        print(f"\n  → {len(orphans)} 份死文件。它们在产出时不会被读到。")
    else:
        print("  ✓ 无死文件，每份 references 都至少被一个角色引用")

    # ── B. 读取时机缺失 ──
    print("\n── B. 读取时机声明（只写「详见」不说何时读的引用）──")
    no_timing = [r for r in refs if not r.has_timing]
    if no_timing:
        by_src = defaultdict(list)
        for r in no_timing:
            by_src[r.src].append(r.dst)
        for src in sorted(by_src):
            print(f"  · {src}.md → {', '.join(sorted(by_src[src]))}")
        print(f"\n  → {len(no_timing)} 处引用未声明读取时机（模型可能跳过）")
    else:
        print("  ✓ 所有引用都带了触发时机")

    # ── C. 职责越界（可疑，需人判）──
    print("\n── C. 职责越界可疑项（脚本只标记，判断在人）──")
    hits = []
    for r in refs:
        key = (r.src, r.dst)
        if key in SUSPECT_CROSS_DOMAIN:
            hits.append((r.src, r.dst, SUSPECT_CROSS_DOMAIN[key]))
    if hits:
        for src, dst, why in sorted(set(hits)):
            print(f"  ⚠ {src} → {dst}")
            print(f"      {why}")
    else:
        print("  ✓ 无可疑越界")

    # ── D. references 之间的呼应 ──
    print("\n── D. references 互引闭环 ──")
    ref_graph: dict[str, set[str]] = defaultdict(set)
    for p in sorted(REFERENCES.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"`references/([A-Za-z0-9_.-]+\.md)`", text):
            if m.group(1) in ref_names:
                ref_graph[p.name].add(m.group(1))
    # 悬空引用
    dangling = []
    for p in sorted(REFERENCES.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"`references/([A-Za-z0-9_.-]+\.md)`", text):
            if m.group(1) not in ref_names:
                dangling.append(f"{p.name} → {m.group(1)}")
    if dangling:
        for d in sorted(set(dangling)):
            print(f"  ✗ 悬空引用：{d}")
    else:
        print("  ✓ references 之间无悬空引用")

    # 入度为 0 的 references（除 prompt 外无人引用 = 孤岛）
    print("\n── E. references 孤岛（prompt 引用了，但同族无人引用）──")
    islands = []
    for name in sorted(ref_names):
        if name not in graph:
            continue  # 已在 A 报过
        inbound = [k for k, v in ref_graph.items() if name in v]
        if not inbound:
            islands.append(name)
    if islands:
        for i in islands:
            print(f"  · {i} —— prompt 引用了它，但其他 references 都不引用它")
        print("  （不一定是问题：叶子文件本就不必被引；但值得确认定位是否清晰）")
    else:
        print("  ✓ 每份 references 都至少被另一份 references 引用")

    # ── 汇总 ──
    print("\n" + "=" * 72)
    problems = len(orphans) + len(no_timing) + len(hits) + len(set(dangling))
    print(f"死文件 {len(orphans)} · 缺时机 {len(no_timing)} · "
          f"越界可疑 {len(set((a, b) for a, b, _ in hits))} · "
          f"悬空 {len(set(dangling))} · 孤岛 {len(islands)}")
    if problems:
        print("→ A 项（死文件）是硬结论；B/C/D 是线索，须人工判断，**不作为门禁**。")
        return 0 if not orphans else 1
    print("→ 信息架构完整（死文件 0）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
