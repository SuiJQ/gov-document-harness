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
from dataclasses import dataclass, replace
from threading import Lock
from typing import Final

from ..domain.errors import ErrorCode, Result
from ..domain.event import Event, EventKind, Level, make_call_id
from ..domain.gate import (
    FORMAL_GATES,
    GATES,
    GateId,
    处置,
    并行门禁组,
    终闸组,
    退回上限,
    门禁判定,
)
from ..domain.ids import RunId
from ..domain.phase import PhaseId, PhaseState
from ..domain.role import RoleId, RoleState, display_name, 取温度
from ..domain.run_state import GateRecord, RunState, new_run_state
from ..domain.task import TaskSpec
from ..domain.tier import TIER_BY_ID, TierId
from ..domain.validation import g3降级标注, validate_task
from ..kernel.bus import EventBus
from ..kernel.driver import Step, drive
from ..kernel.scheduler import GateGroup, run_groups
from ..ports.content_pack import ContentPackPort
from ..ports.llm import LlmPort, LlmRequest, LlmResponse, Message, ToolCall, ToolSchema
from ..ports.storage import StoragePort
from ..ports.validators import ValidatorPort
from ..services.context import 压缩, 组装, 需要压缩
from ..services.context.view import 估算视图
from ..services.prompting import 装配提示词, 装配请求, 预算闸门
from ..services.tools.calllog import 调用存档 as 调用存档类
from ..services.tools.runner import 工具运行器
from .machine import 该发请求

#: 产物路径前缀。**必须以 `data/` 开头**——
#: `paths.AI_WRITABLE` 只认 `data/`（AI 只能写 `data/`），
#: 写成运行号开头会被路径守卫拒绝（第 13 步第九次 D-165 的教训）。
#: **工具调用轮数上限**（D-267）。
#:
#: 模型可能一轮里连调多个工具，也可能据前一个结果再调后一个，
#: 故按上限迭代。**上限不是可选的**：连续调用可能永不停止，
#: 没有上限就是死循环（内核保险丝同族，架构 §5.2）。
_工具轮上限 = 4

_产物根: Final = "data"

#: 默认预算（D-189）。真跑时应来自配置里的上下文容量；
#: 这里给保守值，**不给「无上限」**——无上限等于闸门不存在。
_默认预算: Final = 20_000


def _工具声明(角色: RoleId) -> tuple[ToolSchema, ...]:
    """该角色**实际可调**的工具子集 → API 层声明（D-233）。

    ## 为什么必须是子集而不是全集

    提示词装配器早就按角色白名单渲染「本次可调用的工具」，
    并明写着「调不到的工具不会出现在这里」。
    若这里发全集，就与提示词**自相矛盾**：
    模型会看到并尝试调用白名单外的工具，然后被工具层拒绝——
    用户看到的现象是「模型说能调用却调不动」。

    ## 为什么放在编排层而不是服务层

    `ToolSpec`（工具层自己的规格）与 `ToolSchema`（端口的线格式）
    字段同名但分属两层。跨层转换是这个位置独有的职责：
    工具层不该知道端口，编排层是唯一同时看得见两者的层。
    """
    from zhengtong.services.tools.registry import 该角色可调

    return tuple(
        ToolSchema(name=t.name, description=t.description, parameters=t.parameters)
        for t in 该角色可调(角色)
    )


@dataclass(frozen=True, slots=True)
class 阶段产物:
    """一次阶段产出。**内容非空是判据**，故单独成类——
    用 `tuple[str, str]` 会让「有名字但内容空」也长得像成功。"""

    阶段: PhaseId
    名称: str
    内容: str


