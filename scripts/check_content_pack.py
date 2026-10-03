"""内容包约束核对脚本。

**为什么要有这个脚本**

第 12 步的判据是「原始提示词里的每一条实质约束，在新版本里都能找到对应」，
而不是「文件数量对不对」。这个判据**靠人读是读不完的**——
五份提示词 + 八份 references，交叉引用的约束有几十条。

更重要的是：前几批审计我反复栽在同一类错误上——
**验证脚本出错，却读成「发现了 bug」**。
所以这个脚本本身也要有自检，且**每条断言都写清它到底在断言什么**。

跑法：
    python3 scripts/check_content_pack.py
退出码 0 = 全部通过；非 0 = 有约束丢失。

新增约束时：先在 CONTENT_CONSTRAINTS 加一条，再跑。
**不要绕过脚本手工确认**——那正是本项目第 12 步要防的失误。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
PROMPTS = CONTENT / "prompts"
REFERENCES = CONTENT / "references"

#: 五个角色的内部标识（D-080：代码与内容包共用的英文标识）
ROLE_IDS = ("team-lead", "structure", "language", "compliance", "doc-advisor")

#: D1-D9 九个字段（D-Card 是全流程唯一事实源，一个都不能少）
DCARD_FIELDS = tuple(f"D{i}" for i in range(1, 10))

#: G0-G13 共 14 项编号，其中 G0/G6 为自检
GATE_IDS = tuple(f"G{i}" for i in range(0, 14))

#: 正式门禁 12 道（不含 G0/G6）
FORMAL_GATES = tuple(g for g in GATE_IDS if g not in ("G0", "G6"))

#: E1-E14 十四条红线（D-036/D-037 授权改写但不得丢失）
RED_LINES = tuple(f"E{i}" for i in range(1, 15))

#: 三个档位（D-029）
TIER_NAMES = ("咨询", "成文 Flash", "成文 Pro")


@dataclass(frozen=True)
class Check:
    """一条核对结果。"""

    name: str
    passed: bool
    detail: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_text() -> str:
    """内容包全部文本，便于跨文件查约束。"""
    parts = []
    for p in sorted(CONTENT.rglob("*.md")):
        parts.append(_read(p))
    return "\n".join(parts)


def check_file_presence() -> list[Check]:
    """文件齐备性：5 份角色提示词 + 关键 references。"""
    checks = []
    for rid in ROLE_IDS:
        p = PROMPTS / f"{rid}.md"
        checks.append(Check(f"提示词存在：{rid}", p.is_file(), str(p.relative_to(ROOT))))
    required_refs = (
        "gate-manual.md",
        "error-memory.md",
        "compliance-rules.md",
        "doc-type-patterns.md",
        "admin-ordering-rules.md",
        "deai-rules.md",
        "writing-techniques.md",
        "gbt9704-format.md",
        "punctuation.md",
        "stats-baseline.md",
        "language-dna.md",
        "param-card.md",
        "title-techniques.md",
        "cognition-dna.md",
        "cognition-baseline.md",
        "doc-templates.md",
    )
    for name in required_refs:
        p = REFERENCES / name
        checks.append(Check(f"references 存在：{name}", p.is_file(), str(p.relative_to(ROOT))))
    return checks


def check_red_lines() -> list[Check]:
    """E1-E14 十四条红线，每条都要在 error-memory.md 有独立小节。

    断言的是**结构**（`## E<n> ` 开头的小节标题），
    不是「E1 这个字符串出现过」——
    后者会被正文里的「E1 红线」等引用误判为通过。
    """
    text = _read(REFERENCES / "error-memory.md")
    checks = []
    for e in RED_LINES:
        # 小节标题形如 "## E1 日期格式错误"
        has_section = re.search(rf"^## {e} \S", text, re.MULTILINE) is not None
        # 每条红线须有四要素：错误模式 / 正确形式 / 抑制触发 / 判定与退回责任方
        section_match = re.search(
            rf"^## {e} \S.*?(?=^## E\d+ \S|^## 维护说明)", text, re.MULTILINE | re.DOTALL
        )
        section = section_match.group(0) if section_match else ""
        has_pattern = "**错误模式**" in section
        has_correct = "**正确形式**" in section
        has_trigger = "**抑制触发**" in section
        has_owner = "判定与退回责任方" in section
        ok = has_section and has_pattern and has_correct and has_trigger and has_owner
        missing = [
            n
            for n, v in (
                ("小节", has_section),
                ("错误模式", has_pattern),
                ("正确形式", has_correct),
                ("抑制触发", has_trigger),
                ("责任方", has_owner),
            )
            if not v
        ]
        checks.append(
            Check(
                f"红线 {e} 四要素齐备",
                ok,
                "缺：" + "、".join(missing) if missing else "错误模式/正确形式/抑制触发/责任方",
            )
        )
    return checks


def check_gates() -> list[Check]:
    """G0-G13 编号齐备，且 12 道正式门禁在手册中各有判读段。"""
    text = _read(REFERENCES / "gate-manual.md")
    lead = _read(PROMPTS / "team-lead.md")
    checks = []

    for g in GATE_IDS:
        # 手册中须有 `## G<n> ` 小节（G0/G6 在「降级自查项」一节）
        if g in ("G0", "G6"):
            ok = g in text and "降级自查项" in text
            detail = "自检项，降级不占门禁序号"
        else:
            has_section = re.search(rf"^## {g} \S", text, re.MULTILINE) is not None
            in_lead = g in lead
            ok = has_section and in_lead
            detail = (
                "手册小节 + 主理人提示词引用"
                if ok
                else (f"手册小节={has_section} 主理人引用={in_lead}")
            )
        checks.append(Check(f"门禁 {g} 有判据", ok, detail))

    # 正式门禁必须是 12 道
    checks.append(
        Check(
            "正式门禁为 12 道",
            len(FORMAL_GATES) == 12,
            f"G0-G13 共 14 项编号，去 G0/G6 后 {len(FORMAL_GATES)} 道",
        )
    )
    return checks


def check_dcard() -> list[Check]:
    """D-Card 是全流程唯一事实源：D1-D9 九个字段须在主理人提示词各有判读。"""
    lead = _read(PROMPTS / "team-lead.md")
    checks = []
    for d in DCARD_FIELDS:
        # 断言字段在方向锁表格里有独立行：`| **D1 主件** | ...`
        has_row = re.search(rf"\*\*{d} ", lead) is not None
        checks.append(Check(f"D-Card 字段 {d} 有判读", has_row, "方向锁表中须有独立字段行"))
    # 关键纪律：F/S/M 分级、逃生选项、D3 硬确认
    for token, why in (
        ("**F**", "事实分级 F（缺失致命）"),
        ("**S**", "事实分级 S（瑕疵）"),
        ("**M**", "事实分级 M（无妨）"),
        ("逃生选项", "用户逃生选项"),
        ("硬确认项", "D3 硬确认"),
        ("唯一停车项", "D1 唯一停车项"),
    ):
        checks.append(Check(f"方向锁纪律：{why}", token in lead, f"须含 {token}"))
    return checks


def check_tiers() -> list[Check]:
    """三档命名须与代码侧 tier.py 一致（中文显示名）。"""
    lead = _read(PROMPTS / "team-lead.md")
    return [
        Check(f"档位名一致：{t}", t in lead, "须与 domain/tier.py 的 display_name 一致")
        for t in TIER_NAMES
    ]


def check_fact_tristate() -> list[Check]:
    """事实三态 + 推定词黑名单。"""
    text = _all_text()
    checks = [
        Check(
            "事实三态齐备",
            all(s in text for s in ("[已核·来源]", "[假设·待确认]", "[未知]")),
            "已核·来源 / 假设·待确认 / 未知",
        ),
        Check(
            "推定词黑名单",
            all(w in text for w in ("预计", "按惯例", "大概", "应该是")),
            "预计/按惯例/通常/大概/应该是/据了解",
        ),
        Check(
            "依据三态齐备（R9c）",
            all(s in text for s in ("[已核对原文]", "[未核验]", "[待向")),
            "已核对原文 / 未核验 / 待向××确认",
        ),
    ]
    return checks


def check_dual_axis_ledger() -> list[Check]:
    """双轴台账：数据状态台账（可靠度态 × 时效态）须五列齐备。"""
    text = _all_text()
    cols = ("对象·属性", "可靠度态", "时效态", "时间戳", "声明域")
    missing = [c for c in cols if c not in text]
    return [
        Check(
            "数据状态双轴台账五列齐备",
            not missing,
            "缺：" + "、".join(missing) if missing else " / ".join(cols),
        ),
        Check(
            "时效态三值齐备",
            all(v in text for v in ("定值", "截至值", "待核")),
            "定值 / 截至值 / 待核",
        ),
    ]


def check_no_proprietary_names() -> list[Check]:
    """**专有名词清零**（用户本轮明确要求）。

    扫的是**来源系统的专有名词**，不含任何本项目自有术语。
    这是两层过滤：
    1. 正则列出的具体字符串
    2. 内部标识泄漏（如 `gov-document-team-team-lead`）
    """
    # 来源系统的专有名词与工具名
    banned_literals = (
        "WorkBuddy",
        "Workbuddy",
        "workbuddy",
        "SuiJQ",
        "gov-document-team",
        "CodeBuddy",
        "codebuddy",
        "claude-code",
        "skill-creator",
        "official-document-writing",
        "gongwen-deai",
        "lieflat",
        "register_expert",
        "package_expert",
        "lint.py",
        "hard_constraint_check.py",
        "evidence_anchor_check.py",
        "consistency_check.py",
        "strip_meta.py",
        "TeamCreate",
        "SendMessage",
        "spawn",
        "subagent_type",
        "Agent ID",
        "maxTurns",
        "mrrobot",
    )
    checks = []
    for p in sorted(CONTENT.rglob("*.md")):
        text = _read(p)
        hits = [b for b in banned_literals if b in text]
        checks.append(
            Check(
                f"专有名词清零：{p.relative_to(ROOT)}",
                not hits,
                "命中：" + "、".join(hits) if hits else "无",
            )
        )
    # 原始 Agent 标识不得作为角色引用出现
    for p in sorted(PROMPTS.glob("*.md")):
        text = _read(p)
        old_ids = (
            "outline-structure-expert",
            "language-polishing-editor",
            "compliance-validator",
            "document-type-advisor",
            "gov-document-team-team-lead",
        )
        hits = [o for o in old_ids if o in text]
        checks.append(
            Check(
                f"角色标识用内部标识：{p.relative_to(ROOT)}",
                not hits,
                "命中旧标识：" + "、".join(hits) if hits else "无",
            )
        )
    return checks


def check_cross_refs() -> list[Check]:
    """交叉引用的 references 文件必须真实存在（防悬空引用）。"""
    checks = []
    ref_names = {p.name for p in REFERENCES.glob("*.md")}
    pattern = re.compile(r"`references/([A-Za-z0-9_.-]+\.md)`")
    for p in sorted(CONTENT.rglob("*.md")):
        text = _read(p)
        for ref in pattern.findall(text):
            exists = ref in ref_names
            checks.append(
                Check(
                    f"引用可达：{p.relative_to(ROOT)} → {ref}",
                    exists,
                    "存在" if exists else "**悬空引用**",
                )
            )
    return checks


def check_no_external_tools() -> list[Check]:
    """D-055：去 AI 味规则在内容包，**不是可选模块**，无不可用分支。

    断言的是「不存在『去 AI 味工具不可用时改用别的』这类分支」——
    源材料里正有此句（原文：「该 skill 不可用时无条件回落至本条内嵌规则」），
    改写版必须整句消失，改为无条件执行。

    **只扫去 AI 味相关的文件**：
    「不可用时」在 compliance-rules.md 的「环境兜底」语境里是**必需内容**
    （政策检索不可达时的 R9c 兜底），不是工具降级分支。
    全仓扫该词会把这条必需纪律误判为缺陷——这正是本脚本自己踩过的坑。
    """
    scoped = (
        PROMPTS / "language.md",
        REFERENCES / "deai-rules.md",
    )
    forbidden = (
        "该 skill 不可用时",
        "skill 不可用",
        "回落至本条内嵌规则",
        "预加载 skill",
        "按其规则执行",
        "技能不可用",
    )
    hits = []
    for p in scoped:
        if not p.is_file():
            continue
        text = _read(p)
        hits.extend(f"{p.name}:{f}" for f in forbidden if f in text)

    return [
        Check(
            "去 AI 味无条件执行（D-055）",
            not hits,
            "命中降级分支：" + "、".join(hits) if hits else "去 AI 味相关文件内无降级分支",
        ),
        Check(
            "去 AI 味规则文件被引用",
            "references/deai-rules.md" in _read(PROMPTS / "language.md"),
            "辞达意提示词须显式引用",
        ),
        Check(
            "政策检索兜底纪律未被误删（D-055 收窄后须仍在）",
            "环境兜底" in _read(REFERENCES / "compliance-rules.md"),
            "政策检索不可达时的 R9c 兜底是必需内容，不得被当成降级分支删掉",
        ),
    ]


def check_output_format() -> list[Check]:
    """产出格式：TXT 优先、UTF-8 默认、只落 data/（D-044 / D-008）。"""
    lead = _read(PROMPTS / "team-lead.md")
    return [
        Check("成稿为 TXT", ".txt" in lead, "成稿文件名后缀"),
        Check("只落 data/ 目录", "data/<run-id>/" in lead, "不得写系统临时目录"),
        Check("禁写系统临时目录", "禁写系统临时目录" in lead, "须显式禁止"),
        Check("占位符导出前扫描", "扫描残留占位符" in lead, "D-034 最高数据纪律"),
    ]


def check_manifest() -> list[Check]:
    """manifest.json：名称、版本、兼容区间、内容哈希、角色映射（D-080）。"""
    mpath = CONTENT / "manifest.json"
    if not mpath.is_file():
        return [Check("manifest.json 存在", False, "缺文件")]
    data = json.loads(_read(mpath))
    checks = [Check("manifest.json 存在", True, str(mpath.relative_to(ROOT)))]

    for field in ("name", "version", "requires_zhengtong", "files", "roles"):
        checks.append(Check(f"manifest 字段 {field}", field in data, f"须含 {field}"))

    files = data.get("files", {})
    # 哈希覆盖全部内容文件
    on_disk = {str(p.relative_to(CONTENT)).replace("\\", "/") for p in CONTENT.rglob("*.md")}
    listed = set(files)
    missing = on_disk - listed
    extra = listed - on_disk
    checks.append(
        Check(
            "manifest 哈希覆盖全部内容文件",
            not missing and not extra,
            f"未列：{sorted(missing)} 多列：{sorted(extra)}"
            if (missing or extra)
            else f"{len(on_disk)} 个文件全部登记",
        )
    )
    # 哈希格式
    bad = [k for k, v in files.items() if not str(v).startswith("sha256:")]
    checks.append(
        Check(
            "manifest 哈希格式为 sha256:",
            not bad,
            f"格式错：{bad}" if bad else f"{len(files)} 条均为 sha256: 前缀",
        )
    )
    # 角色映射（D-080）：中文显示名 + 内部标识
    roles = data.get("roles", {})
    checks.append(
        Check(
            "角色映射五项齐全",
            set(roles) == set(ROLE_IDS),
            f"实有 {sorted(roles)}",
        )
    )
    # 不设授权字段（D-087：纯自用不分发）
    checks.append(
        Check(
            "manifest 不含 license 字段（D-087）",
            "license" not in data,
            "纯自用不分发，不加授权字段",
        )
    )
    return checks


def check_g1_grouping() -> list[Check]:
    """G1 的 18 项分组口径：版头 5 + 主体 9 + 版记 3 + 版式 1。

    **页码不属版记**（用户裁决，与 GB/T 9704—2012 一致）——
    版记只含分隔线、抄送机关、印发机关和印发日期。

    为什么单独断言：这类口径在多文件复述，
    实审时已出现过 gate-manual / compliance / error-memory 三处不一致。
    脚本只查「有无说错」，**查不出是否漏读某处**，故断言给全仓扫描。
    """
    files = {
        "gate-manual": _read(REFERENCES / "gate-manual.md"),
        "compliance": _read(PROMPTS / "compliance.md"),
        "error-memory": _read(REFERENCES / "error-memory.md"),
    }
    checks = [
        Check(
            "gate-manual 版记分组为 3",
            "**版记（3）**" in files["gate-manual"],
            "版头 5 + 主体 9 + 版记 3 + 版式 1 = 18",
        ),
        Check(
            "gate-manual 页码明示不属版记",
            "不属版记" in files["gate-manual"],
            "须写明页码是版式要素",
        ),
        Check(
            "compliance 版记仅列三项",
            "版记仅此三项" in files["compliance"],
            "G1 概览表须与 gate-manual 同口径",
        ),
        Check(
            "error-memory E5 不把页码算进版记",
            "页码属版式要素，不在版记之内" in files["error-memory"],
            "红线库是权威口径，不得沿用旧分组",
        ),
    ]
    # 反向扫描：把「页码」当**版记的构成要素**并列列举的地方都算错。
    # 注意排除两类合法表述：
    #   ① 显式否定式（含「不属」「不在」「独立」「版式要素」）
    #   ② 「版记页」——指整页版面，不是版记要素
    #      （证伪时实测到误报：「版记页前有空白页」说的是整页）
    bad: list[str] = []
    for p in sorted(CONTENT.rglob("*.md")):
        for line in _read(p).splitlines():
            if "版记" not in line or "页码" not in line:
                continue
            if any(k in line for k in ("不属", "不在", "独立", "版式要素")):
                continue
            if "版记页" in line:  # 「版记页」＝整页版面，非要素
                continue
            bad.append(f"{p.relative_to(ROOT)}: {line.strip()[:40]}")
    checks.append(
        Check(
            "全仓无「版记含页码」的要素并列",
            not bad,
            "；".join(bad) if bad else "无",
        )
    )
    return checks


def check_encoding() -> list[Check]:
    """编码完整性：不得有替换字符（U+FFFD）。

    **为什么单独查**：内容包是**要被模型当正文读**的文本，
    一个 U+FFFD 会被模型当成正文里的一个怪字符照读或照抄。

    编写内容包时用 Python 写中文长文本，
    实测**两次**在字符串里混进 U+FFFD（编辑时字符被截断）——
    肉眼在长段落里很难发现，grep 一下就出来了。
    """
    bad: list[str] = []
    for p in sorted(CONTENT.rglob("*.md")):
        text = _read(p)
        for i, line in enumerate(text.splitlines(), 1):
            if "\ufffd" in line:
                bad.append(f"{p.relative_to(ROOT)}:{i}")
    return [
        Check(
            "全仓无替换字符 U+FFFD",
            not bad,
            f"{len(bad)} 处：" + "、".join(bad[:5]) if bad else "无",
        )
    ]


def main() -> int:
    groups = [
        ("文件齐备", check_file_presence()),
        ("编码完整性", check_encoding()),
        ("红线 E1-E14", check_red_lines()),
        ("门禁 G0-G13", check_gates()),
        ("G1 分组口径", check_g1_grouping()),
        ("D-Card 字段", check_dcard()),
        ("档位一致性", check_tiers()),
        ("事实三态", check_fact_tristate()),
        ("双轴台账", check_dual_axis_ledger()),
        ("专有名词清零", check_no_proprietary_names()),
        ("交叉引用可达", check_cross_refs()),
        ("D-055 无降级", check_no_external_tools()),
        ("产出格式", check_output_format()),
        ("manifest", check_manifest()),
    ]

    total = 0
    failed = 0
    for gname, checks in groups:
        bad = [c for c in checks if not c.passed]
        total += len(checks)
        failed += len(bad)
        mark = "OK  " if not bad else "FAIL"
        print(f"[{mark}] {gname}（{len(checks) - len(bad)}/{len(checks)}）")
        for c in bad:
            print(f"       ✗ {c.name} —— {c.detail}")

    print()
    print(f"合计 {total} 项，通过 {total - failed}，失败 {failed}")
    if failed:
        print("→ 有约束丢失，改完重跑。")
        return 1
    print("→ 内容包约束全部齐备。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
