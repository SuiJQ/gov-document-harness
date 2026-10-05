"""从架构 §14 对照表生成 DECISIONS.md 的 D-001~D-083 段落。

**只用 §14 表格里的原文**，不新增任何解释——本次是"落盘"不是"重写"。
每条带 `出` 字段指回架构节号，便于日后反查。

一次性脚本：生成后即可删除。保留它是为了让"决策落盘"可重复执行，
万一 §14 更新了能重跑而不是手抄。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile

ARCH = pathlib.Path("docs/ARCHITECTURE.md")
OUT = pathlib.Path("DECISIONS.md")
#: 输入文件的缺省位置。**用系统临时目录而不是写死 `/tmp`**——
#: Windows 上没有 `/tmp`，而 `pathlib.Path("/tmp/rows.json")` 在那儿不报错，
#: 它静默变成 `\tmp\rows.json`，于是这个脚本在 Windows 上读不到、
#: 或者更糟：读到了另一个文件（D-381 同型）。
缺省输入 = pathlib.Path(tempfile.gettempdir()) / "rows.json"

#: 分组：(标题, 起, 止)
GROUPS: tuple[tuple[str, int, int], ...] = (
    ("边界与独立性", 1, 19),
    ("格式、输入与需求采集", 20, 34),
    ("门禁、档位与重试", 35, 60),
    ("本轮架构设计自身产生", 61, 72),
    ("实施与工程纪律", 73, 83),
)


def parse() -> dict[str, tuple[str, str, str]]:
    """取 §14 表格首列的每个决策号 → (首列原文, 机制, 节号)。"""
    sec = ARCH.read_text(encoding="utf-8").split("## 14.")[1].split("## 15.")[0]
    out: dict[str, tuple[str, str, str]] = {}
    for line in sec.splitlines():
        if not line.startswith("|"):
            continue
        c = [x.strip() for x in line.split("|")]
        if len(c) < 4:
            continue
        ids = re.findall(r"D-\d{3}", c[1])
        for i in ids:
            out[i] = (c[1], c[2], c[3])
    return out


def main() -> None:
    # 输入路径可由命令行给出——它是**人工准备的中间产物**，
    # 不该被钉死在某个平台上。`python backfill_decisions.py 别的路径.json`
    输入 = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else 缺省输入
    rows = json.loads(输入.read_text(encoding="utf-8"))
    parts: list[str] = []
    for title, lo, hi in GROUPS:
        body: list[str] = []
        for n in range(lo, hi + 1):
            did = f"D-{n:03d}"
            if did not in rows:
                continue
            first, mech, ref = rows[did]
            # 决策名：剥掉开头的编号前缀，剩下的就是决策本身
            name = re.sub(r"^D-\d{3}(\s*/\s*D-\d{3})*\s*", "", first).strip()
            shared = "（与同行决策共享机制）" if len(re.findall(r"D-\d{3}", first)) > 1 else ""
            body.append(
                f"### {did} · {name}{shared}\n\n"
                f"- **决策**：{name}\n"
                f"- **机制落点**：{mech}\n"
                f"- **出**：架构 §14，{ref}\n"
            )
        if body:
            parts.append(f"## {title}（D-{lo:03d} ~ D-{hi:03d}）\n\n" + "\n".join(body))

    block = "\n".join(parts)
    text = OUT.read_text(encoding="utf-8")
    marker = "## 语言与类型（D-084 ~ D-086）"
    head = text.split(marker)[0]
    tail = marker + text.split(marker)[1]

    note = (
        "> **本节 D-001 ~ D-083 由架构 §14 对照表逐条落盘**（第 4.5 步）。\n"
        "> 原文在 `docs/ARCHITECTURE.md` §14，每条标了节号可反查。\n"
        "> 此前这些决策只存在于讨论中、被代码注释引用却从未进本文件，\n"
        "> 违反本文件开头『以本文件为准』的规定；第 4.5 步修的就是这个。\n\n"
        "---\n\n"
    )
    new = head + note + block + "\n---\n\n" + tail
    OUT.write_text(new, encoding="utf-8")
    print(f"已写入 {OUT}：{len(rows)} 条决策")


if __name__ == "__main__":
    main()
