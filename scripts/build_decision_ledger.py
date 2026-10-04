#!/usr/bin/env python3
"""决策台账对照表（只读）。

## 它回答什么

对 `DECISIONS.md` 的每一条决策，回答三个可机械判定的问题：

1. **有没有被引用**——代码、判据、内容包、文档各引用几次
2. **最后一次被触及是什么时候**——哪次提交增删过这个决策号
3. **正文有多长**——搬走它要付多少编辑成本

## 为什么不判定「已实现 / 未实现」

**实现状态无法机械判定。** 「这条决策的代码还在不在」要读代码才知道，
291 条逐条读就是一次全面审查——那是第二轮全面检查的活。

所以本脚本只出**事实**，判断留给人和判据。
凭印象判定的下场这轮吃过两次：
把 D-063 说成「三驱动同源」（原文实为「内核纯同步状态机」）、
把 D-032 说成流式那条（实为「支持格式」，流式是 D-026）。

## 「末次触及」怎么算

一次 `git log -p` 走完全部历史，扫每个提交的增删行，
记录每个决策号**首次出现**在哪个提交里。
`git log` 输出最新在前，故首次出现 = 末次触及。

**不要对每个决策号单跑 `git log -S`**：那要 291 次全历史遍历，
在本机（NAS 挂载）实测每次约 4 秒，合计约 19 分钟，直接超时。
一次 `git log -p` 只要几秒。

## 引用为零意味着什么

**不等于没用**，只等于**没有代码依赖它**。分四种情况：

- 内容包靠它约束生成
- 它是长期约束，靠人读（删了就没人守）
- 它的实现已被后续决策取代（正文会写「已作废」）
- 它确实还没实现

本脚本**不区分这四种**，只把零引用标出来供人分诊。

## 输出

`docs/decision_ledger.tsv`——制表符分隔，便于 `sort` / `awk` / 人工扫读。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

根 = Path(__file__).resolve().parents[1]
台账 = 根 / "DECISIONS.md"
出 = 根 / "docs" / "decision_ledger.tsv"

条目 = re.compile(r"^### (D-\d{3})\s+(.*)$")
号 = re.compile(r"D-\d{3}")

#: 扫哪些目录。**排除 DECISIONS.md 自身**——
#: 决策正文里互相引用是常态，那不算外部依赖。
扫 = ("src", "tests", "scripts", "docs", "content", "内容包")
后缀 = (".py", ".md", ".toml", ".txt", ".json")


def _决策与行数() -> list[tuple[str, str, int, int]]:
    """返回 `(决策号, 标题, 正文行数, 起始行号)`。**只读一次文件**。"""
    行 = 台账.read_text(encoding="utf-8").split("\n")
    起: list[tuple[str, str, int]] = []
    for i, l in enumerate(行):
        m = 条目.match(l)
        if m:
            起.append((m.group(1), m.group(2).strip(), i))
    出_: list[tuple[str, str, int, int]] = []
    for idx, (号_, 标题, i) in enumerate(起):
        止 = 起[idx + 1][2] if idx + 1 < len(起) else len(行)
        正文 = sum(1 for l in 行[i + 1 : 止] if l.strip())
        出_.append((号_, 标题, 正文, i + 1))
    return 出_


def _引用统计() -> dict[str, dict[str, int]]:
    计数: dict[str, dict[str, int]] = {}
    for 目录 in 扫:
        d = 根 / 目录
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.suffix not in 后缀 or "__pycache__" in str(p):
                continue
            try:
                文本 = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            # 该目录内每个号只数一次：一个文件里提 5 次不等于 5 个依赖点
            for n in set(号.findall(文本)):
                计数.setdefault(n, {})[目录] = 计数.setdefault(n, {}).get(目录, 0) + 1
    return 计数


def _末次提交() -> dict[str, str]:
    """一次 `git log -p` 走完全部历史，返回每个决策号的末次触及提交。"""
    # **标记前缀必须与 git 自己的 hunk 头不冲突**——
    # 首版用 `@@%h`，而 diff 里也有 `@@ -1,2 +3,4 @@`，两者撞车，
    # 于是「末次触及」全记成了 hunk 头的行号（下面那串 -979,0 +980,55）。
    # 故用 `提交:%h` 这种 git 不会产出的形式。
    r = subprocess.run(
        ["git", "log", "--format=提交:%h", "-p", "--no-color", "--unified=0"],
        cwd=根,
        capture_output=True,
        text=True,
        check=False,
    )
    结果: dict[str, str] = {}
    当前 = ""
    for l in r.stdout.split("\n"):
        if l.startswith("提交:"):
            当前 = l[3:].strip()
            continue
        if not 当前 or not l.startswith(("+", "-")) or l.startswith(("+++", "---")):
            continue
        for n in 号.findall(l):
            结果.setdefault(n, 当前)  # 最新在前，首次出现即末次
    return 结果


def main() -> int:
    决策 = _决策与行数()
    计数 = _引用统计()
    末次 = _末次提交()
    出.parent.mkdir(parents=True, exist_ok=True)
    行 = ["决策号\t标题\t正文行数\t代码引用\t内容包引用\t文档引用\t末次触及提交"]
    for 号_, 标题, 正文, _起 in 决策:
        c = 计数.get(号_, {})
        代码 = c.get("src", 0) + c.get("tests", 0) + c.get("scripts", 0)
        行.append(
            f"{号_}\t{标题}\t{正文}\t{代码}\t{c.get('content', 0) + c.get('内容包', 0)}"
            f"\t{c.get('docs', 0)}\t{末次.get(号_, '-')}"
        )
    出.write_text("\n".join(行) + "\n", encoding="utf-8")
    零 = [l for l in 行[1:] if l.split("\t")[3:6] == ["0", "0", "0"]]
    print(f"共 {len(决策)} 条 → {出.relative_to(根)}")
    print(f"零引用（代码/内容包/文档皆无）：{len(零)} 条")
    print(f"有代码引用：{sum(1 for l in 行[1:] if int(l.split(chr(9))[3]) > 0)} 条")
    print(f"末次触及提交取到：{len(末次)} 个号")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())