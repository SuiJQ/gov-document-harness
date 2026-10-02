"""编排器：把前 11 层真正接起来（第一轮增量检查的修复）。

## 这个模块为什么存在

第 13 步交出的编排是**孤岛**：`bus`/`emit` 出现 0 次、
`call_id` 出现 0 次、无一处走装配根、发给模型的是手搓的两条 `Message`、
`model=""`。它自己测自己，35 条判据全绿——
而**与前 11 层没有一行代码相连**。

| 接缝 | 接在哪 |
|---|---|
| → 事件流 | 阶段边界发 `PHASE_STARTED`/`PHASE_EXITED`，由 `drive` 消费 |
| → 内核 | `取一步()` 返回 `drive` 要的 `step` 回调，**一次一阶段** |
| → 装配器 | 每次提问都过 `装配提示词`，含角色提示词正文与预算闸门 |
| → 幂等 | `call_id` 记账 + 续跑跳过已完成 |
| → 装配根 | `App.编排器()` 从 `build_app_from_workspace` 拿真件 |

## 依赖方向：编排层不 import 适配层

**这是硬约束**（D-165 的第十次教训）：
本模块只 import `ports` / `domain` / `kernel` / `services`，
真件由 `App.编排器()` 注入。
一旦出现 `from ..adapters.x import ...`，就是第十次 D-165。
`test_编排层不碰适配层` 把这条钉死。

唯一例外是 `_临时总线()` 里的 `Redactor`——
它**不是构造一个真件**，而是「没有总线时给个最小可用的」，
且该分支只在测试与无界面模式走。构造真总线仍由 `App` 负责。

## 为什么必须 `drive` 而不是 for 循环

架构 §8.1：三种接入方式**共用同一份驱动循环**（D-037）。
`for` 循环跑完，`drive` 就退化成空壳，而
**续跑（`resume_from`）与 TUI 的检查点插话全部失效**——
后者正是 D-012「插话在下一个检查点生效」的实现基础。

## 事件的载荷纪律

只有 `ARTIFACT_READY` 携全文（D-072/D-046）——
界面拿到的与落盘的是同一份。其余事件只放标量。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from ..domain.errors import ErrorCode, Result
from ..domain.event import Event, EventKind, Level, make_call_id
from ..domain.gate import FORMAL_GATES, GATES, GateId
from ..domain.ids import RunId
from ..domain.phase import PhaseId, PhaseState
from ..domain.role import RoleId, RoleState
from ..domain.run_state import RunState, new_run_state
from ..domain.task import TaskSpec
from ..domain.tier import TIER_BY_ID, TierId
from ..domain.validation import g3降级标注, validate_task
from ..kernel.bus import EventBus
from ..kernel.driver import Step, drive
from ..ports.content_pack import ContentPackPort
from ..ports.llm import LlmPort, LlmRequest, Message
from ..ports.storage import StoragePort
from ..ports.validators import ValidatorPort
from ..services.prompting import 装配提示词, 装配请求, 预算闸门
from .machine import 该发请求

#: 产物路径前缀。**必须以 `data/` 开头**——
#: `paths.AI_WRITABLE` 只认 `data/`（AI 只能写 `data/`），
#: 写成运行号开头会被路径守卫拒绝（第 13 步第九次 D-165 的教训）。
_产物根: Final = "data"

#: 默认预算（D-189）。真跑时应来自配置里的上下文容量；
#: 这里给保守值，**不给「无上限」**——无上限等于闸门不存在。
_默认预算: Final = 20_000


@dataclass(frozen=True, slots=True)
class 阶段产物:
    """一次阶段产出。**内容非空是判据**，故单独成类——
    用 `tuple[str, str]` 会让「有名字但内容空」也长得像成功。"""

    阶段: PhaseId
    名称: str
    内容: str


@dataclass(slots=True)
class 编排结果:
    """一次跑完的结果。**过程可查**——「跑通了」而不留痕等于没法验证空转。"""

    终态阶段: PhaseId
    完整阶段序列: tuple[PhaseId, ...]
    产物: tuple[阶段产物, ...]
    交付物: str
    走过的门禁: tuple[GateId, ...]
    动过的角色: tuple[RoleId, ...]
    出现过的成员状态: tuple[RoleState, ...]
    成果标注: tuple[str, ...] = ()
    已完成调用: tuple[str, ...] = ()

    def 该段产物(self, 阶段: PhaseId) -> tuple[阶段产物, ...]:
        return tuple(p for p in self.产物 if p.阶段 is 阶段)


class _过程:
    """推进过程的全部可变状态。**从编排器里拆出来**——
    否则 `step` 闭包要同时抓五六个变量，读代码时无从确认谁在改什么。"""

    def __init__(
        self,
        *,
        编排: 编排器,
        llm: LlmPort,
        storage: StoragePort,
        序列: tuple[PhaseId, ...],
        状态: RunState,
        初始已完成: tuple[str, ...],
    ) -> None:
        self.编排 = 编排
        self.llm = llm
        self.storage = storage
        self.序列 = 序列
        self.状态 = 状态
        self.已完成 = 初始已完成
        self.产物: list[阶段产物] = []
        self.门禁: list[GateId] = []
        self.出现过的状态: list[RoleState] = []
        self.动过的: list[RoleId] = []
        self.回复: dict[RoleId, str] = {}
        self.交付物 = ""
        self.标注: tuple[str, ...] = ()

    # ── drive 回调：一次一阶段 ──

    def 推进一步(self, 进度: int) -> Result[Step[int]]:
        if 进度 >= len(self.序列):
            return Result.ok(Step(phase=PhaseId.DONE, state=进度, done=True))
        if self.序列[进度] is PhaseId.DONE:
            # **DONE 不是阶段，是终点**。
            #
            # 第一版只写了 `进度 >= len(序列)` 那一条，而 DONE **在序列里**，
            # 于是进度 6 < 长度 7 时它被当成普通阶段走完整流程：
            # 落盘一个 `done.md`、把状态存成 `phase=done`。
            #
            # 后果：续跑读到的是「已完成」，而**最后一段 FINAL 的状态被覆盖**——
            # 而 FINAL 恰恰是交付前的那段。查出来的现象是
            # 「读回的是 done」，第一眼看着像判据写错。
            #
            # 旧 `跑一次` 里有 `if 阶段 is DONE: break`，
            # 重写成 `drive` 分步时**那一条没跟过来**。
            return Result.ok(Step(phase=PhaseId.DONE, state=进度, done=True))

        阶段 = self.序列[进度]
        self.编排.发(EventKind.PHASE_STARTED, 阶段)

        跑段 = self._跑一段(阶段)
        if 跑段.is_error:
            self.编排.发(EventKind.PHASE_EXITED, 阶段, ok=False)
            段错 = 跑段.error
            assert 段错 is not None
            return Result.fail(段错.code, 段错.detail)

        件 = 跑段.unwrap()
        存 = self.storage.write_atomic(self._路径(件.名称), 件.内容)
        if 存.is_error:
            self.编排.发(EventKind.PHASE_EXITED, 阶段, ok=False)
            写错 = 存.error
            assert 写错 is not None
            return Result.fail(写错.code, f"{件.名称} 落盘失败：{写错.detail}")

        self.产物.append(件)
        # **只有 ARTIFACT_READY 携全文**（D-072/D-046）
        self.编排.发(
            EventKind.ARTIFACT_READY,
            阶段,
            name=件.名称,
            artifact_kind="note",
            content=件.内容,
        )
        if 阶段 is PhaseId.FINAL:
            self.交付物 = 件.内容

        存态 = self.storage.save_run_state(self.状态.with_phase(阶段, PhaseState.APPROVED))
        if 存态.is_error:
            self.编排.发(EventKind.PHASE_EXITED, 阶段, ok=False)
            存错 = 存态.error
            assert 存错 is not None
            return Result.fail(存错.code, f"状态落盘失败：{存错.detail}")

        self.编排.发(EventKind.PHASE_EXITED, 阶段, ok=True)
        return Result.ok(Step(phase=阶段, state=进度 + 1))

    def _跑一段(self, 阶段: PhaseId) -> Result[阶段产物]:
        """问该问的人，产出一件东西。

        **幂等在此**：同一阶段的同一角色，`call_id` 不变，
        续跑时已完成的部分直接跳过、不重复问模型（D-074）。
        """
        if 阶段 is PhaseId.DONE:
            return Result.ok(阶段产物(阶段, f"{阶段.value}.md", "（终态，无产出）"))

        档 = self.状态.task.tier
        if 阶段 is PhaseId.GATING:
            # **门禁段也必须派发取证方**。
            #
            # 第一版在这里提前 return，GATING 段压根不派任何人——
            # 于是文择体（G3 取证）在 Pro 档**编制内却不当值**，
            # 门禁静默无人取证，比不做门禁更危险（D-049）。
            #
            # **这是第 13 步那个缺陷的同型复发**：那次是 `for` 循环
            # 没覆盖 GATING，这次是重写成提前 return 时又漏了。
            # 判据 `test_编制内必须当值` 在迁移后才暴露它。
            派 = self._派发(档, 阶段)
            if 派.is_error:
                return _转为失败(派, "门禁段派发失败")
            门禁 = _该跑门禁(档)
            self.门禁.extend(门禁)
            return Result.ok(阶段产物(阶段, "gate-report.md", _门禁报告(self.门禁)))

        派 = self._派发(档, 阶段)
        if 派.is_error:
            return _转为失败(派, f"{阶段.value} 段派发失败")
        return Result.ok(阶段产物(阶段, f"{阶段.value}.md", _正文(阶段, self.回复)))

    def _派发(self, 档: TierId, 阶段: PhaseId) -> Result[None]:
        """按编制派发该段该动的人，问模型，记账。**幂等在此**。

        抽成方法是因为 GATING 段与普通段都要派发，
        写两遍必然有一处漏——**这次漏的就是 GATING**。
        """
        for 角色 in _该段角色(档, 阶段):
            self.出现过的状态.append(RoleState.RUNNING)
            # **状态必须写回 `RunState`**，不只是记一份「出现过」的列表。
            # 迁移时漏了这一步，于是存盘的状态里 `role_states` 全是 IDLE——
            # 而 D-042 的六态机是「成员状态要能查」的硬约束，
            # 存回去等于没发生过。
            self.状态 = self.状态.set_role_state(角色, RoleState.RUNNING)
            if not 该发请求(RoleState.RUNNING):
                continue
            cid = make_call_id(
                "prompt", str(self.编排.运行号), 阶段.value, role=角色.value
            )
            if cid not in self.已完成:
                问 = self._构造请求(角色, 阶段)
                答 = self.llm.complete(问)
                if 答.is_error:
                    错 = 答.error
                    assert 错 is not None
                    return Result.fail(
                        错.code, f"{阶段.value}/{角色.value} 调用失败：{错.detail}"
                    )
                self.回复[角色] = 答.unwrap().text
                self.已完成 = (*self.已完成, cid)
            self.动过的.append(角色)
            self.出现过的状态.append(RoleState.DONE)
            self.状态 = self.状态.set_role_state(角色, RoleState.DONE)
        return Result.ok_none()

    def _构造请求(self, 角色: RoleId, 阶段: PhaseId) -> LlmRequest:
        """**走第 11 步的装配器**——不再手搓 Message。

        没有内容包时**不发一句空话冒充角色提示词**：
        第 13 步那版发的是「你是 xxx，当前阶段 yyy」，
        等于没给任何写作标准，而判据查不出来。
        故明确标注「这是降级提示」，让日志与判据都能看见。
        """
        需求 = self.状态.task.requirement
        if self.编排.内容包 is not None:
            装配 = 装配提示词(
                请求=装配请求(角色=角色, 产物=(f"{阶段.value}.md",)),
                内容包=self.编排.内容包,
                闸门=预算闸门(上限=self.编排.预算),
            )
            消息 = 装配.消息
        else:
            消息 = (
                Message(
                    role="system",
                    content=(
                        f"[内容包未接入] 你{角色.value}，当前阶段 {阶段.value}。"
                        "**这是降级提示，不是完整角色提示词。**"
                    ),
                ),
                Message(role="user", content=需求),
            )
        请求 = LlmRequest(model=self.编排.模型名, messages=消息)
        self.编排.发出的请求.append(请求)
        return 请求

    def _路径(self, 名称: str) -> str:
        return f"{_产物根}/{self.编排.运行号}/{名称}"

    def 收结果(self) -> 编排结果:
        档 = self.状态.task.tier
        self.标注 = g3降级标注(档)
        if self.标注:
            self.交付物 = f"{self.交付物}\n\n" + "\n".join(self.标注)
            self.产物.append(
                阶段产物(阶段=PhaseId.FINAL, 名称="g3-降级标注.md", 内容="\n".join(self.标注))
            )
        return 编排结果(
            终态阶段=PhaseId.DONE,
            完整阶段序列=self.序列,
            产物=tuple(self.产物),
            交付物=self.交付物 or "（本档不产出正文）",
            走过的门禁=tuple(self.门禁),
            动过的角色=tuple(dict.fromkeys(self.动过的)),
            出现过的成员状态=tuple(dict.fromkeys(self.出现过的状态)),
            成果标注=self.标注,
            已完成调用=self.已完成,
        )


class 编排器:
    """一次编排的驱动器。**不碰文件系统、不 new 适配层**——
    端口从构造参数进来（依赖倒置，架构 §3.4）。

    ## 为什么是类而不是函数

    `drive` 要求 `step: Callable[[StateT], Result[Step[StateT]]]`，
    而 `step` 要携带总线、端口、记账。
    用函数就得塞进闭包或全局变量——
    前者读不懂，后者正是本项目反复否掉的「隐藏的第二状态源」。

    ## `总线` 缺省时的行为

    构造一个**临时总线**并在跑完后关闭它。
    不用「无总线就不发事件」——那会让测试与无界面模式看不到事件流，
    而事件流正是本轮要接的缝之一。
    """

    def __init__(
        self,
        *,
        llm: LlmPort,
        storage: StoragePort,
        bus: EventBus | None = None,
        content_pack: ContentPackPort | None = None,
        validators: ValidatorPort | None = None,
        模型名: str = "",
        预算: int = _默认预算,
        初始状态: RunState | None = None,
        运行号: RunId | None = None,
        初始已完成: tuple[str, ...] = (),
    ) -> None:
        self.llm = llm
        self.storage = storage
        self.bus = bus
        self.内容包 = content_pack
        self.validators = validators
        self.模型名 = 模型名
        self.预算 = 预算
        self.初始状态 = 初始状态
        self.运行号 = 运行号 or RunId("r-0001")
        self.初始已完成 = 初始已完成

        #: **发出去过的请求**。刻意保留而非丢掉——
        #: 「模型到底收到了什么」是最难查的问题之一。
        self.发出的请求: list[LlmRequest] = []
        self.事件: list[Event] = []
        self.进度终值: int | None = None
        self.终态: RunState | None = None
        #: `跑到终` 建的推进过程。**显式初始化为 None**——
        #: 让类型检查器知道它一定有值，而不是靠 `__init__` 里没赋值。
        self._过程: _过程 | None = None

    def 发(self, kind: EventKind, 阶段: PhaseId, **载荷: object) -> None:
        """发一条事件。**本地留一份**，无论有没有总线——
        否则测试与无界面模式看不到事件流。
        """
        事件 = Event(
            seq=len(self.事件) + 1,
            kind=kind,
            run_id=self.运行号,
            phase=阶段.value,
            level=Level.INFO,
            data=载荷,
        )
        self.事件.append(事件)
        if self.bus is not None:
            self.bus.emit(事件)

    def 取一步(self) -> Callable[[int], Result[Step[int]]]:
        """返回 `drive` 要的 `step` 回调。**一次一阶段**。

        若一步跑完全部阶段，`drive` 就是空壳，
        而续跑与 TUI 检查点插话会全部失效（D-037/D-012）。

        **必须先 `建过程`**——`取一步` 单独用时（如测试 drive 的分段性）
        不会经过 `跑到终`，故这里就地建一次。
        """
        return self._建过程().推进一步

    def _建过程(self) -> _过程:
        """建立（或复用）推进过程。**幂等**——
        `取一步` 与 `跑到终` 都要它，故不能建两次。
        """
        if self._过程 is not None:
            return self._过程
        任务 = TaskSpec(tier=TierId.PRO, requirement="")
        self._过程 = _过程(
            编排=self,
            llm=self.llm,
            storage=self.storage,
            序列=成文序列,
            状态=new_run_state(self.运行号, 任务),
            初始已完成=self.初始已完成,
        )
        return self._过程

    def 跑到终(self, *, 档位: TierId, 需求: str) -> Result[编排结果]:
        """用 `drive` 跑到底。**这是唯一入口**——
        没有另一条「一口气跑完」的路，否则 `drive` 会被绕过。
        """
        任务 = TaskSpec(tier=档位, requirement=需求)
        合法 = validate_task(任务)
        if 合法.is_error:
            校验错 = 合法.error
            assert 校验错 is not None
            return Result.fail(校验错.code, 校验错.detail)

        序列 = 阶段序列(档位)
        状态 = self.初始状态 or new_run_state(self.运行号, 任务)
        # **强制重建**：同一编排器跑两次时，第二次不能沿用上次的进度。
        self._过程 = _过程(
            编排=self,
            llm=self.llm,
            storage=self.storage,
            序列=序列,
            状态=状态,
            初始已完成=self.初始已完成,
        )
        self.进度终值 = None

        借来的总线 = self.bus is None
        总线 = self.bus or _临时总线()
        self.发(EventKind.RUN_STARTED, PhaseId.INTAKE, tier=档位.value)
        got = drive(
            run_id=self.运行号,
            bus=总线,
            initial_state=0,
            step=self._过程.推进一步,
        )
        if 借来的总线:
            总线.stop()
        if got.is_error:
            错 = got.error
            assert 错 is not None
            self.发(EventKind.RUN_FAILED, PhaseId.DONE, detail=错.code.value)
            return Result.fail(错.code, 错.detail)
        总成 = got.unwrap()
        if not 总成.ok:
            self.发(EventKind.RUN_FAILED, PhaseId.DONE, detail=总成.detail)
            return Result.fail(ErrorCode.KERNEL_STALLED, 总成.detail)
        self.进度终值 = 总成.steps
        已存 = self.storage.load_run_state(self.运行号)
        self.终态 = 已存.unwrap() if 已存.is_ok else 状态
        self.发(EventKind.RUN_FINISHED, PhaseId.DONE)
        return Result.ok(self._建过程().收结果())


# ── 阶段线 ──

咨询序列: Final[tuple[PhaseId, ...]] = (
    PhaseId.INTAKE,
    PhaseId.DIRECTION,
    PhaseId.OUTLINE,
    PhaseId.DRAFTING,
    PhaseId.FINAL,
    PhaseId.DONE,
)
成文序列: Final[tuple[PhaseId, ...]] = (
    PhaseId.INTAKE,
    PhaseId.DIRECTION,
    PhaseId.OUTLINE,
    PhaseId.DRAFTING,
    PhaseId.GATING,
    PhaseId.FINAL,
    PhaseId.DONE,
)


def 阶段序列(档: TierId) -> tuple[PhaseId, ...]:
    """该档走的阶段线。**查 `is_consult` 不查人数**（D-206）。"""
    return 咨询序列 if TIER_BY_ID[档].is_consult else 成文序列


def _转为失败(错: Result[None], 前缀: str) -> Result[阶段产物]:
    """把 `Result[None]` 的失败转成 `Result[阶段产物]` 的失败。

    **不能直接 `return 派`**——`Result` 不协变，
    `Result[None]` 不是 `Result[阶段产物]`。
    写 `return 派` 运行时没问题（只用到 `.is_error` / `.error`），
    而类型检查器会拦；这里显式转换，**顺手把环节名写进 detail**，
    排查时能直接看出是哪一段派发出问题。
    """
    内 = 错.error
    assert 内 is not None
    return Result.fail(内.code, f"{前缀}：{内.detail}")


def _该跑门禁(档: TierId) -> tuple[GateId, ...]:
    """该档应跑的门禁。

    **本步仍不是真判定**——真门禁属第 14 步（并行 4 组 / 退回重写）。
    这里查 `gates_apply`（数据，不是档位名），咨询档自然落空。
    """
    return FORMAL_GATES if TIER_BY_ID[档].gates_apply else ()


def _该段角色(档: TierId, 阶段: PhaseId) -> tuple[RoleId, ...]:
    """该阶段该动谁。**从编制里查**，不另列表——
    另列就会与 `TierSpec.roster` 分叉，而分叉之后没人会发现。
    """
    编制 = TIER_BY_ID[档].roster
    if 阶段 is PhaseId.INTAKE:
        return (RoleId.TEAM_LEAD,) if RoleId.TEAM_LEAD in 编制 else ()
    if 阶段 in (PhaseId.DIRECTION, PhaseId.FINAL):
        return (RoleId.TEAM_LEAD,) if RoleId.TEAM_LEAD in 编制 else ()
    if 阶段 is PhaseId.OUTLINE:
        return (RoleId.STRUCTURE,) if RoleId.STRUCTURE in 编制 else ()
    if 阶段 is PhaseId.DRAFTING:
        # §8.3：Phase 1 起草是辞达意，Phase 2 格式壳是骆准绳。
        return tuple(r for r in (RoleId.LANGUAGE, RoleId.COMPLIANCE) if r in 编制)
    if 阶段 is PhaseId.GATING:
        # 该段取证方 = 各门禁 owners 里在本档编制内的那些，
        # 从 GATES 现查，不另列名单。
        该当值 = {角色 for 规 in GATES.values() if 规.owners for 角色 in 规.owners if 角色 in 编制}
        return tuple(sorted(该当值, key=lambda r: r.value))
    return ()


def _正文(阶段: PhaseId, 回复: dict[RoleId, str]) -> str:
    段 = [f"【{阶段.display_label}】"]
    for 角色, 文 in 回复.items():
        段.append(f"—— {角色.value}：{文}")
    return "\n".join(段)


def _门禁报告(门禁: list[GateId]) -> str:
    return "\n".join(["【门禁检查】", *(f"- {g.value} {GATES[g].name}" for g in 门禁)])


def _临时总线() -> EventBus:
    """无总线时的最小可用总线。**不是「构造真件」**——
    真实总线由 `App` 装配根持有并注入。
    """
    from ..kernel.redactor import Redactor

    return EventBus(Redactor())
