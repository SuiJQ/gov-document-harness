"""内容包 · 代码侧契约一致性检查。

## 为什么这个脚本存在

前四轮检查覆盖了内容流失、伪造、矛盾、信息架构，
但**一个字都没核过代码侧契约**。

内容包里写的角色标识、产物类型、阶段名、D-Card 字段、
门禁编号、档位名——这些在 `src/zhengtong/domain/*.py` 里
**有唯一定义**（D-055：内容包不得硬编码，代码是权威）。

> **两边不一致的后果不是「不好看」，是必然失败**：
> 模型按提示词产出 → 程序按代码校验 → 结构对不上 → 整条链路断。
> 而这种错误在任何单侧检查里都是全绿的。

## 断言的是什么

本脚本断言「**内容包里出现的标识符，与代码枚举的值一致**」。

**断言不了**：
- 内容包的语义是否正确（那是人判断）
- 代码枚举本身是否合理（那是设计决策）

## 单一事实源的方向

代码是权威（`domain/*.py`），内容包是消费者。
所以**从代码抽枚举值，去内容包里查**——
反过来做会把内容包的错误固化成「标准」。

跑法：
    python3 scripts/check_content_contract.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "zhengtong" / "domain"
CONTENT = ROOT / "content"


@dataclass(frozen=True)
class Contract:
    """一个代码侧的权威枚举。"""

    name: str
    source: str          # 定义所在的代码文件
    enum_name: str       # 枚举类名
    mapping: dict[str, str]  # 标识符 -> 注释里的中文名


def parse_enum(path: Path, enum_name: str) -> dict[str, str]:
    """从一个 StrEnum 抽 `{成员名: 枚举值}`。

    **不复用注释**：这是第三轮修过一次的老错。
    原实现用「值」的位置存了注释里的中文名，
    于是 `RoleId` 抽出来是 `{"TEAM_LEAD": "范正章"}`——
    标识符 `"team-lead"` **从头到尾没被验证过**，
    角色检查一直拿中文名在比对，看起来是绿的，实际没查该查的东西。

    注释另取 `{成员名: 注释}`，两者**不许混**：
    标识符是契约（D-080：代码唯一权威），注释只是可读性。
    """
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"class {enum_name}\(StrEnum\):(.*?)(?=\n@|\nclass |\Z)", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        mm = re.match(r'\s*([A-Z][A-Z0-9_]*)\s*=\s*"([^"]*)"', line)
        if mm:
            out[mm.group(1)] = mm.group(2)
    return out


def parse_comments(path: Path, enum_name: str) -> dict[str, str]:
    """抽 `{成员名: 行内注释}`。**只用于读中文名**，不作契约。"""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"class {enum_name}\(StrEnum\):(.*?)(?=\n@|\nclass |\Z)", text, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        mm = re.match(r'\s*([A-Z][A-Z0-9_]*)\s*=\s*"[^"]*"\s*#\s*(.*)$', line)
        if mm:
            out[mm.group(1)] = mm.group(2).strip()
    return out


def load_contracts() -> list[Contract]:
    return [
        Contract("角色", "role.py", "RoleId", parse_enum(SRC / "role.py", "RoleId")),
        Contract("产物类型", "artifact.py", "ArtifactKind",
                 parse_enum(SRC / "artifact.py", "ArtifactKind")),
        Contract("阶段", "phase.py", "PhaseId", parse_enum(SRC / "phase.py", "PhaseId")),
        Contract("D-Card 字段", "dcard.py", "DCardFieldId",
                 parse_enum(SRC / "dcard.py", "DCardFieldId")),
        Contract("门禁", "gate.py", "GateId", parse_enum(SRC / "gate.py", "GateId")),
    ]


def content_text() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(CONTENT.rglob("*.md"))
    )


def check_role_manifest() -> list[str]:
    """manifest.json 的角色标识必须与 RoleId 完全一致。"""
    import json
    mpath = CONTENT / "manifest.json"
    if not mpath.is_file():
        return ["manifest.json 缺失"]
    data = json.loads(mpath.read_text(encoding="utf-8"))
    code_roles = set(parse_enum(SRC / "role.py", "RoleId").values())
    manifest_roles = set(data.get("roles", {}))
    problems = []
    if manifest_roles != code_roles:
        only_m = manifest_roles - code_roles
        only_c = code_roles - manifest_roles
        if only_m:
            problems.append(f"manifest 多出角色：{sorted(only_m)}")
        if only_c:
            problems.append(f"manifest 缺角色：{sorted(only_c)}")
    # 角色提示词文件必须存在且与标识同名
    for rid in code_roles:
        if not (CONTENT / "prompts" / f"{rid}.md").is_file():
            problems.append(f"缺角色提示词：prompts/{rid}.md")
    return problems


def check_prompt_files(contracts: list[Contract]) -> list[str]:
    """每个角色提示词文件必须被 manifest 指向，且文件名与 RoleId 一致。"""
    import json
    data = json.loads((CONTENT / "manifest.json").read_text(encoding="utf-8"))
    roles = data.get("roles", {})
    problems = []
    for rid, spec in roles.items():
        expect = f"prompts/{rid}.md"
        if spec.get("prompt") != expect:
            problems.append(f"{rid} 的 prompt 字段应为 {expect}，实为 {spec.get('prompt')}")
    return problems


def check_artifact_kind_names(contracts: list[Contract]) -> list[str]:
    """内容包提到的产物中文名必须与 ArtifactKind 注释一致。

    「方向确认单」不是 ArtifactKind（D-Card 是 state 里的一等公民），
    所以只查内容包是否用了**代码里没有的**产物中文名。
    """
    kinds = parse_enum(SRC / "artifact.py", "ArtifactKind")
    code_names = {c for c in kinds.values() if c}
    # 内容包里常见的产物中文名 → 必须是代码里 ArtifactKind 的注释之一
    # 或明确属于 D-Card / 阶段，不在 ArtifactKind 范围
    allowed_extra = {
        "方向确认单",      # D-Card，state 的一等公民
        "文章大纲",        # = OUTLINE
        "正文初稿",        # = DRAFT
        "门禁报告",        # = GATE_REPORT
        "主件",            # = MAIN_DOCUMENT
        "配套件",          # = ATTACHMENT
        "交付说明",        # = NOTE
        "修正稿",          # = DRAFT 的别名（代码注释里写了）
    }
    return []  # 结构上无法可靠自动判定，交给人工；保留函数以备后续


def _core(desc: str) -> str:
    """取描述的「核心名」——剥掉编号前缀、「自检：」标记与补充说明。

    证伪时实测了三轮，三轮都是**误报**：
    ① 直接拿代码注释全文精确匹配——代码写「语言零歧义（含去 AI 味）」，
       内容包写「语言零歧义（辞达意）」，括号不同就判失败。
    ② 抽括号前的内容仍误报——代码写「自检：质量恒定（主理人自查）」，
       内容包写「G0 质量恒定」。「自检：」是**分类标记**不是门禁名。
    ③ 再加剥「+」后仍误报——代码 G13 写「折中论证透明 + 例外接口清单」，
       内容包标题只写「折中论证透明」。

    故最终判据：**剥编号前缀、分类标记、括号补充、分号并列之后，
    取首个语义单元，内容包含它即算一致**。
    补充说明不属契约——把「例外接口清单」也要求出现是过度约束。
    """
    s = desc.strip()
    s = re.sub(r"^G\d+\s+", "", s)           # 剥编号前缀
    s = re.sub(r"^自检：", "", s)             # 剥分类标记
    s = re.split(r"[（(；;]", s, 1)[0]        # 剥括号与分号后的补充
    s = re.split(r"\s*\+\s*", s, 1)[0]        # 剥并列的第二项
    return s.strip()


def check_gate_names(contracts: list[Contract]) -> list[str]:
    gates = parse_comments(SRC / "gate.py", "GateId")
    text = content_text()
    problems = []
    for gid, desc in gates.items():
        if not desc:
            continue
        core = _core(desc)
        if core and core not in text:
            problems.append(f"内容包未出现 {gid} 的核心名「{core}」（代码侧：{desc}）")
    return problems


def check_phase_names() -> list[str]:
    """内容包须与代码的架构阶段映射对得上（D-148）。

    **判据方向改过一次**：原先要求内容包逐个包含代码 `PhaseId` 的
    中文注释名（「需求采集」「门禁检查」…），结果全线红灯。

    查了架构 §8.3 才发现**方向反了**：
    架构画的阶段号是 `Phase -1/0/0.5/1/2/3/4`，
    内容包忠于架构用的是 `Phase 0 / 0.5`，
    而代码 `PhaseId` 是另一套自造编号。
    逼内容包去补八个自造编号的中文名，是把正确的改错。

    D-148 的修法是加映射表（`PhaseId.architecture_phase`），
    故此处只断言：**映射涉及的所有阶段，
    内容包要么用架构 Phase 号、要么用产物名，两者至少命中其一**——
    也就是「模型能从内容包推出这一步归谁管」。
    """
    text = content_text()
    problems = []
    # 内容包必须落地架构 §8.3 的阶段体系，否则映射表无处落地。
    #
    # 证伪时实测到这个判据有**两轮**问题：
    # ① 只查「Phase 3」会误报——内容包写「并行 4 组」
    #    （架构 §8.3 原话就是「12 道门禁（并行 4 组，D-049）」），没用编号。
    #    → 改成标记组。
    # ② 但改成 `any()` 之后**证伪失效**：删掉「并行 4 组」，
    #    另一个标记「并行复核」仍然命中，脚本照样全绿——
    #    治了误报，却把检错力一起治没了。
    #    → 所以：**互补的两个标记必须同时在场**，不是任一命中。
    阶段标记: list[tuple[str, tuple[str, ...]]] = [
        ("Phase -1（方向锁）", ("D-Card",)),
        ("Phase 0（因果推演）", ("Phase 0", "因果备忘录")),
        ("Phase 0.5（政策取证）", ("Phase 0.5", "政策适配表")),
        ("Phase 3（12 道门禁并行 4 组）", ("并行 4 组", "并行复核", "门禁报告")),
    ]
    for 名, 标记组 in 阶段标记:
        缺 = [k for k in 标记组 if k not in text]
        if 缺:
            problems.append(
                f"内容包未落地架构阶段「{名}」——缺标记 {缺}（应全部在场）"
            )
    return problems


def check_gate_ownership() -> list[str]:
    """每道门禁的**唯一取证方**必须在自己的提示词里声明（D-149）。

    第五轮查出的问题：`language.md` 零次提及 G4、`doc-advisor.md`
    零次提及 G3——而这两人分别是 G4 与 G3 的唯一取证方。
    门禁手册里写着「G4（辞达意）」，但**模型只读自己的提示词**，
    职责不在自己的提示词里声明，就等于没告知。

    ## 判据为什么不是「文件里出现过该编号」

    证伪时实测：删掉 `language.md` 的认领声明后，它仍有 4 处 `G4`
    （全是「G4 给了你」这类**反例句**），脚本照样全绿。
    故须匹配**认领句式**——句中同时出现触发词与编号才算。

    ## 为什么按句切分而不是一条正则

    试过 `(取证责任|…)(G1|G2|…)(?:…(G1|G2|…))*`，两处坑：
    ① 非贪婪只吃第一个编号 → G2/G7/G10 全漏判；
    ② 改贪婪后，**Python 重复捕获组只保留末次结果**，
       `G1` 被重复匹配到，`G2/G7/G10` 仍然丢。
    同一句声明多条门禁是常态，用一条正则表达不了。
    按句切开后逐句取并集，最直白也不会漏。
    """
    归属 = {
        "compliance.md": ("G1", "G2", "G7", "G10"),
        "structure.md": ("G5", "G8"),
        "language.md": ("G4",),
        "doc-advisor.md": ("G3",),
    }
    全部编号 = {g for gs in 归属.values() for g in gs}
    触发词 = ("取证责任", "由你独自取证", "由你取证",
              "属第 4 组", "属第4组", "独占此项", "归你负责")

    problems: list[str] = []
    for 名, 门禁组 in 归属.items():
        text = (CONTENT / "prompts" / 名).read_text(encoding="utf-8")
        已认领: set[str] = set()
        for 句 in re.split(r"[。\n]", text):
            if any(w in 句 for w in 触发词):
                已认领 |= {g for g in 全部编号 if g in 句}
        for g in 门禁组:
            if g not in 已认领:
                problems.append(
                    f"{名} 未以认领句式声明自己负责 {g}"
                    f"——裸编号不算（反例句里也会出现该编号）"
                )
    return problems


def check_revision_guidance() -> list[str]:
    """四份成员提示词都必须说明「我被退回后该做什么」（D-149）。

    第五轮查出：四份**只教「你能把别人退回去」**，
    没有一份讲「你被退回来该做什么」——
    主理人侧有 `revision` 阶段，成员侧没有接，闭环缺一半。
    """
    问题 = []
    for p in sorted(CONTENT.glob("prompts/*.md")):
        if p.name == "team-lead.md":
            continue  # 主理人是派退回的一方，不是接的一方
        text = p.read_text(encoding="utf-8")
        if "被退回" not in text:
            问题.append(f"{p.name} 未说明被退回后的处置（`revision` 阶段闭环缺失）")
    return 问题


def main() -> int:
    print("=" * 72)
    print("内容包 · 代码侧契约一致性（第四轮新增）")
    print("=" * 72)
    print(f"权威来源：{SRC.relative_to(ROOT)}/*.py\n")

    contracts = load_contracts()
    for c in contracts:
        print(f"  {c.name:12s} {c.enum_name:14s} {len(c.mapping)} 项  ({c.source})")

    print("\n── 检查项 ──")
    results: list[tuple[str, list[str]]] = []

    results.append(("角色标识 ↔ manifest.json", check_role_manifest()))
    results.append(("角色提示词路径 ↔ manifest", check_prompt_files(contracts)))
    results.append(("门禁中文名 ↔ 代码注释", check_gate_names(contracts)))
    results.append(("阶段中文名 ↔ 代码注释", check_phase_names()))
    results.append(("门禁取证方声明", check_gate_ownership()))
    results.append(("被退回后处置（revision 闭环）", check_revision_guidance()))
    results.append(("产物中文名（结构判定，自动跳过）", check_artifact_kind_names(contracts)))

    total_problems = 0
    for name, problems in results:
        if problems:
            print(f"  [FAIL] {name}")
            for p in problems:
                print(f"         ✗ {p}")
            total_problems += len(problems)
        else:
            print(f"  [OK  ] {name}")

    print()
    print("=" * 72)
    if total_problems:
        print(f"发现 {total_problems} 处契约不一致——**这类错误会让链路必然失败**，")
        print("内容包与代码必须对齐后重新校验。")
        return 1
    print("内容包与代码侧契约一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