class 编排回调:
    """工具层需要的编排侧回调（第 14.5 步接）。

    ## 为什么单独一个类

    工具层的 `工具依赖.汇报` 等回调需要一个「知道编排器」的载体。
    早先把它们写成编排器的方法会让编排器背上工具层的全部职责，
    故这里用一个薄壳，只持有一个编排器引用。

    ## 汇报：成员的话进**主理人**的上下文

    `msg.report_to_lead` 的回调签名是 `(内容, 依据) -> str`，
    主理人答复**即返即弃**——不进任何历史。成员汇报完就失忆了：
    主理人下一轮看不到谁报过什么。

    故汇报成对记进主理人历史：成员的报告（user 角色，标为背景）
    + 主理人的答复（assistant 角色）。**成对记**，否则读到答复时
    不知道它是在回应什么。
    """

    __slots__ = ("编排",)

    def __init__(self, 编排: 编排器) -> None:
        self.编排 = 编排

    def 汇报(self, 内容: str, 依据: dict[str, object]) -> str:  # noqa: ARG002  # 统一签名的一部分
        """成员向主理人汇报。返回主理人答复，并**两侧都记进历史**。"""
        答复 = f"（主理人已收到：{内容[:40]}）"
        self.编排.记汇报(内容, 答复)
        return 答复

    def 记事件(self, kind: str, **载荷: object) -> None:
        """工具层把工具调用/拒绝报上来，转成事件发出去（D-266）。

        ## 为什么工具层不自己发 Event

        工具层在 L4、编排层在 L5。低层构造高层值对象会倒置依赖方向
        （架构 §2.3「唯一一条，不可协商」）。故工具层只传
        `(kind, **载荷)`，由这里组装 `Event`。

        ## 载荷放在 data 里，不拼进 detail

        `detail` 是给人看的一句话，`data` 才是给检索与台账用的结构化字段。
        工具名与角色将来要按字段过滤，混进 detail 就只能全文搜。
        """
        from ..domain.event import EventKind

        try:
            事件类 = EventKind(kind)
        except ValueError:
            # 工具层传了未登记的 kind：**静默丢弃比炸掉整轮好**，
            # 但必须留痕，否则拼错的名字会永久静默（D-124 同族处理）。
            self.编排.发(
                EventKind.PHASE_EXITED,
                PhaseId.INTAKE,
                detail=f"工具层上报了未登记的事件类型「{kind}」，已忽略",
            )
            return
        # **阶段填当前所处的那一段**：工具调用可能发生在任何阶段，
        # 用 GATING 会有误导（那是门禁取证段）。此处用最新阶段——
        # 工具层不传阶段，因为它调用时未必知道自己在哪一段。
        过程 = self.编排._过程
        阶段 = 过程.状态.phase if 过程 is not None else PhaseId.INTAKE
        self.编排.发(事件类, 阶段, **载荷)


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
        #: 《政策依据适配表》单独挂一份（D-255）；无则 None
        self._政策取证件: 阶段产物 | None = None
        #: 工具结果存档（D-227）。跨阶段存活——
        #: 模型在 OUTLINE 调的工具，起草阶段仍可能要查回结果。
        self.工具存档: 调用存档类 | None = None
        #: **工具层**（D-267）。真正执行 `tool_calls` 的是 `_过程`，
        #: 故字段落在这一层——放编排器上会让读取跨一层中转。
        self.工具层 = 编排.工具层
        self.出现过的状态: list[RoleState] = []
        self.动过的: list[RoleId] = []
        self.回复: dict[RoleId, str] = {}
        self.交付物 = ""
        self.标注: tuple[str, ...] = ()
        #: **门禁退回的轮次**。退回重写必须重新问模型、
        #: 而重试不得重复问（D-065）；差异由阶段（REVISION vs DRAFTING）承担，
        #: **不掺进 call_id**——架构 §8.6 判「轮次」是第几次尝试而非哪一件事。
        self.轮次 = 0
        #: 每角色一份对话历史（D-041「每角色一个 ContextView」）。
        #: **第 14.5 步接线**：此前 `self.回复` 存了却从不进下一次请求，
        #: 模型每轮从零开始——只知用户为啥，不知自己干了什么。
        self.历史: dict[RoleId, list[Message]] = {}
        #: 主理人收到的汇报（D-041：汇报进主理人上下文）。
        self.汇报: list[Message] = []

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
            return self._门禁循环(档)

        校验 = self._提问前校验(阶段)
        if 校验.is_error:
            self.编排.发(EventKind.PHASE_EXITED, 阶段, ok=False)
            return _转为失败(校验, f"{阶段.value} 段提问前校验失败")
        派 = self._派发(档, 阶段)
        if 派.is_error:
            return _转为失败(派, f"{阶段.value} 段派发失败")
        if 阶段 is PhaseId.OUTLINE:
            # 《政策依据适配表》**不混进阶段聚合正文**（D-255）：
            # `_跑一段` 是单件契约（调用方按 `件.名称` 落盘），
            # 塞第二个产物会破坏它。故挂到编排器，收结果时统一入库
            # ——与 `g3-降级标注` 同一模式。
            self._政策取证件 = self._政策取证产物()
        return Result.ok(阶段产物(阶段, f"{阶段.value}.md", _正文(阶段, self.回复)))

    def _政策取证产物(self) -> 阶段产物 | None:
        """《政策依据适配表》**单独落一份**，不混在阶段聚合正文里（D-255）。

        ## 为什么要单独落

        它有两个下游要**按名取**：G7「依据全可溯」门禁要核政策出处，
        起草阶段要据它写正文。混在 `outline.md` 的多角色聚合里，
        下游只能整篇读、自己拆角色段落——**等于没有稳定的取法**。

        名与内容包一致（`content/prompts/compliance.md` 第 11 条），
        否则模型产出了对不上名字。

        咨询档不产：它豁免政策取证（架构 §8.3）。
        """
        档 = self.状态.task.tier
        if not TIER_BY_ID[档].policy_retrieval:
            return None
        正文 = self.回复.get(RoleId.COMPLIANCE, "")
        if not 正文:
            return None
        return 阶段产物(
            阶段=PhaseId.OUTLINE,
            名称="政策依据适配表.md",
            内容=正文,
        )

    def _提问前校验(self, 阶段: PhaseId) -> Result[None]:
        """**提问前**跑硬约束（D-040 / D-069）。

        `self.validators` 从构造日起就存着却**从未被读**——真校验器接上了，
        没人调它，于是「必填字段零沉降」这条只存在于类型与文档里。
        这是第 11 步 D-196 的同型缺口：装配对了，调用没跟上。

        方向确认（DIRECTION）之前必须校验：拿着缺字段的 D-Card 去问用户，
        问出来的答案填不进去，等于白问一轮。故不通过即拦，不设放行出口
        （D-028 无绕过）。

        无 `validators` 时**不拦**：那是没接校验器的降级运行，与
        「接了但没调」不是一回事——后者才是本条欠账。
        """
        if 阶段 is not PhaseId.DIRECTION:
            return Result.ok_none()
        if self.编排.validators is None:
            return Result.ok_none()
        卡 = self.状态.dcard
        got = self.编排.validators.run_hard_constraint(
            {
                "phase": 阶段.value,
                "dcard": {
                    编号.value: (字段.value or "")
                    for 编号, 字段 in sorted(卡.by_id.items(), key=lambda kv: kv[0].value)
                },
            }
        )
        if got.is_error:
            错 = got.error
            assert 错 is not None
            return Result.fail(错.code, 错.detail)
        结 = got.unwrap()
        self.编排.发(
            EventKind.GATE_PASSED if 结.passed else EventKind.GATE_FAILED,
            阶段,
            script="hard",
        )
        if not 结.passed:
            # **带上规则号与锚点**（D-260、D-271）。
            #
            # 此前只拼 `i.message`，`rule` 与 `at` 明明已经填好却被丢掉——
            # 用户只看到「D1 为空」，既不知道命中哪条规则，也不知道去哪儿改。
            # 格式化收敛在 `ValidationIssue.定位`，两处出口不许各写一份。
            问题 = "；".join(i.定位 for i in 结.issues) or "硬约束不通过"
            return Result.fail(ErrorCode.GATE_FAILED, f"提问前硬约束不通过：{问题}")
        return Result.ok_none()

    def _门禁循环(self, 档: TierId) -> Result[阶段产物]:
        """门禁段主体：真跑判定，不通过则退回重写（D-028/D-050）。

        ## 为什么退回是**内部循环**而不是让 `drive` 重跑

        `drive` 一次性走完静态序列、中途不可改线。退回重写要「回到起草
        再回门禁」，塞进静态序列就只能预先写死若干次重跑——而该跑几次
        取决于门禁结果，事前不可知。故退回循环放在 GATING 段**内部**：
        这也贴合语义——退回是门禁的反馈回路，不是编排主线的一部分。

        ## 为什么退回重写**不吃幂等**

        架构 §8.6 裁决：**「旧口径角色+轮次+序号是错的」**——轮次是
        「第几次尝试」而非「哪一件事」，掺进 `call_id` 会让网络重试也
        命中不了幂等。故重起草**派发 `REVISION` 而非复用 `DRAFTING`**：
        `call_id` 因 `phase_id` 不同而自然不同，正合「退回重写因 phase_id
        不同而不命中」（架构 D-065 表格）。

        ## 无证据 ≠ 通过（D-221）

        原先只遍历裁定**给了**结论的门禁，对**没给**的一律放行——12 道门禁
        若一道结论都没交，整轮却「全部通过」。那正是 E041「无证据不通过」
        的反面：无库/无网导致取证失败时，系统会把「查不了」当成
        「查过了没问题」。

        故按 `该跑门禁` 全集核对，缺席者即**环境性缺失**：一次性升级用户
        决策（gate-manual 反死循环铁律），不消耗退回数、不空转。
        """
        # **该跑门禁是档位的函数，不是轮次的函数**（D-253）。
        # 故在循环外登记一次：原先放在 `while True` 内，每退回一轮就
        # 多加一份全集——实测退回 1 次得 24 条、转裁决得 36 条，
        # 而实际只有 12 道。`gate-report.md` 用的是同一个列表，
        # 于是**用户拿到的报告整份重复一遍**。
        门禁 = _该跑门禁(档)
        self.门禁.extend(门禁)
        while True:
            裁定 = self._并行取证(档, 门禁)
            # 结论落进 RunState——续跑、取证台账、用户裁决都要看它
            for 记 in 裁定.values():
                self.状态 = self.状态.put_gate(记)
            # **缺席即无证据**（D-221）：按该跑门禁全集核对
            # 排除 **动态取证方** 的门禁：G9 不独立取证，按所触发的红线
            # 归属退回（D-084），故它没有独立结论是正常的，
            # 拿「全集核对」去要求它出结论，等于凭空造出一道不存在的缺证据。
            # 再排除**取证方不在本档编制**的门禁：Flash 无文择体，
            # G3 降级给主理人研判并带标注（D-116），本就不该独立取证。
            # 拿它要求出结论，等于凭空造出一道不存在的缺证据（D-049 配平铁律）。
            编制 = set(TIER_BY_ID[档].roster)
            缺席 = [
                g.value
                for g in 门禁
                if g.value not in 裁定
                and not GATES[g].dynamic_owners
                and any(r in 编制 for r in GATES[g].owners)
            ]
            if 缺席:
                self._留门禁报告(裁定)
                self.编排.发(
                    EventKind.GATE_ESCALATED,
                    PhaseId.GATING,
                    detail=f"取证无结论：{'、'.join(缺席)}",
                )
                self.编排.发(EventKind.PHASE_EXITED, PhaseId.GATING, ok=False)
                return Result.fail(
                    ErrorCode.GATE_NO_EVIDENCE,
                    f"门禁无取证结论：{'、'.join(缺席)}（无库/无网？），转用户处理",
                )
            驳回 = [记 for 记 in 裁定.values() if not 记.passed]
            if not 驳回:
                return Result.ok(
                    阶段产物(
                        PhaseId.GATING,
                        "gate-report.md",
                        _门禁报告(self.门禁, 裁定),
                    )
                )
            # **本档不许退回时，任何门禁不通过都直接转用户裁决**（D-254）。
            #
            # 现状：咨询档因 `gates_apply=False` → 门禁数为 0 → 循环不进，
            # 故 `allows_rewrite` 一直**没有被执行过**，只是恰好正确。
            # 一旦咨询档将来要门禁，它立刻变成真漏洞。故在此显式兑现。
            #
            # 判据：`allows_rewrite=False` 时**退回上限为 0**——
            # 交给 `门禁判定` 统一裁决，不另开分支：
            # 另开分支就多一条「不经退回上限」的路，而 D-050 的
            # 语义正是「attempt >= 上限 → 升级裁决」。
            允许退回 = TIER_BY_ID[档].allows_rewrite
            for 记 in 驳回:
                # 退回数以**编排实际经手的轮次**为准（`self.轮次`），
                # 不轻信裁定方自报的 attempt——否则上限只在脚本配合时才成立。
                真实记 = (
                    replace(
                        记,
                        attempt=max(记.attempt, self.轮次),
                        # 不许退回的档位把上限压到 0 → 必然升级裁决
                    )
                    if 允许退回
                    else replace(记, attempt=退回上限)
                )
                判 = 门禁判定(真实记, 环境性缺失=False)
                self.编排.发(
                    EventKind.GATE_REJECTED,
                    PhaseId.GATING,
                    detail=f"{真实记.gate}：{真实记.rejection_reason}",
                )
                if 判 is 处置.升级裁决:
                    self._留门禁报告(裁定)
                    if 允许退回:
                        说明 = (
                            f"{真实记.gate} 退回 {真实记.attempt} 次仍未过"
                            f"（{真实记.rejection_reason}），转用户裁决"
                        )
                        详情 = 说明
                    else:
                        # **要说清是「不允许改」而非「改不动」**（D-254）：
                        # 只说「门禁未通过」会让用户以为再试一次有用。
                        说明 = (
                            f"{真实记.gate} 未通过（{真实记.rejection_reason}），"
                            f"本档位不允许退回重写，转用户裁决"
                        )
                        详情 = f"{说明}——该档位已关闭退回重写，无法自动修正"
                    self.编排.发(
                        EventKind.GATE_ESCALATED,
                        PhaseId.GATING,
                        detail=说明,
                    )
                    self.编排.发(EventKind.PHASE_EXITED, PhaseId.GATING, ok=False)
                    return Result.fail(ErrorCode.GATE_REWRITE_EXHAUSTED, 详情)
            # 全部判退回 → 轮次 +1，以 REVISION 阶段重起草（call_id 天然不同）
            self.轮次 += 1
            self.编排.发(EventKind.GATE_REWORK, PhaseId.GATING, detail=f"第 {self.轮次} 轮重写")
            self.编排.发(EventKind.PHASE_STARTED, PhaseId.REVISION)
            重跑 = self._派发(档, PhaseId.REVISION)
            if 重跑.is_error:
                self._留门禁报告(裁定)
                self.编排.发(EventKind.PHASE_EXITED, PhaseId.REVISION, ok=False)
                return _转为失败(重跑, "退回后重起草失败")
            self.编排.发(EventKind.PHASE_EXITED, PhaseId.REVISION, ok=True)

    def _留门禁报告(self, 裁定: dict[str, GateRecord]) -> None:
        """**失败路径上也要留下门禁报告**（D-236）。

        ## 为什么失败路径更需要它

        用户被门禁卡住时拿到的是一个错误码，
        而「哪道门禁、为什么、谁查的、证据是什么」全在这份报告里。
        原先三条失败路径（无结论 / 升级裁决 / 退回失败）
        都直接 `return Result.fail`，产物列表是空的——
        用户除了错误码什么都拿不到，只能回去翻日志原文，
        而那正是用户明确要求「不要展示」的东西。

        报告**照留、但不落盘**：`ARTIFACT_READY` 事件带全文（D-046），
        这条路径不发该事件，故不会把未过稿当成果报给界面。
        """
        件 = 阶段产物(PhaseId.GATING, "gate-report.md", _门禁报告(self.门禁, 裁定))
        self.产物.append(件)

    def _并行取证(self, 档: TierId, 门禁: tuple[GateId, ...]) -> dict[str, GateRecord]:
        """门禁取证：**并行组并发跑，主理人终闸组串行在后**（D-049）。

        ## 复查时补上的接线

        首版这里调的是 `_派发`——一条串行 `for` 循环，12 道门禁 11 次调用
        全程严格 enter/exit 交替。D-049 要的是「并行 4 组」，
        `kernel/scheduler.run_groups` 早就是完整的并行调度器
        （ThreadPoolExecutor、D-130 异常就地转失败、顺序恒等于入参），
        但**生产代码零引用**。这正是本项目反复记录的同型缺口：
        零件都测过、衔接没测。

        ## 编排侧同样也有终闸混入（D-218 的另一半）

        `_该段角色` 从 `GATES[...].owners` 展开，会把 G12 的取证方
        （主理人）也算进 GATING 当值。故并行组**只取 `并行门禁组()`**，
        终闸组由 `_派发` 单独跑——架构 §8.3：「主理人独占终闸，
        不与其他门禁并行」。
        """
        并行组 = tuple(组 for 组 in 并行门禁组() if any(r in TIER_BY_ID[档].roster for r in 组))
        # 终闸组：主理人独占，串行在后
        终闸 = tuple(r for 组 in 终闸组() for r in 组 if r in TIER_BY_ID[档].roster)

        裁定: dict[str, GateRecord] = {}
        if 并行组:
            任务 = tuple(
                GateGroup(
                    owner=组[0], gate_ids=tuple(g.value for g in 门禁 if 组[0] in GATES[g].owners)
                )
                for 组 in 并行组
            )

            def _跑一组(g: GateGroup) -> Result[dict[str, GateRecord]]:
                组门禁 = tuple(x for x in 门禁 if x.value in g.gate_ids)
                # 取证方**在自己的线程里**走一遍状态机并动笔——
                # 并行不能绕过 D-042 的六态机，否则 `动过的角色` 缺人，
                # 「编制内必须当值」这类判据就查不出谁真被派发过。
                self.状态 = self.状态.set_role_state(g.owner, RoleState.RUNNING)
                self.出现过的状态.append(RoleState.RUNNING)
                cid = make_call_id(
                    "gate", str(self.编排.运行号), PhaseId.GATING.value, role=g.owner.value
                )
                if cid not in self.已完成:
                    答 = self.llm.complete(self._构造请求(g.owner, PhaseId.GATING))
                    if 答.is_error:
                        错 = 答.error
                        assert 错 is not None
                        return Result.fail(错.code, f"{g.owner.value} 取证失败：{错.detail}")
                    self.回复[g.owner] = 答.unwrap().text
                    self._记入历史(g.owner, PhaseId.GATING, 答.unwrap().text)
                    self.已完成 = (*self.已完成, cid)
                self.动过的.append(g.owner)
                self.出现过的状态.append(RoleState.DONE)
                self.状态 = self.状态.set_role_state(g.owner, RoleState.DONE)
                # 每组**各自**取一次取证结论：并行的意义就在这儿——
                # 一组一请求，而不是 12 道门禁挤在一次调用里。
                return Result.ok(self.编排.门禁取证(组门禁))

            结果 = run_groups(任务, _跑一组)
            for 项 in 结果:
                if 项.value.is_ok:
                    裁定.update(项.value.unwrap())
        if 终闸:
            终闸门禁 = tuple(g for g in 门禁 if any(r in GATES[g].owners for r in 终闸))
            if 终闸门禁:
                裁定.update(self.编排.门禁取证(终闸门禁))
        return 裁定

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
            cid = make_call_id("prompt", str(self.编排.运行号), 阶段.value, role=角色.value)
            if cid not in self.已完成:
                问 = self._构造请求(角色, 阶段)
                答 = self.llm.complete(问)
                if 答.is_error:
                    错 = 答.error
                    assert 错 is not None
                    return Result.fail(错.code, f"{阶段.value}/{角色.value} 调用失败：{错.detail}")
                响应 = 答.unwrap()
                self.编排._记用量(阶段, 问, 响应)
                # **执行模型请求的工具**（D-267）。
                #
                # 此前 `LlmResponse.tool_calls` 从不被读：请求的 `tools`
                # 真的发给模型了（D-233），模型也会真的调用，
                # 但响应一到就被当普通文本丢掉——**16 个工具全是死的**。
                工具轮 = self._跑工具(角色, 阶段, 响应)
                if 工具轮.is_error:
                    错 = 工具轮.error
                    assert 错 is not None
                    return Result.fail(
                        错.code, f"{阶段.value}/{角色.value} 工具调用失败：{错.detail}"
                    )
                响应 = 工具轮.unwrap()
                self.回复[角色] = 响应.text
                self._记入历史(角色, 阶段, 响应.text)
                self.已完成 = (*self.已完成, cid)
            self.动过的.append(角色)
            self.出现过的状态.append(RoleState.DONE)
            self.状态 = self.状态.set_role_state(角色, RoleState.DONE)
        return Result.ok_none()

    def _跑工具(self, 角色: RoleId, 阶段: PhaseId, 响应: LlmResponse) -> Result[LlmResponse]:
        """跑完模型这一轮请求的全部工具调用，返回最终响应（D-267、D-274）。

        ## 结果**要**进对话序列（D-272）

        此前只记动作不记结果，理由是「上下文会被材料正文撑爆」。
        D-274 推翻了它：材料经工具读入后是 `role="tool"` 消息，
        与对话混在同一窗口，压缩时**材料先于对话丢**
        （`compactor.py:97 _丢弃顺序()`）——所以「白占窗口」是临时的、
        压缩时自动回收的，而截断的代价是永久不可逆的。
        不设额外截断，超窗口交 `CONTEXT_COMPACTED`。

        ## 顺带修掉一处**协议顺序**错误（D-274 同批发现）

        此前对每个调用各记一条 `assistant` 消息。一轮里调两个工具，
        历史就成了 `assistant(c1), assistant(c2)`，
        后面再跟两条 `tool` 结果——**OpenAI 协议要求每条 tool 消息
        紧跟在发出该 tool_calls 的 assistant 之后**，
        `assistant(c1), assistant(c2), tool(c1), tool(c2)` 会被判为
        「tool 消息找不到对应的 tool_calls」而整条请求被拒。

        故改为：**一轮一条 assistant 消息带全部 tool_calls，
        随后按调用顺序各跟一条 tool 消息**。

        ## 为什么要循环

        模型可能一轮里连续调多个工具，也可能**根据第一个工具的结果
        再调第二个**（先查目录再读文件）。单次执行会让后一种直接丢失。

        故按上限迭代——**上限不是可选的**：模型连续调工具可能永不停止，
        没有上限就是死循环（内核保险丝同族，架构 §5.2）。

        ## 没有工具层时怎么办

        静默返回原响应，不失败：工具层未接入时其余流程仍应能跑通
        （测试与降级路径都依赖这一点，D-192）。
        """
        if not 响应.tool_calls:
            return Result.ok(响应)
        if self.工具层 is None:
            return Result.ok(响应)
        当前 = 响应
        for _ in range(_工具轮上限):
            if not 当前.tool_calls:
                break
            本轮 = 当前.tool_calls
            # **一条 assistant 消息带全部 tool_calls**——不是每个调用一条。
            self.记工具动作(角色, *本轮)
            正文组: list[tuple[str, str]] = []
            for 调用 in 本轮:
                结果 = self.工具层.调用(调用.name, 角色=角色, 参数=dict(调用.arguments))
                正文 = 结果.unwrap() if not 结果.is_error else f"[工具失败] {结果.error}"
                self.存档工具结果(调用.id, 正文)
                正文组.append((调用.id, 正文))
            # 全部执行完再按顺序回传，让 tool 消息紧跟那条 assistant
            for 调用id, 正文 in 正文组:
                self.记工具结果(角色, 调用id, 正文)
            # **再问一轮**，让模型看得到自己调过什么、决定要不要继续
            问 = self._构造请求(角色, 阶段)
            答 = self.llm.complete(问)
            if 答.is_error:
                错 = 答.error
                assert 错 is not None
                return Result.fail(
                    错.code, f"{阶段.value}/{角色.value} 工具后续调用失败：{错.detail}"
                )
            当前 = 答.unwrap()
            # 工具后续轮的用量同样要记（D-277）——
            # 一轮里多次调模型，少记一次累计值就对不上，
            # 而用户是拿这个数字对账的。
            self.编排._记用量(阶段, 问, 当前)
        return Result.ok(当前)

    def 存档工具结果(self, 调用id: str, 正文: str) -> None:
        """把工具结果存进 `调用存档`，供 `tool.result(call_id)` 查回（D-227）。

        **存在编排器上而不是 `_过程` 上**：它跨阶段存活——
        模型在 OUTLINE 调的工具，起草阶段仍可能要查回结果。

        **D-274 后它不再是结果的唯一去处**：结果同时进了对话序列。
        存档保留的价值是「要全文时不占常驻上下文」，
        且跨阶段存活而对话历史不跨阶段清理。
        """
        if self.工具存档 is None:
            self.工具存档 = 调用存档类()
        self.工具存档.记(调用id, 正文)

    def 记工具动作(self, 角色: RoleId, *调用: ToolCall) -> None:
        """把一轮的**工具调用动作**记进该角色历史（D-274）。

        消息 `content` 留空而只挂 `tool_calls`——它是「我做过什么」，
        不是「我看到了什么」。两者混在一处，模型会把结果当既成事实。

        **一轮一条消息带全部 tool_calls**（不是一个调用一条）：
        OpenAI 协议要求 `tool` 消息紧跟在发出该 `tool_calls` 的
        `assistant` 之后，逐个调用各记一条会让后续所有 `tool` 消息
        配不上号，整条请求被服务端拒收。

        **结果不在这里记**：见 `记工具结果`。
        """
        if not 调用:
            return
        条 = self.历史.setdefault(角色, [])
        条.append(Message(role="assistant", content="", tool_calls=调用))

    def 记工具结果(self, 角色: RoleId, 调用id: str, 正文: str) -> None:
        """把工具结果**按 OpenAI 协议**记进历史（D-272、D-274）。

        ## 修的是什么

        `Message.tool_call_id` 此前**只发不收**——适配器会把它序列化进请求体，
        但全仓没有任何一处构造带 `tool_call_id` 的消息。
        于是「半条协议」：模型调了工具，工具也执行了，
        **结果却回不到模型手上**，它只能凭空猜自己查到了什么。

        ## 完整回传，不截断（D-274）

        正文原样进历史。超窗口由 `CONTEXT_COMPACTED` 按
        「材料先于对话丢」的优先级回收，**不由这里截断**。

        **失败也必须回传**：`[工具失败] …` 是一条 `tool` 消息而不是空串——
        空串会让模型以为「确实没有结果」（D-227 禁止的那种欺骗）。
        """
        条 = self.历史.setdefault(角色, [])
        条.append(Message(role="tool", content=正文, tool_call_id=调用id))

    def 记汇报(self, 内容: str, 答复: str) -> None:
        """把一次汇报成对记进**主理人**历史（第 14.5 步）。

        成员的报告标成 `user` 角色并打上「背景」标签——
        它是**别人说的话**，不是主理人的指令；不标的话模型会
        把它当成对自己的要求。
        """
        条 = self.历史.setdefault(RoleId.TEAM_LEAD, [])
        条.append(Message(role="user", content=f"【背景·成员汇报】{内容}"))
        条.append(Message(role="assistant", content=答复))

    def _记入历史(self, 角色: RoleId, 阶段: PhaseId, 回复: str) -> None:
        """把这一轮的**提问与回复**记进该角色的上下文（第 14.5 步）。

        ## 为什么必须记

        接线前 `self.回复` 存了却从不进下一次请求：模型每轮从零开始，
        只知道用户要什么，不知道**自己上一轮干了什么**。退回重写时更糟——
        第 14 步的门禁退回让角色重起草，但它拿不到上一版为什么被打回。

        ## 提问也记，否则「它干了什么」是断的

        只记回复的话，历史是一串没有上下文的答案。模型读到第 5 条回复时
        不知道它是在回应什么。故**成对记**（user 问 / assistant 答）。

        ## 用户消息要打背景标签

        历史里的 user 消息前缀「【背景·{阶段}】」——
        否则模型会把它们与末位那条「本轮任务」同等对待，
        于是每轮都想回答所有以前提过的要求。

        ## 工具轮之后**不需要**特殊处理（证伪记录）

        D-274 让历史里出现了 `role="tool"` 消息，我一度以为
        `_记入历史` 会破坏协议——它在 tool 消息之后插了一条 `user`。
        **逐条核对 OpenAI 协议后确认这不违规**：
        协议只要求 `tool` 消息紧跟在**发出该 `tool_calls` 的那条
        assistant** 之后，而这里它确实紧跟；
        `user` 出现在其后不影响配对。

        故**保持原样**：为「显得整齐」去调换顺序，
        反而会破坏本方法「成对记」的设计意图
        （`user` 提问在前、`assistant` 回复在后），**且并无任何收益**。
        """
        条 = self.历史.setdefault(角色, [])
        条.append(
            Message(
                role="user",
                content=f"【背景·{阶段.value}】{self.状态.task.requirement}",
            )
        )
        条.append(Message(role="assistant", content=回复))

    def _接入上下文(
        self, 静态: tuple[Message, ...], 角色: RoleId, 阶段: PhaseId, 需求: str
    ) -> tuple[Message, ...]:
        """把历史、工具清单、本轮任务接到装配器产出的静态消息之后（第 14.5 步）。

        ## 接线前是什么样

        装配器只发**一条** system（角色提示词 + 规则 + D-Card 全拼在一起），
        编排层再补一条裸需求。实测每次请求**恒 2 条消息**、零历史——
        前一角色写的正文、上一轮的审核意见，模型每轮都看不见。
        那不是效率问题，是质量问题：`self.回复` 存了却从不进下一次请求。

        ## 装配器产出的静态块**整体前置**，不拆

        装配器已经按预算闸门截断过五块（角色提示词/规则条目/D-Card/产物契约/工具），
        拆开会打乱那个闸门的语义。此处只做**追加**，让静态块保持
        连续前缀——前缀缓存只匹配从头开始的连续前缀，拆了就击穿缓存。

        故「六层显式化」在 `ContextView` 那一层做（`system_and_role` /
        `dynamic_rules` / `tool_manifest` 各归各位），而这里保证它们
        在**实际发出的序列**里也是这个顺序。

        ## 本轮任务恒为末位

        末位 user 写成祈使句并自带「以上均为背景资料」——
        模型的指令跟随对近端更敏感，末位能让「这轮要回答什么」
        压过「背景资料是什么」。这不是暗示，是结构性强化。
        """
        历史 = tuple(self.历史.get(角色, ()))
        视图 = 组装(
            系统与角色=静态,
            动态规则=(Message(role="system", content=f"【当前阶段】{阶段.value}"),),
            工具清单=(
                Message(
                    role="system",
                    content=f"【本次可调用工具】{_工具清单(角色, 阶段)}",
                ),
            ),
            对话=历史,
            本轮任务=_本轮任务(需求, 角色, 阶段),
        )
        序列 = 视图.messages()
        if 需要压缩(视图, 上下文容量=self.编排.容量token):
            # 压缩接在这里——此前整个压缩模块**生产零引用**（第 14.5 步接线）
            压后 = 压缩(视图, 上下文容量=self.编排.容量token, 摘要器=self.编排.摘要器)
            if 压后 is not None:
                序列 = 压后.视图.messages()
                self.编排.发(
                    EventKind.CONTEXT_COMPACTED,
                    阶段,
                    丢弃条数=压后.丢弃条数,
                    压缩前token=压后.压缩前token,
                    压缩后token=压后.压缩后token,
                    摘要token=压后.摘要token,
                    真的变小=压后.是否真的变小了,
                )
            else:
                # **压不动也要说出来**（D-266）。
                # 原注释写「压不动就照发，由服务端判它超限」——那是诚实的选择，
                # 但**用户与排障者无从知道本地没压**：
                # 「服务端会超限」与「本地压过了还是超限」在 journal 里长得一样。
                self.编排.发(
                    EventKind.CONTEXT_COMPACTED,
                    阶段,
                    压不动=True,
                    原因="恒定段（角色提示词 + 本轮任务）已超容量，压缩无可施力处",
                    压缩前token=估算视图(视图),
                )
            # 压不动（恒定段就超限）就照发——由服务端判它超限，
            # 比本地悄悄截断掉上下文更诚实
        return 序列

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
            消息 = self._接入上下文(装配.消息, 角色, 阶段, 需求)
        else:
            消息 = self._接入上下文(
                (
                    Message(
                        role="system",
                        content=(
                            f"[内容包未接入] 你{角色.value}，当前阶段 {阶段.value}。"
                            "**这是降级提示，不是完整角色提示词。**"
                        ),
                    ),
                ),
                角色,
                阶段,
                需求,
            )
        请求 = LlmRequest(
            model=self.编排.模型名,
            messages=消息,
            # **工具必须真的发给模型**（D-233）。
            #
            # 此前只有提示词里那段「本次可调用的工具」文本，
            # 请求的 `tools` 字段**全仓生产零填充**——
            # 于是模型看清单却调不到任何工具，`tool_calls` 恒为空。
            # 提示词层与请求层各自测试都绿，**因为没人在两层之间对账**。
            tools=_工具声明(角色),
            # 温度走配置基准（D-234）。此前 `LlmRequest.temperature`
            # 用的是字段默认值 0.3，`Config.temperature` 从未影响任何请求。
            # **温度按角色取**（D-243）：此前所有角色的请求都用
            # 同一个全局基准，「各角色可不同温度」这条约束从未实现。
            # 取值在 `domain/role.py` 的常量表里，用户不可配（D-146 的坑）。
            temperature=取温度(角色),
        )
        # 并行取证时多线程共写这张表，故加锁（D-049）
        with self.编排.请求锁:
            self.编排.发出的请求.append(请求)
        return 请求

    def _路径(self, 名称: str) -> str:
        return f"{_产物根}/{self.编排.运行号}/{名称}"

    def 收结果(self) -> 编排结果:
        档 = self.状态.task.tier
        # 《政策依据适配表》落盘并入库（G7 门禁与起草阶段按名取，D-255）
        if self._政策取证件 is not None:
            件 = self._政策取证件
            存 = self.storage.write_atomic(self._路径(件.名称), 件.内容)
            if not 存.is_error:
                self.产物.append(件)
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
        门禁取证: Callable[[tuple[GateId, ...]], dict[str, GateRecord]] | None = None,
        容量token: int = 128_000,
        温度: float = 0.3,
        摘要器: Callable[[tuple[Message, ...]], Message] | None = None,
        工具层: 工具运行器 | None = None,
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
        #: 门禁取证：给一组门禁，返回各门禁结论。
        #: 未注入时返回**空结论**而非「全部通过」——按 `FORMAL_GATES`
        #: 全集核对时空结论即缺席，会判无证据（E041/D-221）。
        #: 「没人取证」与「取过了都没问题」必须是两回事。
        self.门禁取证 = 门禁取证 or (lambda _门禁: {})  # noqa: ARG005
        #: 门禁并行取证（D-049）时保护共享可变的锁。
        self.请求锁 = Lock()
        #: 上下文容量（token）。用于 85% 压缩兜底阈值。
        #: 工具层的编排回调（第 14.5 步接）。
        #: **此前工具层与编排器完全没连**——`msg.report_to_lead` 只存在于
        #: 提示词文本里，真跑起来会走 D-192「入口未接线」失败。
        self.工具回调 = 编排回调(self)
        #: **工具层**（D-267）。此前编排器不持有它，
        #: 于是 `LlmResponse.tool_calls` 无处执行——
        #: 模型看得见工具清单、调得到接口，调用却从不被跑。
        self.工具层 = 工具层

        self.容量token = 容量token
        #: 基准温度（D-234）。此前每次请求都吃 `LlmRequest` 的字段默认
        #: 0.3，`Config.temperature` 从未影响任何真实请求——
        #: 用户改配置里的温度**完全无效**。
        self.温度 = 温度
        #: 压缩摘要器。为 None 时走「有界原文」兜底并标明未经摘要。
        self.摘要器 = 摘要器

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

    def _记用量(self, 阶段: PhaseId, 请求: LlmRequest, 响应: LlmResponse) -> None:
        """发 `model.request` / `model.response`，**载荷只有数字**（D-277）。

        ## 修的是什么

        `MODEL_REQUEST` / `MODEL_RESPONSE` 两个 kind 在枚举里躺了很久，
        测试也测了它们，**但生产代码一处都没发过**——
        与 D-118（分支挂在白名单外因而永不执行）、D-237
        （事件两端都在却没连线）完全同型。
        于是 `TokenUsage` 三个字段用户路径零消费：
        跑完一稿，**永远不知道花了多少 token**。

        ## 载荷纪律：只放数字，不放正文

        `prompt_tokens` / `completion_tokens` / `finish_reason`，
        **没有 `text`**。`MODEL_RESPONSE` 进了 `USER_VISIBLE_KINDS`
        （标准模式要显示账单），若把模型回复正文一并塞进去，
        那就是 D-046 明令禁止的「模型间交互」上了标准界面——
        **可见的是用量，不是内容**。

        ## 为什么不用估算值

        D-077 确立的纪律：估算只用于 85% 压缩触发的**提前示警**；
        「界面展示用量」必须用 **API 返回的真实值**（架构 §686）。
        用户看的是实际消耗，不是我们的预测。

        ## 为什么不再建 `system/telemetry.json`

        D-031 修订已定该文件不建，且明写「token 台账不受影响」
        ——用户 2026-10-03 裁决取**读法 A**：只发事件。
        事件本来就会落 journal（`kernel/journal.py` 的 `_to_json`
        带 `data`），事后可查；而聚合台账要维护一套状态，
        对一个「跑一次看个数字」的场景是净成本。
        """
        self.发(
            EventKind.MODEL_REQUEST,
            阶段,
            model=请求.model,
            消息数=len(请求.messages),
            工具数=len(请求.tools),
        )
        用量 = 响应.usage
        self.发(
            EventKind.MODEL_RESPONSE,
            阶段,
            model=请求.model,
            prompt_tokens=用量.prompt_tokens,
            completion_tokens=用量.completion_tokens,
            reasoning_tokens=用量.reasoning_tokens,
            finish_reason=响应.finish_reason.value,
        )

    def 取一步(self) -> Callable[[int], Result[Step[int]]]:
        """返回 `drive` 要的 `step` 回调。**一次一阶段**。

        若一步跑完全部阶段，`drive` 就是空壳，
        而续跑与 TUI 检查点插话会全部失效（D-037/D-012）。

        **必须先 `建过程`**——`取一步` 单独用时（如测试 drive 的分段性）
        不会经过 `跑到终`，故这里就地建一次。
        """
        return self._建过程().推进一步

    def 记汇报(self, 内容: str, 答复: str) -> None:
        """转给当前推进过程记账（见 `_过程.记汇报`）。"""
        self._建过程().记汇报(内容, 答复)

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


