"""按分类拆分 `DECISIONS.md` → `RULES.md` / `EXPERIENCE.md` / `TODO.md`。

## 为什么留存根而不是删掉

`check_decision_refs.py` 要求代码与内容包里每个 `D-xxx` 引用
都能在 `DECISIONS.md` 找到定义。代码里引用了 190+ 个决策号，
**删掉已实现条目的正文就会让那些引用悬空**。

所以已搬走的条目在 `DECISIONS.md` 里降级成一行存根：

    ### D-043 终端字体
    → 全文见 `RULES.md` §D-043

编号保留、引用照样解析，正文也搬走了——两边都不吃亏。

## 三类的区别不是「位置」，是「态度」

| 去向 | 能不能删 |
|---|---|
| `RULES.md` | **不能删**。删了下一个人会重新发明一遍，然后踩同一个坑 |
| `EXPERIENCE.md` | 原则上不删；但其中的**快照**类可以删（那只是当时的数字） |
| `TODO.md` | 做完即删——它本来就是「还没做」 |

## 为什么不做双向同步

搬走的正文**只存一份**，不做链接回填。
两份副本必然分叉，分叉之后没人知道哪份是真的——
`STATUS.md` 就是这么烂掉的。

## 幂等

重跑本脚本应当是空操作（除首次搬运）。
判据：每块要么在 `DECISIONS.md` 有完整正文，要么有一行存根，二者恰居其一。
"""

from __future__ import annotations

import collections
import pathlib
import re

根 = pathlib.Path(__file__).resolve().parents[1] / "政通Harness"
台账 = 根 / "DECISIONS.md"
分类 = 根 / "docs" / "decision_classification.tsv"

头 = re.compile(r"(D-\d{3})\s+(.*)")
存根 = re.compile(r"^→ 全文见 ")

抬头 = {
    "RULES": (
        "# RULES · 全项目规则\n\n"
        "**永久有效，不得删。**删了下一个人会重新发明一遍，然后踩同一个坑。\n\n"
        "每条对应 `DECISIONS.md` 里的同号存根。编号是引用凭据——\n"
        "代码与内容包引用 `D-xxx` 时，`check_decision_refs.py` 查的是 `DECISIONS.md`。\n\n"
    ),
    "EXPERIENCE": (
        "# EXPERIENCE · 经验与经历\n\n"
        "**经历**是发生过的事，分已解决与未解决；**经验**是可复用的教训。\n\n"
        "编号只为了可被引用，**不表示它们是决策**。\n"
        "标「未解决」的条目是真欠账，见 `TODO.md`。\n\n"
    ),
    "TODO": ("# TODO · 尚未实现的事项\n\n每条都记着**它卡在哪、什么时候记的**。做完即删。\n\n"),
}

去处文件 = {
    "RULES": "RULES.md",
    "EXPERIENCE": "EXPERIENCE.md",
    "TODO": "TODO.md",
}


def main() -> int:
    表 = [l.split("\t") for l in 分类.read_text(encoding="utf-8").split("\n")[1:] if l.strip()]
    去向 = {r[1]: r[4] for r in 表}

    块 = re.split(r"^### ", 台账.read_text(encoding="utf-8"), flags=re.M)[1:]
    留: list[str] = []
    搬: dict[str, list[str]] = collections.defaultdict(list)
    快照数 = 0
    无号序 = 0

    for i, b in enumerate(块):
        首 = b.split("\n")[0].strip()
        m = 头.match(首)
        if m:
            号, 标题 = m.group(1), m.group(2)
        else:
            # **无编号块**（此前的经验与经历）。按出现次序取分类表给它补的号。
            # 不给它编号，它们就永远进不了按号管理的任何办法——
            # 这正是它们当初被漏管的原因。
            无号序 += 1
            号, 标题 = 表[i][1], 首
        正文 = b.split("\n", 1)[1] if "\n" in b else ""
        目标 = 去向.get(号, "")
        if 目标 == "可删":
            快照数 += 1
            留.append(
                f"### {号} {标题}\n→ **已归档**：{标题[:40]}（此类条目不承载规矩，仅是当时的数据或已失效的决定）\n\n"
            )
            continue
        if 目标 in 去处文件:
            搬[去处文件[目标]].append(f"### {号} {标题}\n{正文.rstrip()}\n")
            留.append(f"### {号} {标题}\n→ 全文见 `{去处文件[目标]}`\n\n")
        else:
            留.append(f"### {号} {标题}\n" + 正文)

    台账.write_text(
        台账.read_text(encoding="utf-8").split("### ")[0] + "".join(留), encoding="utf-8"
    )
    for 键, 文件 in 去处文件.items():
        p = 根 / 文件
        已有 = p.read_text(encoding="utf-8") if p.exists() else ""
        if not 已有.startswith("#"):
            已有 = ""
        body = "".join(搬[文件])
        p.write_text(已有 + 抬头[键] + body, encoding="utf-8")
    print(f"台账留 {len(留)} 块；归档 {快照数} 条快照；")
    for 文件, 条 in 搬.items():
        print(f"  {文件}: {len(条)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
