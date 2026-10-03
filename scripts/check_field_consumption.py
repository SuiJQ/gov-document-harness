#!/usr/bin/env python3
"""领域数据字段的生产消费检查（D-257）。

## 为什么 `check_wiring.py` 查不到这一类

D-247 的接线检查查的是「属性存进 `self.X` 后**类内**从无读取」——
它盯的是**类实例**。而本脚本盯的是**数据字段**：

| 形状 | D-247 查得到吗 | 本脚本查 |
|---|---|---|
| `self.温度` 存了不读 | ✅ 查得到 | — |
| `TierSpec.policy_retrieval` 全仓无人读 | ❌ **查不到** | ✅ |

D-239 的三个字段正是后者：它们是 frozen dataclass 的字段，
经 `TIER_BY_ID[档].字段名` 读取——**类内 self 链上根本不出现**。

## 为什么这类缺陷最危险

1. **测试照常全绿**——字段值对不对是可测的，
   「它有没有改变行为」要另写判据；
2. **不报错**——读不到就不读，没有异常；
3. **看起来像已实现**——数据在那儿、测试在断言它的值。

D-239 三项查出来时，`allows_rewrite` / `policy_retrieval` 已零消费
（其中 `allows_rewrite` 还被 `gates_apply` 巧合遮住，看起来完全正常）。

## 本脚本只出候选，不定性

与 D-247 同一条纪律：**机械检查误报率高**。

四类合法「零消费」必须靠白名单放行，且每条都要写明理由：
- frozen dataclass 的纯数据字段（只在构造与序列化用）
- 端口签名要求的字段（实现方在别的文件）
- 提示词/内容包消费（不在 `.py` 里）

**脚本返回 0 时仍要注入验证**：塞一个明知零消费的字段进去，
看它是否真报出来。不做这一步，就重演 D-247 第一版
「任何属性都算见过，判据永远抓不到东西」。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

根 = Path(__file__).resolve().parent.parent
源码 = 根 / "src"

#: 合法的零消费字段 → 理由。**每条都要写理由，不许裸放行。**
白名单: dict[str, str] = {
    # 反序列化专用：写进去是为了往返，读取方在别的模块
    "完整阶段序列": "投影层读取，见 orchestrate.编排结果",
}

#: **已立案未修**的真缺陷 → 缺陷编号。
#:
#: 为什么要有这一栏：D-247 的接线检查是「有候选即红」，
#: 但本脚本查出的 4 个真缺陷**本轮不修**（各有前置依赖，
#: 详见 D-257）。若不登记，检查一接进 check.sh 就永久红。
#:
#: **每条必须写编号，不许裸豁免**——否则「先记着以后再说」
#: 会变成永远不说（D-125 就是被推了两轮才发现的）。
已立案: dict[str, str] = {
    # —— 第 5 部分逐条回调用链定性的结果 ——
    # 已立案未修（各有前置，详见 D-257）
    "工具名": "D-258 审计条目零消费",
    "裁决": "D-258",
    "时间戳": "D-258",
    "参数摘要": "D-258",
    "结果摘要": "D-258",
    "错误码": "D-258",
    "score": "D-259 SearchHit.score 零消费",
    "rule": "D-260 校验结论丢了「命中哪条规则」",
    "script": "D-260",
    "丢弃条数": "D-261 压缩省了多少从不显示",
    "摘要token": "D-261",
    "effect": "D-262 ToolSpec.effect 仅测试读取，副作用分类未执行",
    "requires_publish": "D-262",
    "原文出处": "D-263 导出扫描的「原文出处」未接入",
    "reasoning_policy": "D-239 Pro 深度思考从未生效（台账待办第 2 项）",
    # —— 定性为「设计上就零消费」——
    # 编排结果的 6 个字段是给调用方的结果快照；TUI 与无界面都走
    # **事件投影**（Projection.accept(事件)），不读结果对象。
    "终态阶段": "事件投影路线：界面不读结果对象（tui/headless 走 Projection）",
    "走过的门禁": "事件投影路线：界面不读结果对象",
    "动过的角色": "事件投影路线：界面不读结果对象",
    "出现过的成员状态": "事件投影路线：界面不读结果对象",
    "成果标注": "事件投影路线：界面不读结果对象",
    "已完成调用": "事件投影路线：界面不读结果对象",
    # —— 其余：测试探针 / 内容包消费 / 端口签名 / 内部结构 ——
    "built_with": "测试探针：FakeRetriever 记录建库入参（D-190）",
    "duty": "角色职责文案，内容包消费（不进 .py）",
    "实测写法": "探测结论备用字段，D-051 重试时才分叉（当前无重试）",
    "finish_reason": "端口签名要求：适配器回传，实现方在 http_llm",
    "raw_error_code": "端口签名要求：透传后端原始错误码",
    "raw_finish_reason": "端口签名要求：透传后端原始停止原因",
    "secrets": "PortRegistry 字段齐全性要求（架构第 2 步判据）",
    "描述": "装载条件的人读说明，调试时看；不进 .py 消费",
    "产物全文": "装配请求的可选全文，默认不传（省 token，D-189）",
    "词表": "倒排内部结构：构建期用完即弃，非对外数据",
    # —— D-267 引入的跨层中转，**确实被消费但消费点在别的类** ——
    # `编排器.工具层` 由 `_过程.__init__` 读走（`self.工具层 = 编排.工具层`），
    # 真正用它的 `_跑工具` 挂在 `_过程` 上。
    # 检查只看类内 self 链，故报「类内从未读取」——这是已知的检查盲点
    # （同 D-265 的 @property 盲点），不是零消费。
    "工具层": "D-267 跨层中转：由 _过程.__init__ 读走（见 D-265 已知盲点）",
}


def _字段(tree: ast.AST) -> list[tuple[str, str]]:
    """抽出 dataclass 字段（`AnnAssign` 于 ClassDef 内）。"""
    出: list[tuple[str, str]] = []
    for 类 in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for 项 in 类.body:
            if isinstance(项, ast.AnnAssign) and isinstance(项.target, ast.Name):
                名 = 项.target.id
                if not 名.startswith("_"):
                    出.append((类.name, 名))
    return 出


def _消费索引() -> dict[str, int]:
    """**一次扫描**建「被读取的属性名 → 次数」索引。

    ## 为什么不按字段逐个扫

    首版 `_消费点(名字)` 对每个字段都重扫全仓，
    80 文件 × N 字段 → 超过 60 秒超时。
    改为扫一遍收集全部 `Load` 上下文的属性访问，再计数。

    ## 只认 `Load`，不认 `Store`

    `x.字段 = v` 是写入不是消费。混进来就等于
    「声明过就算见过」，与 D-247 第一版的坑相同。

    ## **本脚本的已知盲点（D-265 复核时查出）**

    只认 `Load` 上下文的 `ast.Attribute`，故**看不见两条路径**：

    1. **`@property` 返回对象**——如 `runner.审计` 返回整个 `审计器`。
       方法返回不是字段读取，脚本判「零消费」而实际**有对外出口**。
    2. **序列化消费**——`state_codec` 显式写字段，
       属被动消费（数据存下来了，不等于功能被用）。

    **故「脚本报零消费」不等于「零消费」。**
    必须手工补查：① 该类有没有返回自身/他对象的 property；
    ② 该类型进没进 state。两者都不是本脚本能回答的。
    """
    索引: dict[str, int] = {}
    for p in sorted(源码.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for 节点 in ast.walk(tree):
            if isinstance(节点, ast.Attribute) and isinstance(节点.ctx, ast.Load):
                索引[节点.attr] = 索引.get(节点.attr, 0) + 1
    return 索引


def main() -> int:
    索引 = _消费索引()
    候选: list[tuple[str, str, str]] = []
    for p in sorted(源码.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for 类名, 字段名 in _字段(tree):
            if 字段名 in 白名单:
                continue
            if 索引.get(字段名, 0) == 0:
                候选.append((p, 类名, 字段名))

    if 候选:
        print("数据字段消费检查：以下字段在全仓**无任何读取点**——")
        print("**这些是候选，必须逐条回调用链定性**")
        print("（合法零消费：仅序列化用、端口签名要求、内容包消费）")
        print()
        print("⚠ **已知盲点（D-265）**：本脚本看不见 @property 返回对象")
        print("  与序列化消费。「报零消费」≠「零消费」，须手工补查这两条。\n")
        for p, 类名, 名 in 候选:
            if 名 in 已立案:
                print(f"  - {p.relative_to(根)} :: {类名}.{名}（已立案 {已立案[名]}）")
            elif 名 in 白名单:
                print(f"  - {p.relative_to(根)} :: {类名}.{名}（白名单：{白名单[名]}）")
            else:
                print(f"  - {p.relative_to(根)} :: {类名}.{名}  ← **未定性，需回调用链核实**")
        新增 = [c for c in 候选 if c[2] not in 已立案 and c[2] not in 白名单]
        print(f"\n共 {len(候选)} 个候选，其中 {len(新增)} 个未定性。")
        if 新增:
            print("**存在未定性候选——必须逐条回调用链核实后再决定修或豁免。**")
            return 1
        print("全部候选均已定性（已立案或有白名单理由）。")
        return 0

    print("数据字段消费检查通过：所有领域字段都有生产读取点。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