def _本轮任务(需求: str, 角色: RoleId, 阶段: PhaseId) -> Message:
    """末位 user 消息：**祈使句** + 显式背景声明（第 14.5 步）。

    「刻意突出用户指令」不靠位置暗示，靠两件事同时做：
    写成祈使句（模型看到的是待办，不是一段素材），
    并明说「以上均为背景资料，本轮任务以本条为准」。

    没有第二句时，模型会把历史里那些需求陈述也当成待办——
    于是每轮都在试图回答所有以前提过的要求，这正是「岔子」。
    """
    return Message(
        role="user",
        content=(
            "## 本轮任务\n"
            f"你是{角色.value}，当前处于「{阶段.value}」阶段。\n"
            "请依据以上全部背景，**只回答这一件事**：\n"
            f"{需求}\n"
            "\n"
            "**以上均为背景资料。本轮任务以本条为准。**"
        ),
    )


def _工具清单(角色: RoleId, 阶段: PhaseId) -> str:  # noqa: ARG001  # 统一签名；清单暂按角色白名单恒定
    """本次实际可调用的工具（供模型知道边界）。"""
    return "shared.read / shared.write / shared.list / kb.search / kb.read / msg.report_to_lead"


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
        # **架构 §8.3 的 Phase 0 与 Phase 0.5 合并在此格**（D-255）：
        # Phase 0 = 陈纲举因果推演，Phase 0.5 = 骆准绳政策取证。
        # 内容包 `compliance.md` 第 11 条明写「政策主动检索
        # （Phase 0.5 · 起草前）」并要求产出《政策依据适配表》——
        # 而此前**代码从不派骆准绳**，产物名在 `src/` 里也不存在。
        #
        # 不拆独立 `PhaseId`：一格走完两段是**有意设计**
        # （`domain/phase.py` 注释：「假装是一对一，等于把代码少一次交接
        # 藏起来」），拆枚举会波及续跑、投影与事件序列。
        #
        # 按 `policy_retrieval` 分流而非查档位名：咨询档豁免的只是
        # **政策取证**，因果推演照做（架构 §8.3 三项豁免之一）。
        该动 = [RoleId.STRUCTURE] if RoleId.STRUCTURE in 编制 else []
        if TIER_BY_ID[档].policy_retrieval and RoleId.COMPLIANCE in 编制:
            该动.append(RoleId.COMPLIANCE)
        return tuple(该动)
    if 阶段 is PhaseId.DRAFTING:
        # §8.3：Phase 1 起草是辞达意，Phase 2 格式壳是骆准绳。
        return tuple(r for r in (RoleId.LANGUAGE, RoleId.COMPLIANCE) if r in 编制)
    if 阶段 is PhaseId.REVISION:
        # **退回修正 = 重起草**，动笔人是起草人（§8.3 Phase 1 辞达意）
        # + 格式壳（骆准绳）。用 REVISION 这个 phase 而非复用 DRAFTING，
        # 是为了让 `call_id` 自然不同（架构 D-065：退回靠 phase 区分、不靠轮次）。
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


