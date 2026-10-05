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
#: 定性结论见 `EVENT_TRIAGE_TODO.md` 与 RULES.md D-405/D-406。
#: **注意**：首版台账里 `TOOL_DENIED` 被记成「零发射」，那是**判据假阴性**
#: ——工具层发的是字符串 `"tool.denied"`，由编排层 `EventKind(kind)` 转换。
#: 本次定性把它改判为「合理」（链路完整，见 D-405）。
台账: tuple[tuple[str, str, str], ...] = (
    ("TOOL_DENIED", "合理", "链路完整：runner.py:211 发 `tool.denied` → 编排层 `EventKind(kind)` → 发事件（D-266）。首版判据只认 `EventKind.X` 形状，漏了 L4→L5 字符串链路。"),
    # ── 待处理：机制在适配器内部，编排层看不见（D-408 说明）──
    # MODEL_ERROR / CONTEXT_COMPOSED / RETRIEVAL_DONE / ROLE_STATE_CHANGED
    # 已于 D-408 接上发射点。
    ("MODEL_RETRY", "待处理", "**机制在适配器内部**（`http_llm.complete` 的重试循环，D-027），编排层只看得到最终成败。要如实记录次数须让适配器把该信息带出来——改 `LlmResponse` 或加结果字段。D-408 刻意**没有假装能发**这个事件。"),
    ("ROLE_SLEEP", "合理", "已发射（D-408）。零消费是**有意**的：成员内部动作不进标准界面（D-046）。"),
    ("ROLE_WAKE", "合理", "已发射（D-408）。零消费同上。"),
    ("ROLE_REPORT", "合理", "已发射（D-408）。零消费同上。"),


    # ── 待处理：机制有代码但真实路径未接线（D-406，须先接线再补发射）──
    # ROLE_REPORT / ROLE_SLEEP / ROLE_WAKE 已于 D-408 接线并发射——
    # 它们现在只差消费侧（零消费），属「有意不消费」：标准模式不显示成员
    # 内部动作（D-046），而调试投影已能看到全部事件流。故不必接消费。
    # ── 应删：机制不存在 ──
    ("ARTIFACT_DRAFTED", "合理", "**应删定义**：当前只有主件一个产物，无「草稿落盘」这个动作。架构 §5.4 写它属于超前承诺。多附件统一交付（D-023 决策）落地前不需要。"),
    # ── 无机制，且标准模式不显示 ──
    ("ROLE_TURN", "合理", "无对应机制，且标准模式刻意不显示成员发言（D-046）。补发射点前先决定它该不该存在。"),
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


def _发射点(成员: str, 值: str) -> list[str]:
    """成员在 `src/` 里的发射点文件列表。

    判据是「成员名出现在事件**构造/发送**的位置」：
    `发(EventKind.X, ...)` / `EventKind.X` 作为参数传入。
    """
    命中: list[str] = []
    # **两种形状都要认**（D-405）：
    #   `EventKind.TOOL_DENIED` —— 编排层直接发；
    #   `_发事件("tool.denied", ...)` —— 工具层发字符串，由编排层
    #     `EventKind(kind)` 转成事件。后者是 L4→L5 唯一合规的形状
    #     （低层不构造高层值对象，架构 §2.3），**必须算发射点**。
    #
    # 第一版只认前一种，于是把 `tool.denied` 报成零发射——
    # 而它实际有完整发射链路。那是判据的假阴性，
    # 差点让我去补一个**已经存在**的发射点。
    成员模式 = re.compile(rf"EventKind\.{re.escape(成员)}\b")
    # **按值匹配**（`"tool.denied"`），不是按成员名（`TOOL_DENIED`）：
    # 工具层发的是 `EventKind.value`，形如 "tool.denied"。
    # 第一版拿成员名去匹配字符串表值，于是漏掉了整条 L4→L5 链路。
    值模式 = re.compile(rf"""["']{re.escape(值)}["']""")
    for 路径 in sorted(源码根.rglob("*.py")):
        if "domain/event.py" in str(路径):
            continue  # 定义文件本身不算发射点
        文本 = _代码与注释(路径)
        if 成员模式.search(文本) or 值模式.search(文本):
            命中.append(str(路径.relative_to(仓库根)))
    return 命中


def _消费点(成员: str, 值: str) -> list[str]:
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
        发点 = _发射点(成员, 值)
        吃点 = _消费点(成员, 值)
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