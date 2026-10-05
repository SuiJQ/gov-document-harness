#!/usr/bin/env python3
"""未接线检查：查「**字段在、回调接得上、但实际值是 None**」。

## 为什么必须有这支脚本

前面所有检查脚本都读源码。而 D-406 那七个缺陷**源码上完全正常**：

- 字段有声明（`工具依赖.汇报: 汇报回调 | None = None`）
- 处理器有判空、有失败返回（`if d.汇报 is None: return _未接线(...)`）
- 测试全绿

只有**造一个真实对象、读完字段的实际值**才看得见：
七处「有判空、有失败返回」的背后，装配根一个都没传。
模型调 `msg.report_to_lead` 直接失败，而程序**不报任何错**。

这类缺陷静态查不出——只能**运行时实测**。

## 查什么

1. **可空注入字段在真实装配后的实际值**：
   造一个真的 `App`，建编排器，再读 `工具依赖` 每个字段。
   仍为 `None` 的报出来。
2. **故意后补 vs 漏接**：有些回调装配时为 None 是**设计如此**
   （鸡生蛋，编排器后建才能补）。判据必须区分二者——
   否则满屏误报，而满屏误报的检查等于没有检查。

## 「后补」的白名单必须**逐个写明理由**

理由不成立就不要豁免。本脚本的豁免表里每一条都指向
具体的机制说明，而不是「暂时这样」。

## 白名单之外的新缺口会自动被抓

2026-10-05 实测：本脚本写好当天就抓出 `调用存档` 一个新缺口——
它在 `_过程` 建好前必然是 None，故由编排器经 `接调用存档` 后补。
这属于「必须后补」类，已写进豁免表并注明补的时机。

## 脚本返回 0 时仍要注入验证

造一个明知未接的字段（把某个回调置回 None），
看它是否真报出来。不做这一步，就重演 D-247 第一版
「任何东西都算合规，检查永远抓不到东西」。

```bash
python3 check_wiring_runtime.py                 # 应报 0
# 注入：临时把某个回调清掉
PYTHONPATH=src python3 - <<'EOF'
...
EOF
python3 check_wiring_runtime.py                 # 应报出来
```
"""

from __future__ import annotations

import pathlib
import sys

根 = pathlib.Path(__file__).resolve().parent.parent / "政通Harness"
仓库根 = 根
源码根 = 仓库根 / "src"

if not (源码根 / "zhengtong").is_dir():
    print(f"源码根不存在：{源码根}")
    print("根路径算错了——症状是「恒返回 0」，看起来完全正常。")
    sys.exit(2)

#: **必须后补**的字段：`（字段名, 补的时机, 为什么必须后补)`。
#:
#: 鸡生蛋：工具层在装配时先建好，编排器（含工具回调）后建，
#: 于是持有编排器的回调只能在编排器建好后回填。
#: 判据若不认这类，报告里会全是它们——那就没法看了（D-247 纪律）。
必须后补: tuple[tuple[str, str, str], ...] = (
    (
        "记事件",
        "编排器建好后（app.py 接记事件）",
        "回调要持有编排器，而编排器在工具层之后才建（D-266 的鸡生蛋）。",
    ),
    (
        "转达",
        "编排器建好后（app.py 接转达）",
        "同上，回调要持有编排器的指令队列（D-012）。",
    ),
    (
        "调用存档",
        "_过程 建好后（编排器 接调用存档）",
        "它不是回调而是 `_过程` 的字段，按需惰性创建；过程不存在时无对象可传。",
    ),
)


def _实测() -> dict[str, object]:
    """造真实 App、建编排器，返回 `工具依赖` 每个字段的实际值。"""
    sys.path.insert(0, str(源码根))
    try:
        from zhengtong.app import build_app
    except Exception as exc:  # pragma: no cover - 环境缺依赖时
        print(f"无法导入 zhengtong.app：{exc}")
        print("这不是判据问题，是环境缺依赖——**必须报出来**，不能当成通过。")
        sys.exit(2)

    app = build_app()
    try:
        编排 = app.编排器()
        # 建过程——否则 `调用存档` 那类「按需惰性创建」的字段还没有对象。
        编排._建过程()  # noqa: SLF001
        依 = app.tools._d  # noqa: SLF001
        from zhengtong.services.tools.handlers import 工具依赖

        return {f: getattr(依, f, "<无此字段>") for f in 工具依赖.__dataclass_fields__}
    finally:
        app.close()


def 主() -> int:
    后补名 = {名 for 名, _, _ in 必须后补}
    try:
        实际 = _实测()
    except Exception as exc:
        print(f"实测失败：{exc}")
        sys.exit(2)

    未接 = [f for f, v in 实际.items() if v is None]
    漏接 = [f for f in 未接 if f not in 后补名]
    后补中 = [f for f in 未接 if f in 后补名]

    if 后补中:
        print(f"以下 {len(后补中)} 个字段在装配时为 None，属**设计内的后补**（不报）：")
        for f in 后补中:
            时机, 理由 = next((t, r) for n, t, r in 必须后补 if n == f)
            print(f"  · {f}：{时机}")
            print(f"      理由：{理由}")
        print()

    if 漏接:
        print(f"未接线检查：**{len(漏接)} 个回调在真实装配后仍为 None**——")
        print("  它们的处理器有判空、有失败返回，测试全绿，源码完全正常。")
        print("  但模型调对应工具会直接失败。")
        print("  源码看不出问题，**只有实测能看见**（D-406）。\n")
        for f in 漏接:
            print(f"  · {f}")
        print()
        print("若某处确实该后补，请补进脚本顶部的『必须后补』表并写明时机与理由。")
        return 1

    print(
        f"未接线检查：{len(实际)} 个注入字段全部有值"
        f"（其中 {len(后补中)} 个为设计内后补，均已登记理由）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(主())