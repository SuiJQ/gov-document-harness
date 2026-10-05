"""门禁取证实现（缺口见 `GATE_FORGE_TODO.md`）。

## 这模块填的是哪个洞

`orchestrate.py` 的门禁段把「该派谁去查哪几道」算得清清楚楚
（`GATES[].owners`、D-049 四组并行、主理人终闸），调度器
（`kernel/scheduler.run_groups`）也是现成的——**唯独没人真的去查**。
原先那个注入点是 `门禁取证 or (lambda _门禁: {})`，而 `app.py`
建编排器时压根不传它，于是 12 道门禁的结论恒为空。

空结论不是「没人发现问题」，是**没人查**。D-221 要求缺席即
升级用户决策，正是为了不让这种状态被当成「全部通过」。

## 为什么不能用校验器直接出结论（D-069）

两处明文禁止：
- `adapters/validators.py` 模块文档：校验器返回 `ValidationResult`，
  **不是** `GateVerdict`，「脚本输出不得作为门禁通过依据」。
- `content/references/gate-manual.md:447`：纯机械复核「只判字符串
  可定位性，不产出质量结论，**其输出不得作为本闸或任一 Gate 通过依据**」。

所以分工是：**结论出自角色（模型），结构校验只兜结构**。
`run_structure` 唯一的作用是「这道门禁的证据有没有」——
证据为空就不该判通过（D-034），这与「结论由谁下」是两回事。

## 输出契约在内容包里

行式格式（`门禁号 | 结论 | 证据：…`）写在
`content/references/gate-verdict-format.md`，由装配器**仅在 GATING 阶段**
装载（`装载条件.限阶段`）。代码不硬编码格式细节——
换内容包即换一套写作标准（D-055）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..domain.errors import Result
from ..domain.gate import GATES, GateId
from ..domain.role import RoleId
from ..domain.run_state import GateRecord
from ..domain.tier import TierId
from ..ports.validators import ValidationResult, ValidatorPort

#: 一行结论：`G1 | 通过 | 证据：……`
#:
#: **第三段可有可无**：模型写了结论却漏掉证据段是常事，
#: 若把第三段写成必需，那一行就整行不匹配——门禁从「答了但没查实」
#: 变成「压根没答」，D-221 会当缺席转用户决策。两者处置不同：
#: 前者该退回重写，后者该问人。**不区分就把该退回的转给了用户。**
#:
#: **刻意不用 JSON**：模型在长文本回复末尾经常把 JSON 截断，
#: 截断的 JSON 整组作废，12 道门禁就因为最后一行多写了一个逗号
#: 而全组失证。行式格式对残缺宽容——漏一行只丢一道门禁，
#: 而缺席本来就会被 D-221 抓出来，不会被悄悄放行。
#:
#: 结论词只认两个字（`通过`/`退回`）。这是刻意的：
#: 「基本通过」「大致合规」这类模糊词若也算通过，
#: 红线就能被措辞稀释（D-007 严禁编造 / D-034 无证据不通过）。
_结论行 = re.compile(
    r"^\s*(?P<门禁>G\d{1,2})\s*[|｜]\s*(?P<结论>通过|退回)"
    r"(?:\s*[|｜](?P<其余>.*))?$",
    re.MULTILINE,
)

#: 证据段的取值：`证据：……` 或 `原因：…… | 证据：……`
_证据段 = re.compile(r"证据[：:]\s*(?P<证据>.+?)\s*$", re.MULTILINE)

#: 证据太短判为无效：单个字、纯标点都不算可复核的定位（D-034）。
#:
#: 阈值 4 是实测定的：门禁证据写「第 3 段」是 4 个字符，
#: 「已核对」是 3 个字符——而后者正是要拦的那种敷衍表述。
_证据最小长度 = 4


@dataclass(frozen=True, slots=True)
class 门禁取证请求:
    """一次取证的输入。

    ## 为什么单独一个类型而不是三个位置参数

    原注入点签名是 `Callable[[tuple[GateId, ...]], dict[str, GateRecord]]`，
    **只收门禁编号，收不到任何证据**——而编排器在派发时明明
    已经把模型回覆存进了 `self.回复[取证方]`（`orchestrate.py`），
    调用那一行却没把它传下来。参数越少越容易漏传，
    且漏传后类型检查依然通过。打包成一个类型，漏传就变成构造失败。
    """

    门禁: tuple[GateId, ...]
    取证方: RoleId
    #: 该取证方在 GATING 阶段的模型回覆。**证据只从这来**。
    回覆: str
    档位: TierId


def 解析结论(回覆: str, 该答: tuple[GateId, ...], 取证方: RoleId) -> dict[str, GateRecord]:
    """把角色回覆解成逐门结论。**纯函数**：同样输入必得同样输出。

    ## 两道过滤，第二道才是 D-084 的本体

    1. `该答` 是编排器派给本组的门禁——挡「多答没派的」。
    2. `取证方 in GATES[g].owners`——**挡「替别人签字」**。

    只做第 1 道时，一个角色可以在回覆里顺带写 G7，
    而它根本不是 G7 的取证方；那段结论照样被采信、照样进台账。
    D-084 立映射就是为了防这个，所以**取证方必须参与判定**，
    不能只当个记录字段。

    ## 缺席者不给结论（而不是判通过）

    指派了 3 道门禁、模型只答了 2 道，那第 3 道**不出现在返回值里**。
    调用方（编排器 D-221 缺席判据）据此转用户决策。

    绝不能在此处补一条「通过」——那等于把「没查」写成「查过没问题」，
    正是 D-221 立账要挡的那件事。
    """
    合法 = {g.value for g in 该答 if 取证方 in GATES[g].owners}
    出: dict[str, GateRecord] = {}
    for m in _结论行.finditer(回覆):
        编号 = m.group("门禁")
        # **只认「派给它且该角色有权取证」的门禁**：
        # 模型多答别的门禁（幻觉/串门）直接丢。采信它等于让一个
        # 角色替别人的门禁签字，而 D-084 的取证方映射正是为了防这个。
        if 编号 not in 合法:
            continue
        # **重复作答取首条**：后写的无法证伪先写的，
        # 让程序自己挑等于替模型做裁决。
        if 编号 in 出:
            continue
        其余 = m.group("其余") or ""
        证 = _证据段.search(其余)
        证据 = 证.group("证据").strip() if 证 else ""
        通过 = m.group("结论") == "通过" and bool(证据) and len(证据) >= _证据最小长度
        出[编号] = GateRecord(
            gate=编号,
            passed=通过,
            evidence=(证据,) if 证据 else (),
            # **证据不足要写明原因**，否则 `rejection_reason` 空着，
            # 门禁报告里看不出是「查了没通过」还是「压根没查够」。
            rejection_reason=(
                "" if 通过 else (f"{编号} 证据不足：" + (其余.strip() or "未给出证据"))
            ),
        )
    return 出


def 核对结构(结论: dict[str, GateRecord], 校验器: ValidatorPort) -> dict[str, GateRecord]:
    """用结构校验器**兜结构**：证据为空的结论一律降级为不通过。

    ## 这里做与不做的

    **做**：无证据不通过（D-034）。校验器报出的锚点并入
    `rejection_reason`，让用户看到是哪个字段缺失。

    **不做**：拿校验结果去**判定通过**。`run_hard_constraint`
    查的 D-Card 零沉降是动笔前的闸，不是 G11 的通过依据
    （D-069 + gate-manual.md:447）。校验通过**不能**把
    一条不通过改成通过——那正是「机械复核产出质量结论」。
    """
    出 = dict(结论)
    for 编号, 记 in 出.items():
        if 记.passed:
            got = 校验器.run_structure({"gate": 编号, "evidence": list(记.evidence)})
            if got.is_ok and not got.unwrap().passed:
                问题 = "；".join(i.message for i in got.unwrap().blocking)
                出[编号] = GateRecord(
                    gate=编号,
                    passed=False,
                    evidence=记.evidence,
                    rejection_reason=f"{编号} 结构校验不通过：{问题}",
                )
    return 出


class _无校验器:
    """编排层没有注入校验器时的替身。

    ## 为什么不能用 `adapters.fakes.FakeValidators`

    编排层反向依赖适配层就是第十次 D-165（`orchestrate.py` 模块文档
    明令禁止）。故本模块自带一个**什么都不查**的实现。

    ## 为什么它返回 passed=True 而不是 False

    `核对结构` 只做**收紧**：校验报出问题才把已通过的结论降级。
    校验器缺失时若返回 False，等于「没装校验器所以全部门禁不通过」——
    那是拿缺工具冒充坏结论。故返回「无问题」，让收窄逻辑无处可收。

    反向的风险（校验通过就不管了）由 D-069 兜住：校验器结果
    **永远不能单独让门禁通过**，`核对结构` 里没有任何一处
    会因为校验通过而把 False 改成 True。
    """

    def run_hard_constraint(self, payload: dict[str, object]):  # noqa: ARG002
        return Result.ok(ValidationResult(script="hard_constraint", passed=True, issues=()))

    def run_structure(self, payload: dict[str, object]):  # noqa: ARG002
        return Result.ok(ValidationResult(script="structure", passed=True, issues=()))


def 门禁取证实装(校验器: ValidatorPort | None):
    """产出可直接注入编排器的取证函数。

    **无 `or {}` 兜底**：注入点缺了就是构造失败，
    不该退回一个「什么都没有」的实现——那正是本缺口本身的形状。
    """
    真校验 = 校验器 if 校验器 is not None else _无校验器()

    def 取证(请求: 门禁取证请求) -> dict[str, GateRecord]:
        裸 = 解析结论(请求.回覆, 请求.门禁, 请求.取证方)
        return 核对结构(裸, 真校验)

    return 取证
