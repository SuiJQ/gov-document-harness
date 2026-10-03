#!/usr/bin/env python3
"""接线专项检查（第 3 部分，D-247）。

## 查什么

历史上栽得最多的不是「代码写错」，而是**值定义在那里没人往下传**：

| 缺陷 | 形状 |
|---|---|
| D-231 | 容量被读了没往下传 |
| D-233 | `tools` 字段全仓零填充 |
| D-234 | 温度配置零消费 |
| D-235 | 产物编码零消费 |
| D-222 | 端口收了不读 |

本脚本查**其中可机械判定的一类**：属性存进 `self.X` 后，类内从无读取。

## 两处必须做对，否则全是误报

### 一、必须跟踪二层转发（`self.编排.模型名`）

上轮的第一版脚本不跟踪 `self.a.b`，把 13 个合法属性报成嫌疑。
**全部是误报。**

### 二、只认 `Load`，不认 `Store`（D-247 的第一版栽在这）

第二版把「存过的属性」也算「读过」，于是**任何属性都算见过，
判据永远抓不到东西**——注入一个死属性进去，它仍然报 0 个。

这个坑必须靠**注入验证**才能发现：脚本返回 0 时，
先注入一个确知该被抓的死属性，看它抓不抓得到。

## 脚本只出候选，必须人工定性

上轮实测：57 个候选里只有 5 个是真的。
其余是脚本没建模合法接线方式（二层转发、字典值引用）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

根 = Path(__file__).resolve().parent.parent
源码 = 根 / "src" / "zhengtong"

#: 这些名字天然没有 Load 读取，不该报。
#: 「定义了没人调」是**另一个问题**，由 vulture 负责。
豁免前缀 = ("__",)

#: **跨类转发**：属性存进本类，但读取发生在另一个类里。
#:
#: `编排器` 把参数转交给 `_过程`，`_过程` 又持有 `编排=self` 回指编排器
#: ——**两个对象互指**，类内静态分析看不到另一侧。
#:
#: 全部 13 项经人工核实为合法接线（D-247）：
#: `self.编排.模型名`、`self.编排.预算`、`self.编排.容量token` 等
#: 确实在 `_过程._调用` 里被读取。
#:
#: **新增项须人工确认是真缺陷**，不得为「跑绿」随手加。
跨类转发: dict[str, tuple[str, ...]] = {
    "编排器": (
        "内容包", "validators", "模型名", "预算", "门禁取证", "请求锁",
        "工具回调", "容量token", "温度", "摘要器", "进度终值", "终态",
        # D-267：`工具层` 由 `_过程.__init__` 读走
        # （`self.工具层 = 编排.工具层`），真正用它的 `_跑工具` 挂在 `_过程`。
        # **已人工核实**：D-267 证伪 2 就是「编排器不持有工具层 → 存档恒 None」。
        "工具层",
    ),
    # 测试探针：断言「建索引时确实带了这些文档」，不是业务字段
    "FakeRetriever": ("built_with",),
    # 私有状态经只读性质暴露（`_已降级` 由 `已降级` 属性读出）
    "倒排索引": ("_已降级",),
}


def _属性链(节点: ast.AST) -> list[str] | None:
    """`self.a.b.c` → `["a","b","c"]`；不是 self 链则 `None`。"""
    链: list[str] = []
    节点_ = 节点
    while isinstance(节点_, ast.Attribute):
        链.append(节点_.attr)
        节点_ = 节点_.value
    if isinstance(节点_, ast.Name) and 节点_.id == "self":
        return list(reversed(链))
    return None


def _类内可见链(类: ast.ClassDef) -> set[tuple[str, ...]]:
    """类内对 `self` 的**读取**链（只认 Load）。

    `self.a.b` 读一次会让 `("a",)` 与 `("a","b")` 都算见过——
    因为读得到 `a` 就能拿到它。
    """
    见过: set[tuple[str, ...]] = set()
    for 节点 in ast.walk(类):
        if isinstance(节点, ast.Attribute) and isinstance(节点.ctx, ast.Load):
            链 = _属性链(节点)
            if 链 is None:
                continue
            for i in range(len(链)):
                见过.add(tuple(链[: i + 1]))
    return 见过


def _赋值点(类: ast.ClassDef) -> list[str]:
    """`self.X = …` 的首段属性名。"""
    出: list[str] = []
    for 节点 in ast.walk(类):
        if isinstance(节点, ast.Assign):
            for 目标 in 节点.targets:
                链 = _属性链(目标)
                if 链 and len(链) == 1 and not 链[0].startswith(豁免前缀):
                    出.append(链[0])
    return 出


def main() -> int:
    嫌疑: list[tuple[Path, str, str]] = []
    for p in sorted(源码.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        树 = ast.parse(p.read_text(encoding="utf-8"))
        for 类 in (n for n in ast.walk(树) if isinstance(n, ast.ClassDef)):
            可见 = _类内可见链(类)
            白名单 = set(跨类转发.get(类.name, ()))
            for 名 in _赋值点(类):
                if (名,) in 可见 or 名 in 白名单:
                    continue
                嫌疑.append((p, 类.name, 名))

    if 嫌疑:
        print("接线检查：以下属性存进 self 后类内从无读取——")
        print("**这些是候选，必须逐条回调用链定性**")
        print("（历史上此类候选误报率高：合法接线有二层转发、")
        print("  字典表引用、框架回调等形状）\n")
        for p, 类名, 名 in 嫌疑:
            print(f"  - {p.relative_to(根)} :: {类名}.self.{名}")
        print(f"\n共 {len(嫌疑)} 个候选。")
        return 1

    print(
        f"接线检查通过：{len(list(源码.rglob('*.py')))} 个源文件里，"
        f"没有「存进 self 后类内从无读取」的属性。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())