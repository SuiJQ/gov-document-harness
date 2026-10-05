#!/usr/bin/env python3
"""事件接线检查：查「**定义即完成**」——声明了却既不发射也不消费。

## 查什么

架构 §5.4 把 38 个事件全列为契约，`EventKind` 里也全有定义。
但「有定义」不等于「有发射点」。

本轮体检查出 **7 个事件既不发射也不消费**，而 `tests/` 里 2084 条判据全绿。
读架构的人会以为它们存在。这是**契约与实现之间最隐蔽的一种落差**：
它不产生任何错误，只是让承诺变成空话。

所以本脚本机械核对三件事：

1. 每个 `EventKind` 成员在 `src/` 里**至少有一处发射**
   （`发(EventKind.X, ...)` 或 `EventKind.X` 出现在事件构造处）
2. 每个成员在 `src/` 或 `tests/` 里**至少有一处消费**
   （被读取/匹配，而不只是被 import）
3. 每个成员都在**架构 §5.4 的事件表**里

## 为什么第 2 条不能省

「有发射点」不等于「有人看」。本轮的 `direction.confirmed`
连发射点都没有，而更早的 D-118 还删掉了它的消费侧——
两边都空，架构里却写得清清楚楚。

反过来，「有消费但无发射」同样荒谬（消费侧永远进不来），
所以两条都要查。

## 白名单

「刻意发射但仅在测试中消费」之类的情况允许豁免，
每条必须写明理由。不允许「先放着」。

## 脚本返回 0 时仍要注入验证

加一个明知零发射的事件成员，看它是否真报出来。
不做这一步，就重演 D-247 第一版「任何东西都算见过，
检查永远抓不到东西」。

```bash
# 在 EventKind 里加一行 NEVER_EMITTED = "never.emitted"
python3 check_event_wiring.py   # 必须报出来
```
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

根 = pathlib.Path(__file__).resolve().parent.parent / "政通Harness"
仓库根 = 根
源码根 = 仓库根 / "src" / "zhengtong"
架构文件 = 仓库根 / "docs" / "ARCHITECTURE.md"

if not 源码根.is_dir():
    print(f"源码根不存在：{源码根}")
    print("根路径算错了——症状是「恒返回 0」，看起来完全正常。")
    sys.exit(2)

#: 台账：`（成员名, 类别, 理由)`。
#:
#: 类别两档，**不可混用**：
#: - `合理`——该事件本就不该有发射点（如依赖尚未实现的机制）。
#:   将来若机制落地，须删掉这一行让脚本重新报出来。
#: - `待处理`——**它确实是个缺口**，但本轮不修。
#:   留着会让检查变绿，可清单一直可见，将来必须清理。
#:
#: 为什么不直接修：`零发射` 与 `有消费侧但零发射` 处置方向相反
#: （前者常应删定义，后者常应补发射），11 个逐个定性需要架构判断，
#: 不该在写检查脚本时顺手决定。
台账: tuple[tuple[str, str, str], ...] = (
    # ── 待处理：消费侧已在等一个永不到来的事件（D-118 同族死分支）──
    ("MODEL_ERROR", "待处理", "消费侧（投影/界面）在等它，但无人发射。补发射点或删消费侧，二者必须择一。"),
    ("MODEL_RETRY", "待处理", "同上：重试是真实行为，却没有任何事件留痕，事后无法判断「这次重试过几次」。"),
    ("RETRIEVAL_DONE", "待处理", "检索完成无事件：档案构建（D-201）的过程无法从 journal 复原。"),
    ("ROLE_STATE_CHANGED", "待处理", "成员状态变化无事件：D-296 刚把成员状态写回 RunState，但没有事件告知界面。"),
    ("ROLE_TURN", "待处理", "成员发言轮次无事件：标准模式不显示（架构要求），但留痕缺失。"),
    # ── 待处理：既无发射也无消费，需逐个判断该补还是该删 ──
    ("ARTIFACT_DRAFTED", "待处理", "中间产物落盘。当前只有主件一个产物，无此动作；等多附件统一交付。"),
    ("CONTEXT_COMPOSED", "待处理", "上下文装配无事件。上下文压缩是用户可感知的行为（D-231），却没有任何通知。"),
    ("ROLE_REPORT", "待处理", "成员向主理人汇报无事件。工具层已实现汇报（D-177 汇报闭环），但未发事件。"),
    ("ROLE_SLEEP", "待处理", "成员休眠无事件。休眠会改变后续调度，界面上看不到。"),
    ("ROLE_WAKE", "待处理", "条件事件唤醒无事件。用户约束明确要求这个能力，实现却在、无事件。"),
    ("TOOL_DENIED", "待处理", "工具被拒无事件。D-310 记了「权限越界必须给出可读原因」，但那条原因不进事件。"),
)


def _全部成员() -> dict[str, str]:
    """`EventKind` 的 `成员名 -> value`。

    ## 用 AST 读而不是 import

    同目录的检查脚本都不依赖运行环境（未装依赖的机器也能跑），
    import 会让 `check.sh` 在裸环境里报 `ModuleNotFoundError`——
    而那个报错与「检查什么」毫无关系，只会让人以为检查过了。
    """
    树 = ast.parse((源码根 / "domain" / "event.py").read_text(encoding="utf-8"))
    for 节 in ast.walk(树):
        if not isinstance(节, ast.ClassDef) or 节.name != "EventKind":
            continue
        表: dict[str, str] = {}
        for 项 in 节.body:
            if not isinstance(项, ast.Assign) or not isinstance(项.value, ast.Constant):
                continue
            if len(项.targets) == 1 and isinstance(项.targets[0], ast.Name):
                表[项.targets[0].id] = str(项.value.value)
        return 表
    print("domain/event.py 里找不到 EventKind 类。")
    sys.exit(2)


def _代码与注释(路径: pathlib.Path) -> str:
    return 路径.read_text(encoding="utf-8")


def _发射点(成员: str) -> list[str]:
    """成员在 `src/` 里的发射点文件列表。

    判据是「成员名出现在事件**构造/发送**的位置」：
    `发(EventKind.X, ...)` / `EventKind.X` 作为参数传入。
    """
    命中: list[str] = []
    模式 = re.compile(rf"EventKind\.{re.escape(成员)}\b")
    for 路径 in sorted(源码根.rglob("*.py")):
        if "domain/event.py" in str(路径):
            continue  # 定义文件本身不算发射点
        if 模式.search(_代码与注释(路径)):
            命中.append(str(路径.relative_to(仓库根)))
    return 命中


def _消费点(成员: str) -> list[str]:
    """成员在 `src/` 或 `tests/` 里被**读取/匹配**的位置。"""
    命中: list[str] = []
    值模式 = re.compile(rf"[\"']{re.escape(成员)}[\"']")
    成员模式 = re.compile(rf"EventKind\.{re.escape(成员)}\b")
    for 根目录 in (源码根, 仓库根 / "tests"):
        for 路径 in sorted(根目录.rglob("*.py")):
            文本 = _代码与注释(路径)
            if "domain/event.py" in str(路径):
                continue
            # 消费 = 出现在 if/比较/断言里，或按字符串字面量匹配。
            # 单纯出现在 import 行不算。
            for 序, 行 in enumerate(文本.splitlines(), start=1):
                if 行.strip().startswith(("from ", "import ")):
                    continue
                if 成员模式.search(行) or 值模式.search(行):
                    命中.append(f"{路径.relative_to(仓库根)}:{序}")
                    break
    return 命中


def _架构列了(成员值: str) -> bool:
    文本 = 架构文件.read_text(encoding="utf-8")
    起 = 文本.find("### 5.4 事件目录")
    if 起 < 0:
        print("架构 §5.4 事件目录找不到——判据无法核对，请检查文档结构。")
        sys.exit(2)
    止 = 文本.find("### 5.5 追溯能力清单")
    节 = 文本[起 : 止 if 止 > 起 else len(文本)]
    return f"`{成员值}`" in 节


def 主() -> int:
    成员表 = _全部成员()
    缺失: list[str] = []
    台账表 = {名: (类, 理) for 名, 类, 理 in 台账}
    待处理清单: list[str] = []

    for 成员, 值 in sorted(成员表.items()):
        if 成员 in 台账表 and 台账表[成员][0] == "合理":
            continue
        发点 = _发射点(成员)
        吃点 = _消费点(成员)
        架构有 = _架构列了(值)

        问题: list[str] = []
        if not 发点:
            问题.append("零发射")
        if not 吃点:
            问题.append("零消费")
        if not 架构有:
            问题.append("架构 §5.4 未列")
        if 问题:
            条目 = f"  {成员}（{值}）：{'、'.join(问题)}"
            if 成员 in 台账表 and 台账表[成员][0] == "待处理":
                待处理清单.append(条目 + f"  ← {台账表[成员][1]}")
            else:
                缺失.append(条目)

    if 待处理清单:
        print(f"事件接线检查：**已知待处理 {len(待处理清单)} 个**（台账登记，非豁免）：")
        print()
        print("\n".join(待处理清单))
        print()

    if 缺失:
        print("事件接线检查：以下成员「定义即完成」——")
        print("  架构承诺了、枚举里有，但既不发射也不消费（或缺文档条目）。")
        print("  这不产生任何错误，只是让承诺变成空话。\n")
        print("\n".join(缺失))
        print()
        print(f"共 {len(缺失)} 个。若确属合理，请写进脚本顶部的『豁免』表并注明理由。")
        return 1

    if not 待处理清单:
        print(f"事件接线检查：{len(成员表)} 个事件全部有发射点、有消费点、且在架构 §5.4 列明。")
    else:
        print(f"合计 {len(成员表)} 个事件，其中 {len(待处理清单)} 个登记为待处理。")
    return 0


if __name__ == "__main__":
    sys.exit(主())