def _门禁报告(
    门禁: list[GateId],
    裁定: dict[str, GateRecord] | None = None,
) -> str:
    """门禁报告：**责任角色 + 结论 + 证据 + 未过原因**（D-236）。

    ## 为什么必须带这四项

    标准模式不展示审核意见（用户约束），所以这份报告是**事后唯一的台账**。
    原先只有「门禁号 + 名字」两样，于是三个字段生产零消费：

    - `GateRecord.evidence` —— 只在序列化时读写，取证方给的证据**没人看**
    - `rejection_reason` —— 驳回了，但用户看不到**为什么**被驳回
    - `gate_owners(g)` —— 生产零引用，而架构 §8.3「这稿是谁的锅？」
      问的就是这个（D-049 的责任映射）

    用户只知道「被退回了」，无法判断是文稿问题还是环境问题，
    只能回去翻日志原文——而那正是用户要求「不要展示」的东西。

    ## 角色名用中文

    用户约束：内部标识用英文，**界面显示中文角色名**。
    报告是给人看的，故取 `display_name` 而非 `role.value`。

    ## 缺结论要写明「无结论」而不是「通过」

    门禁若不在 `裁定` 里（缺席、动态取证方未派发），
    印「通过」就是把没查说成查过了——D-221 的老问题换个地方复发。
    """
    行: list[str] = ["【门禁检查】"]
    for g in 门禁:
        规 = GATES[g]
        角色 = (
            "（运行期确定）" if 规.dynamic_owners else "、".join(display_name(r) for r in 规.owners)
        )
        记 = (裁定 or {}).get(g.value)
        if 记 is None:
            结论 = "**无结论**"
            证据 = ""
            原因 = ""
        elif 记.passed:
            结论 = "通过"
            证据 = "；证据：" + "；".join(记.evidence) if 记.evidence else ""
            原因 = ""
        else:
            结论 = f"**未通过**（退回 {记.attempt} 次）"
            证据 = "；证据：" + "；".join(记.evidence) if 记.evidence else ""
            原因 = f"；原因：{记.rejection_reason}" if 记.rejection_reason else ""
        行.append(f"- {g.value} {规.name}｜取证：{角色}｜{结论}{证据}{原因}")
    return "\n".join(行)


def _临时总线() -> EventBus:
    """无总线时的最小可用总线。**不是「构造真件」**——
    真实总线由 `App` 装配根持有并注入。
    """
    from ..kernel.redactor import Redactor

    return EventBus(Redactor())
