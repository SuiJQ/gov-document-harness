#!/usr/bin/env python3
"""端口契约字段的**用户路径**消费检查（D-270）。

## 为什么 `check_field_consumption.py` 不够

那个脚本问的是「全仓有没有人读这个字段」。
**对端口字段而言，这个问题太弱**——

`TokenUsage.prompt_tokens` 在 `llm_probe.py` 里被读过，
于是「有人读」成立；但那是**连接检测**路径，不是用户路径：
编排层不记、投影层不展示、journal 不落盘。
**用户永远不知道自己花了多少 token**（D-269）。

> **「有人读」≠「用户看得到」。**
> 这是 D-267 复查时查出的两处缺陷的共同成因。

## 本脚本问的是

「这个端口字段，**在用户路径上**有没有被消费？」

用户路径 = 编排层（orchestration）+ 投影层（kernel/projection）
+ journal + presentation。
**排除** adapters（适配器自我消费不算）与 tests。

## 为什么排除 adapters 是对的、也是危险的

排除 adapters 会漏掉「适配器读了但没送出去」——**那正是 D-269**。
所以本脚本**只报候选、不定性**（与 D-247 同一条纪律），
由人工逐条回调用链核实。

## 白名单：每条都要写理由，不许裸放行

合法零消费有四类：
- 真适配器自我消费（回传后端原值给调用方，属适配器职责）
- 端口自身内部使用（属性、方法依赖它）
- 测试探针
- **已立案**（带 D-xxx 编号）
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

根 = Path(__file__).resolve().parent.parent
#: 包根。**注意多一层包名**：真实布局是 `src/zhengtong/ports/`，
#: 不是 `src/ports/`。
#:
#: 首版写成 `源码 / "ports"`，于是 `_端口字段()` 恒返回空列表、
#: 检查永远「全部通过」——**一个永远绿的检查等于没有检查**
#: （D-247 第一版的坑：任何东西都算见过）。
#: 是注入验证抓出来的：塞一个明知零消费的字段，它仍报「均有消费」。
源码 = 根 / "src" / "zhengtong"

#: **用户路径**：这些目录里的读取才算「用户看得到」。
#:
#: `services` 在列：工具处理器（`handlers.py:205`）把 `SearchHit.ref/snippet`
#: 转成模型可见的文本——**检索结果就是经它送到用户面前的**。
#: 首版漏了它，误报 `SearchHit.ref` / `snippet` / `score` 三个。
用户路径 = ("orchestration", "kernel", "presentation", "services")

#: 已立案未修 → 缺陷编号。
已立案: dict[str, str] = {
    "raw_finish_reason": "D-268 写了没人读，与 D-262 ToolSpec.effect 同型",
    # —— 装配根 app.py 里直接构造这两个适配器（LocalSecrets/SystemClock），
    # 不经 PortRegistry 取。故登记册里的这两个字段确实无人取用。
    # **不是缺陷**：登记册的价值在 missing() 自检（8 个端口漏一个就炸），
    # 字段本身是「登记齐全」的凭据，取用路径另有其人。
    "secrets": "app.py 直接构造 LocalSecrets/SystemClock，不经登记册取；登记册字段用于 missing() 自检",
    "clock": "同上",
    # —— 定性为合法：PortRegistry 由装配根消费，不在三层里 ——
    # `app.py` 用 `self.ports.llm` 等注入各层；它不是「用户可见数据」，
    # 是依赖登记册。漏掉 app.py 会误报 5 个。
}

#: 定性为合法的零消费 → 理由。
#:
#: **`PortRegistry` 的 8 个端口字段在此**：它们由装配根 `app.py` 消费
#: （`self.ports.llm` 等注入各层），而 app.py 不在用户路径三层内。
#: 那不是「零消费」——**依赖登记册本来就不该出现在用户路径上**。
白名单: dict[str, str] = {
    "llm": "装配根 app.py 消费：依赖登记册，不是用户可见数据",
    "storage": "装配根 app.py 消费，同上",
    "user_files": "装配根 app.py 消费，同上",
    "validators": "装配根 app.py 消费，同上",
    # 真适配器自我消费：把后端原值回传给调用方，属适配器职责
    "raw_error_code": "真适配器回传后端原始错误码，供错误映射用（§6）",
    # 端口自身内部使用
    "role": "Message.role 是协议字段，装配与回传时按值取用",
    # —— 请求侧字段：编排层构造，适配器读 ——
    # 「用户路径」不含 adapters，但**请求字段的消费者本来就该是适配器**
    # （它要发 HTTP 请求）。故这几个是合法的跨层消费，不是零消费。
    "max_tokens": "请求侧：http_llm.py:336 据此写 max_tokens（构造者是编排层，消费者是适配器）",
    "额外字段": "请求侧：http_llm.py:353 原样附加（探测透传后端字段，D-245）",
    "model": "请求侧：编排层构造时传，http_llm 写入 URL",
    # —— 响应侧字段：适配器解析，用户路径按值判断 ——
    "finish_reason": "响应侧：http_llm.py:457 解析后端值；用户路径按枚举值分支而非读属性",
    "reasoning_tokens": "响应侧：reasoning_probe.py:115 读它判定推理是否真发生（D-245）",
}

#: 已修（留档以免同一条再被当成新发现报一遍）。
#:
#: **D-272 · `Message.tool_call_id`** —— 此前只发不收：
#: 适配器会把它序列化进请求体，但全仓没有任何一处构造带
#: `tool_call_id` 的消息，于是工具跑得起来、结果却回不到模型手上。
#: D-274 补 `记工具结果()` 后 `orchestrate.py` 已在用户路径消费该字段。
已修 = {
    "Message.tool_call_id": "D-274 已修：orchestrate.py 记工具结果() 按 OpenAI 协议完整回传",
    "TokenUsage.prompt_tokens": "D-277 已修：model.response 事件带真实用量，投影累计显示",
    "TokenUsage.completion_tokens": "D-277 同上",
    "LlmResponse.usage": "D-277 已修：编排层 _记用量() 读它并发事件",
    "ValidationIssue.rule": "D-278 已修：ValidationIssue.定位 把规则号带进两个出口",
    "ValidationIssue.at": "D-278 已修：同上，锚点带进两个出口",
}

#: 已知的两处真缺陷（D-267 复查逐字段查出，非本脚本发现）。
#: 列出以便对照——**本脚本的价值恰在于它们会被报出来**。
已知缺陷: dict[str, str] = {}


def _端口字段() -> list[tuple[Path, str, str]]:
    """抽出 ports/ 下所有 dataclass 的字段。"""
    出: list[tuple[Path, str, str]] = []
    for p in sorted((源码 / "ports").glob("*.py")):
        if p.name == "__init__.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for 类 in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            for 项 in 类.body:
                if isinstance(项, ast.AnnAssign) and isinstance(项.target, ast.Name):
                    名 = 项.target.id
                    if not 名.startswith("_"):
                        出.append((p, 类.name, 名))
    return 出


def _用户路径读取() -> dict[str, int]:
    """扫用户路径，统计各字段名被消费的次数。

    ## 认三种写法（首版只认一种，误报 15 个）

    1. **属性读取** `x.字段`（`Load` 上下文）
    2. **关键字传参** `字段=值` —— 如 `LlmRequest(temperature=...)`。
       编排层构造请求全走这一种，**只认属性读取会把它们全报成零消费**。
    3. **下标** `x["字段"]` —— 少用，但 json 化时常见。

    ## 为什么第 2 种特别要紧

    `LlmRequest` 的字段**几乎只在构造时用**（温度、工具清单、预算），
    之后由适配器 `request.temperature` 读。
    只扫属性读取的话，`temperature` / `tools` / `max_tokens` 全是假零消费。
    """
    索引: dict[str, int] = {}
    for p in sorted(源码.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        相对 = p.relative_to(源码)
        if 相对.parts[0] not in 用户路径:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for 节点 in ast.walk(tree):
            if isinstance(节点, ast.Attribute) and isinstance(节点.ctx, ast.Load):
                索引[节点.attr] = 索引.get(节点.attr, 0) + 1
            elif isinstance(节点, ast.keyword) and 节点.arg:
                索引[节点.arg] = 索引.get(节点.arg, 0) + 1
            elif isinstance(节点, ast.Subscript) and isinstance(节点.slice, ast.Constant):
                if isinstance(节点.slice.value, str):
                    索引[节点.slice.value] = 索引.get(节点.slice.value, 0) + 1
    return 索引


def _端口自身转发(端口字段: set[str], 用户索引: dict[str, int]) -> dict[str, int]:
    """认「**字段在端口自身被读、且承载它的 property 真被用户路径调用**」
    （D-278 补的口子）。

    ## 要解决什么假阳性

    `ValidationIssue.rule` / `.at` 由 `ports/validators.py` 的
    `定位` 属性读取，而**消费**发生在别处：
    `orchestration/orchestrate.py` 与 `services/tools/handlers.py`
    都调用 `i.定位`，把规则号与锚点带进给用户／给模型的文本。

    读取在端口里、消费在用户路径——原脚本只看用户路径，
    于是报「已修却零消费」，是**假阳性**。

    ## 加「property 被调用过」这一条挡住了什么

    不加的话，「字段在自己类里被读」一律算消费。
    而 D-263 那类字段（构造时就没填值）也长这个样子，
    至少不会被静默放过。

    ## 诚实的局限（证伪五查出来的，别把它当万能）

    **本口径挡不住「读了但读到空值」。**
    证伪：把真实校验器的 `rule="D040-必填"` 改成 `rule=""`，
    脚本仍报「已修并有消费 6/6」——因为 `定位` 确实读了 `self.rule`，
    只是读到的是空串。**它证明的是「有人在读」，不是「读到的不是空的」。**

    真正的兜底是判据：`tests/test_validation_localization.py` 的
    `test_空字段带规则与锚点` 断言的是**真实校验器的实际产出**，
    它在上述证伪下确实变红（证伪三已验证）。
    **脚本负责指疑点，判据负责定性。**
    """
    命中: dict[str, int] = {}
    for p in sorted((源码 / "ports").glob("*.py")):
        if p.name == "__init__.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for 类 in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            for 项 in 类.body:
                if not isinstance(项, ast.FunctionDef):
                    continue
                装饰 = {
                    d.attr if isinstance(d, ast.Attribute) else getattr(d, "id", "")
                    for d in 项.decorator_list
                }
                if "property" not in 装饰 and "cached_property" not in 装饰:
                    continue
                # 条件 2：这个 property 在用户路径里被调用过
                if 用户索引.get(项.name, 0) == 0:
                    continue
                # 条件 1：property 内部读了哪些端口字段
                for 节点 in ast.walk(项):
                    if (
                        isinstance(节点, ast.Attribute)
                        and isinstance(节点.ctx, ast.Load)
                        and 节点.attr in 端口字段
                    ):
                        命中[节点.attr] = 命中.get(节点.attr, 0) + 1
    return 命中


def _按名枚举() -> set[str]:
    """扫「字符串字面量」形式的消费。

    ## 为什么单列一类

    `PortRegistry.missing()` 用 `("llm", "storage", ...)` **按名枚举**
    来自检，不是属性访问——只扫 `x.字段` 会把 8 个端口全报成零消费。

    同理，未来若有 `getattr(obj, "字段名")` 式的动态取用，
    也只能靠字符串扫出来。

    ## 为什么只扫「看起来像字段名」的短字符串

    全量扫字符串会把所有中文文案、错误信息都算进去，
    那样任何字段都能「找到消费者」，检查就废了。
    故只认**出现在 ports/ 的 dataclass 字段名集合里**的字符串。
    """
    字段名 = {名 for _, _, 名 in _端口字段()}
    命中: set[str] = set()
    for p in sorted(源码.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        相对 = p.relative_to(源码)
        if 相对.parts[0] not in 用户路径:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for 节点 in ast.walk(tree):
            if isinstance(节点, ast.Constant) and isinstance(节点.value, str):
                if 节点.value in 字段名:
                    命中.add(节点.value)
    return 命中


def main() -> int:
    索引 = _用户路径读取()
    全部字段 = _端口字段()
    # 补「字段在端口自身被 property 读、property 被用户路径调用」这一类（D-278）
    for 名, 次 in _端口自身转发({名 for _, _, 名 in 全部字段}, 索引).items():
        索引[名] = 索引.get(名, 0) + 次
    按名 = _按名枚举()
    for 名 in 按名:
        索引[名] = 索引.get(名, 0) + 1
    候选: list[tuple[str, str, str]] = []
    for p, 类名, 字段名 in 全部字段:
        if 索引.get(字段名, 0) > 0:
            continue
        if 字段名 in 已立案:
            continue
        if 字段名 in 白名单:
            continue
        候选.append((p.relative_to(根).as_posix(), 类名, 字段名))

    已 = [
        (f"{p}::{类}.{名}", 理由)
        for p, 类, 名 in 全部字段
        for 理由 in [已立案.get(名)]
        if 理由 is not None
    ]
    # 已修字段现在**应该有消费**；若仍为零消费，说明修复被回退或漏了接线。
    # 这一条把「已修」表接进检查——否则它只是文档，不是判据。
    修好未接线 = [
        f"{名}：{理由}"
        for 名, 理由 in 已修.items()
        if 索引.get(名.split(".", 1)[-1], 0) == 0
    ]
    # 已知缺陷必须仍处于「已立案」状态——两者不一致说明有人改了其中一张表。
    对不上 = [
        名 for 名 in 已知缺陷 if 名.split(".", 1)[-1] not in 已立案
    ]

    print("端口字段用户路径消费检查")
    print("（用户路径 = orchestration / kernel / presentation；"
          "**适配器自我消费不算**）")
    print()
    if 已:
        print(f"已立案未修（{len(已)} 个）：")
        for 位置, 理由 in 已:
            print(f"  - {位置.replace(str(根) + '/', '')}\n      {理由}")
        print()
    if 候选:
        print(f"**未定性候选 {len(候选)} 个——必须逐条回调用链核实**：")
        for p, 类名, 名 in 候选:
            print(f"  - {p} :: {类名}.{名}")
        print()
        print("合法零消费有四类：真适配器自我消费、端口自身内部使用、"
              "测试探针、已立案。")
        print()
    if 已修:
        print(f"已修并有消费（{len(已修) - len(修好未接线)}/{len(已修)} 个）：")
        for 名, 理由 in 已修.items():
            if 名.split(".", 1)[-1] in 索引 and 索引[名.split(".", 1)[-1]] > 0:
                print(f"  - {名}：{理由}")
        print()
    if 修好未接线:
        print(f"**已修却零消费（{len(修好未接线)} 个）——修复被回退或漏了接线**：")
        for 条 in 修好未接线:
            print(f"  - {条}")
        print()
    if 对不上:
        print(f"**已知缺陷与已立案表不一致（{len(对不上)} 个）**：{对不上}")
        print("  两张表必须同步：已知缺陷 = 尚未修的，已立案 = 全部登记。")
        print()
    return 1 if (候选 or 修好未接线 or 对不上) else 0


if __name__ == "__main__":
    sys.exit(main())