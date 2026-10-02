"""编排层（L5，第 13 步）。

## 这一层在架构里的位置（§8.1）

```
Kernel（纯同步状态机） → 事件流 → TUI / 无界面 / 测试 driver / doctor
```

**编排层不知道界面存在**（D-037）。三种接入方式共用同一份驱动循环，
差别只在投影。本步只做「无界面 + 测试 driver」两端——
TUI 属第 16 步，`drive()` 已经能跑，这里不另写一套。

## 档位差异全在数据里（D-067）

人数、门禁是否适用、是否做政策检索、是否允许重写——
全在 `TierSpec` 里。**本模块只读这些字段，不做「如果档位是 Flash 就…」的判断**。

`test_档位人数改了编排不认得` 就是这条的判据：
改 `TierSpec.roster` 应当直接改变编排行为；
若编排里写死了人数，那条判据立刻红。

**唯一一处按档位分支的代码是「阶段序列」**——
咨询档不走成文那条线（§8.3），这是**业务分叉**不是参数差异，
故查 `is_consult` 而不是查 `len(roster)`：
人数和数据都可能被改，而「咨询不走成文线」是架构硬写的。

## 本模块不做什么

- **不做门禁判定**（第 14 步）：这里只负责「走到 GATING 并把结果读出来」
- **不做导出**（第 15 步）
- **不碰界面**（第 16 步）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..domain.errors import Result
from ..domain.gate import FORMAL_GATES, GATES, GateId
from ..domain.ids import RunId
from ..domain.phase import PhaseId
from ..domain.role import RoleId, RoleState
from ..domain.run_state import RunState, new_run_state
from ..domain.task import TaskSpec
from ..domain.tier import TIER_BY_ID, TierId
from ..domain.validation import g3降级标注, validate_task
from ..ports.llm import LlmPort, LlmRequest, Message
from ..ports.storage import StoragePort
from ..ports.validators import ValidatorPort
from .machine import 允许流转, 该发请求

#: 咨询档不走成文线（§8.3）：仅范正章产出专家答复。
咨询序列: Final[tuple[PhaseId, ...]] = (
    PhaseId.INTAKE,
    PhaseId.DIRECTION,
    PhaseId.OUTLINE,
    PhaseId.DRAFTING,
    PhaseId.FINAL,
    PhaseId.DONE,
)

#: Flash / Pro 的成文线。
成文序列: Final[tuple[PhaseId, ...]] = (
    PhaseId.INTAKE,
    PhaseId.DIRECTION,
    PhaseId.OUTLINE,
    PhaseId.DRAFTING,
    PhaseId.GATING,
    PhaseId.FINAL,
    PhaseId.DONE,
)


#: 本步的运行号。**编排层不生成运行号**——
#: 运行号由接入层给定（续跑时要沿用旧号，D-065），
#: 这里只是「自测跑一次」的默认值。
_默认运行号 = RunId("r-0001")


@dataclass(frozen=True, slots=True)
class 编排请求:
    """跑一次的输入。**全显式**——不读环境、不读全局状态。"""

    档位: TierId
    需求: str
    #: 已就绪的成员状态。测试可注入中间态；生产留空从 IDLE 起。
    初始状态: RunState | None = None


@dataclass(frozen=True, slots=True)
class 产物:
    """一次阶段产出的落地记录。**内容非空是判据，故单独成类**——
    直接用 `tuple[str, str]` 会让「有名字但内容空」也长得像成功。"""

    阶段: PhaseId
    名称: str
    内容: str


@dataclass(frozen=True, slots=True)
class 编排结果:
    """一次跑完的结果。**过程可查**是本步的核心要求——
    「跑通了」而不留痕，等于没法验证是不是空转。"""

    终态阶段: PhaseId
    完整阶段序列: tuple[PhaseId, ...]
    产物: tuple[产物, ...]
    交付物: str
    走过的门禁: tuple[GateId, ...]
    动过的角色: tuple[RoleId, ...]
    出现过的成员状态: tuple[RoleState, ...]
    成果标注: tuple[str, ...] = ()

    def 该段产物(self, 阶段: PhaseId) -> tuple[产物, ...]:
        return tuple(p for p in self.产物 if p.阶段 is 阶段)


def 阶段序列(档: TierId) -> tuple[PhaseId, ...]:
    """该档走的阶段线。**查 `is_consult` 不查人数**（见模块文档）。"""
    return 咨询序列 if TIER_BY_ID[档].is_consult else 成文序列


def 跑一次(
    请求: 编排请求,
    *,
    llm: LlmPort,
    storage: StoragePort,
    validators: ValidatorPort | None = None,
) -> Result[编排结果]:
    """把一次任务从入口跑到交付。

    ## 为什么用**脚本化假 LLM** 就能验全流程（§8.1）

    流程正确性与模型聪不聪明无关——**编排层只关心
    「该问谁、问了拿到什么、拿到后写哪、然后进哪一段」**。
    用假 LLM 跑通，才能在零网络零成本下验证全流程状态迁移；
    换成真模型只会引入不确定性，却验不出更多编排逻辑。

    真模型的接入在 `build_app_from_workspace`，那条路径已有测试（第 6 步）。
    """
    档 = TIER_BY_ID[请求.档位]

    任务 = TaskSpec(tier=请求.档位, requirement=请求.需求)
    合法 = validate_task(任务)
    if 合法.is_error:
        err = 合法.error
        assert err is not None
        return Result.fail(err.code, err.detail)

    序列 = 阶段序列(请求.档位)
    状态 = 请求.初始状态 or new_run_state(_默认运行号, 任务)

    全部产物: list[产物] = []
    走过门禁: list[GateId] = []
    出现过的状态: list[RoleState] = []
    动过的: list[RoleId] = []
    回复: dict[RoleId, str] = {}
    需求 = 请求.需求
    终稿 = ""

    for 阶段 in 序列:
        for 角色 in _该段角色(档.id, 阶段):
            状态 = _流转(状态, 角色, RoleState.RUNNING, 出现过的状态)
            # **D-042 铁律一**：发请求前必查状态。
            # 这一行是「挂起不发请求」唯一的执行点——
            # 规则散落各处就会有一处忘了。
            if not 该发请求(状态.role_states.get(角色, RoleState.IDLE)):
                continue
            # **真调模型**。第一版这里只 `动过的.append(角色)`，
            # 于是 `llm` 参数从头到尾没人用——**编排层一次都没真问过模型**
            # 却能把三档「跑通」，测试全绿。
            # 这就是 D-165 的第八次：**假件接上了，衔接没接上**。
            #
            # 判据见 `test_编排层真的问过模型`。
            问 = _构造请求(角色, 阶段, 需求)
            答 = llm.complete(问)
            if 答.is_error:
                err = 答.error
                assert err is not None
                return Result.fail(err.code, err.detail)
            回复[角色] = 答.unwrap().text
            动过的.append(角色)

        if 阶段 is PhaseId.DONE:
            break

        if 阶段 is PhaseId.GATING:
            门禁 = _跑门禁(档.id, validators)
            if 门禁.is_error:
                err = 门禁.error
                assert err is not None
                return Result.fail(err.code, err.detail)
            走过门禁.extend(门禁.unwrap())
            # **门禁段也落产物**（门禁报告）。第一版这里 `continue` 掉了，
            # 于是 `test_阶段序列逐段可查` 红——而那条判据是对的：
            # 一段跑过却什么都不产出，用户和排查者都无从判断它干了什么。
            报告 = 产物(
                阶段=PhaseId.GATING,
                名称="gate-report.md",
                内容=_产内容(PhaseId.GATING, 档.id, 需求, 回复)
                + "\n"
                + "\n".join(f"- {g}" for g in 走过门禁),
            )
            全部产物.append(报告)
            落盘(storage, 状态, 报告)
            continue

        件 = 产物(阶段=阶段, 名称=f"{阶段.value}.md", 内容=_产内容(阶段, 档.id, 需求, 回复))
        全部产物.append(件)
        落盘(storage, 状态, 件)
        if 阶段 is PhaseId.DRAFTING:
            终稿 = 件.内容

        for 角色 in _该段角色(档.id, 阶段):
            状态 = _流转(状态, 角色, RoleState.DONE, 出现过的状态)

    标注 = g3降级标注(请求.档位)
    if 标注:
        终稿 = f"{终稿}\n\n" + "\n".join(标注)
        全部产物.append(产物(阶段=PhaseId.FINAL, 名称="g3-降级标注.md", 内容="\n".join(标注)))

    return Result.ok(
        编排结果(
            终态阶段=PhaseId.DONE,
            完整阶段序列=序列,
            产物=tuple(全部产物),
            交付物=终稿 or "（本档不产出正文）",
            走过的门禁=tuple(走过门禁),
            动过的角色=tuple(dict.fromkeys(动过的)),
            出现过的成员状态=tuple(dict.fromkeys(出现过的状态)),
            成果标注=标注,
        )
    )


def _运行号() -> str:
    from ..domain.ids import RunId

    return RunId("r-0001")


def _该段角色(档: TierId, 阶段: PhaseId) -> tuple[RoleId, ...]:
    """该阶段该动谁。**从编制里查，不是另列一张表**——
    另列表就会与 `TierSpec.roster` 分叉，而分叉之后没人会发现。"""
    编制 = TIER_BY_ID[档].roster
    if 阶段 in (PhaseId.DIRECTION, PhaseId.FINAL):
        return (RoleId.TEAM_LEAD,) if RoleId.TEAM_LEAD in 编制 else ()
    if 阶段 is PhaseId.OUTLINE:
        return (RoleId.STRUCTURE,) if RoleId.STRUCTURE in 编制 else ()
    if 阶段 is PhaseId.DRAFTING:
        # §8.3：Phase 1 起草是辞达意；**Phase 2 格式壳 + 合规标注是骆准绳**。
        # 第一版只派了辞达意，于是 Pro 档骆准绳虽在编制内却从不露面——
        # 而他一人担 G1/G2/G7/G10/G11 五道门禁的取证。
        # **编制内却不当值 = 门禁静默无人取证，比不做门禁更危险**（D-049）。
        返回 = []
        if RoleId.LANGUAGE in 编制:
            返回.append(RoleId.LANGUAGE)
        if RoleId.COMPLIANCE in 编制:
            返回.append(RoleId.COMPLIANCE)
        return tuple(返回)
    if 阶段 is PhaseId.INTAKE:
        return (RoleId.TEAM_LEAD,)
    if 阶段 is PhaseId.GATING:
        # **G3 由文择体取证（§8.3 并行第 4 组）**。
        # 第一版这里返回空，于是 Pro 档文择体虽然「在编制内」却从没被派发——
        # 而 D-049 配平铁律要的正是「该门禁的取证方必须在编制内且真去取证」。
        # 编制内却不当值，等于门禁静默无人取证，比不做门禁更危险。
        #
        # **该段取证方 = 各门禁 owners 里在本档编制内的那些**，
        # 而不是另列一张名单——另列就会与 GATES 里的 owners 分叉。
        # 查编制而非查档位：Flash 不含文择体时文择体自然落空，
        # D-116 的降级标注才有意义（没人取证才需要降级）。
        编制内 = {g: s.owners for g, s in GATES.items() if s.owners}
        该当值 = {角色 for owners in 编制内.values() for 角色 in owners if 角色 in 编制}
        return tuple(sorted(该当值, key=lambda r: r.value))
    return ()


def _流转(状态: RunState, 角色: RoleId, 到: RoleState, 出现: list[RoleState]) -> RunState:
    """流转成员状态，返回**新**状态。

    ## 第一版这里有个隐蔽的 bug

    写成 `def _流转(角色, 到, 出现, 状态) -> None` 且只往 `出现` 里 append——
    `RunState` 是 frozen，**不回写就等于没流转**。
    于是第二次流转时 `当前` 仍是 `IDLE`，
    `IDLE → DONE` 非法被判下，`出现过的成员状态` 里永远只有 `RUNNING`。

    判据抓到了它（`test_成员状态被真实流转过`），
    而读代码看不出来——`出现.append(到)` 那一行看着完全正常。

    **教训：函数签名里的 `RunState` 参数是只读的还是读写的，
    名字与类型都看不出来，只有行为会告诉你。**
    """
    当前 = 状态.role_states.get(角色, RoleState.IDLE)
    if not 允许流转(当前, 到):
        # 非法流转不抛：状态机是判定器不是校验器，
        # 「还没到那一步」不该变成崩溃。
        return 状态
    出现.append(到)
    return 状态.set_role_state(角色, 到)


def _跑门禁(档: TierId, validators: ValidatorPort | None) -> Result[tuple[GateId, ...]]:
    """跑门禁。**是否适用查数据**（`gates_apply`），不查档位名。

    第 14 步会把它换成真判定（含并行 4 组与退回重写）；
    本步只负责**走完这一段并把结果读出来**，
    门禁的详细语义不在第 13 步的范围内。
    """
    if not TIER_BY_ID[档].gates_apply:
        return Result.ok(())
    if validators is not None:
        got = validators.run_structure({"gate": str(GateId.G3), "evidence": ["占位"]})
        if got.is_error:
            err = got.error
            assert err is not None
            return Result.fail(err.code, err.detail)
    return Result.ok(FORMAL_GATES)


def _构造请求(角色: RoleId, 阶段: PhaseId, 需求: str) -> LlmRequest:
    """问一次模型。**带上角色与阶段**，否则模型不知道该干什么。"""
    return LlmRequest(
        model="",
        messages=(
            Message(role="system", content=f"你是{角色.value}，当前阶段 {阶段.value}。"),
            Message(role="user", content=需求),
        ),
    )


def _产内容(阶段: PhaseId, 档: TierId, 需求: str, 回复: dict[RoleId, str]) -> str:
    """阶段产出内容。

    **正文里带上需求原文**，不是「产出 N 号产物」——
    这样「产物落地」才真的证明模型被问了，
    而不是编排层凭空捏了一段文字塞进 storage。
    """
    段落 = [f"【{阶段.display_label}】依据需求「{需求[:40]}」形成的阶段产出。", f"档位：{档.value}"]
    for 角色, 文 in 回复.items():
        段落.append(f"—— {角色.value}：{文}")
    return "\n".join(段落)


def 落盘(storage: StoragePort, 状态: RunState, 件: 产物) -> None:
    """把产物写进 storage。**写失败不上抛**——
    本步的产物记录在内存里，写盘是第 15 步导出的事。"""
    storage.write_atomic(f"{状态.run_id}/{件.名称}", 件.内容)


__all__ = [
    "Message",
    "产物",
    "咨询序列",
    "成文序列",
    "编排结果",
    "编排请求",
    "跑一次",
    "阶段序列",
]
