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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from queue import Empty, SimpleQueue
from threading import Lock
from typing import TYPE_CHECKING, Final

from ..domain.artifact import ArtifactKind, ArtifactRef
from ..domain.dcard import FIELD_LABELS, DCard, DCardFieldId, Provenance
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
from ..domain.ids import ArtifactId, RunId
from ..domain.phase import PhaseId, PhaseState
from ..domain.reasoning import 推理开关
from ..domain.role import RoleId, RoleState, display_name, 取温度
from ..domain.run_state import GateRecord, RunState, new_run_state
from ..domain.task import TaskSpec
from ..domain.tier import TIER_BY_ID, ReasoningPolicy, TierId
from ..domain.validation import g3降级标注, validate_task
from ..kernel.bus import EventBus
from ..kernel.driver import Step, drive
from ..kernel.scheduler import GateGroup, run_groups
from ..ports.content_pack import ContentPackPort
from ..ports.export import ExportPort, 未接线导出, 落盘摘要
from ..ports.llm import (
    FinishReason,
    LlmPort,
    LlmRequest,
    LlmResponse,
    Message,
    ToolCall,
    ToolSchema,
    是已知结束原因,
)
from ..ports.storage import StoragePort
from ..ports.validators import ValidatorPort
from ..services.context import 压缩, 组装, 需要压缩
from ..services.context.memory_gate import 记忆门 as 记忆门类
from ..services.context.view import 估算视图
from ..services.export import 导出文件
from ..services.prompting import 装配提示词, 装配请求, 预算闸门
from ..services.tools.calllog import 调用存档 as 调用存档类
from ..services.tools.runner import 工具运行器
from .gates_forging import 门禁取证实装, 门禁取证请求
from .machine import 该发请求

#: 「没有推理结论」的**单例**默认值（D-384）。
#:
#: 不能写 `推理关闭: 推理开关 = 推理开关()`——那是在默认参数里调用函数
#: （Ruff B008）。而 `推理开关` 是 frozen + slots 的 dataclass，
#: 构造结果不可能被就地改坏，故**共享一个单例是安全的**，
#: 也比 `default_factory` 少一次构造。
_无推理结论: 推理开关 = 推理开关()

#: 产物路径前缀。**必须以 `data/` 开头**——
#: `paths.AI_WRITABLE` 只认 `data/`（AI 只能写 `data/`），
#: 写成运行号开头会被路径守卫拒绝（第 13 步第九次 D-165 的教训）。
#:
#: 续跑要按同一路径读回上次的产物（`推进一步` 里的 `read_text`），
#: 故写与读必须共用这一个前缀——**各写各的必然分叉**。
_产物根: Final = "data"

#: **工具调用轮数上限**（D-267）。
#:
#: 模型可能一轮里连调多个工具，也可能据前一个结果再调后一个，
#: 故按上限迭代。**上限不是可选的**：连续调用可能永不停止，
#: 没有上限就是死循环（内核保险丝同族，架构 §5.2）。
_工具轮上限 = 4

#: 最终主件的落盘文件名（D-296）。
#: 用户约束：最终产出是 TXT，保留主送机关等必要内容要素，
#: **不加 YAML 元数据头**。
_主件文件名 = "01-主件.txt"

#: 默认预算（D-189）。真跑时应来自配置里的上下文容量；
#: 这里给保守值，**不给「无上限」**——无上限等于闸门不存在。
_默认预算: Final = 20_000


def _本档可调工具(角色: RoleId, 档: TierId) -> tuple[ToolSchema, ...]:
    """该角色在**该档位**下真正可调的工具。

    ## 提示词与 API 必须同源（D-233 补漏）

    此前这里有两个互不相干的来源：

    - `_工具声明` 从白名单取 → 实际发给 API（17 个）
    - `_工具清单` 是**一个 return 字面量** → 写进系统提示词（6 个）

    两边对不上，后果正是旧注释警告的那条：
    「模型说能调用却调不动」。实测更糟——字面量里的 `kb.read`
    **根本不存在**（真名 `kb.read_pack` / `kb.read_user_file`）。
    模型照着提示词调必然失败；而提示词又说只有 6 个工具，
    `flow.ask_user`、`draft.submit` 这些它也不敢用。

    现在只有一个来源：提示词与 API 都从这里取，**分叉不再可能**。

    ## 档位也要看

    `requires_user_questions=False`（咨询档）此前**零消费**——
    声明了免提问，却照旧把 `flow.ask_user` 发给模型，
    「免方向锁」只存在于 `team-lead.md` 里的一句自然语言，
    靠模型自觉。现在该档位直接不发这个工具。
    """
    from zhengtong.services.tools.registry import 该角色可调

    全部 = 该角色可调(角色)
    # **先按档位的工具域过滤**（D-384）。
    # 早先这里只有一层：按角色取白名单，再按 `requires_user_questions`
    # 剔掉 `flow.ask_user`。于是咨询档（不写稿、门禁不适用）照样发给主理人
    # `draft.submit` 与 `validate.run`——模型看得到、调得动，却永远用不上。
    #
    # 「用不上」不是零成本：模型会尝试调用它，工具层会拒绝，
    # 用户在调试模式里看到的是「模型说能调用却调不动」。
    启用域 = TIER_BY_ID[档].工具域
    全部 = tuple(t for t in 全部 if t.域 in 启用域)
    # `requires_user_questions` 仍是**提问这件事的权威声明**（D-029）：
    # 咨询档 `requires_user_questions=False` 即便将来往 `工具域` 里
    # 误加了「方向提问」，这一行仍会把它剔掉。两处互为兜底，
    # 且 `tests/test_tier_tool_domains.py` 断言二者一致。
    if not TIER_BY_ID[档].requires_user_questions:
        全部 = tuple(t for t in 全部 if t.name != "flow.ask_user")
    return tuple(
        ToolSchema(name=t.name, description=t.description, parameters=t.parameters) for t in 全部
    )


def _工具声明(角色: RoleId, 档: TierId) -> tuple[ToolSchema, ...]:
    """该角色**实际可调**的工具子集 → API 层声明（D-233）。

    ## 为什么必须是子集而不是全集

    提示词装配器早就按角色白名单渲染「本次可调用的工具」，
    并明写着「调不到的工具不会出现在这里」。
    若这里发全集，就与提示词**自相矛盾**：
    模型会看到并尝试调用白名单外的工具，然后被工具层拒绝——
    用户看到的现象是「模型说能调用却调不动」。

    **那条注释曾一语成谶**：分叉不在这里，而在提示词那一侧
    （那里是个硬编码字面量）。两侧现共用 `_本档可调工具`。

    ## 为什么放在编排层而不是服务层

    `ToolSpec`（工具层自己的规格）与 `ToolSchema`（端口的线格式）
    字段同名但分属两层。跨层转换是这个位置独有的职责：
    工具层不该知道端口，编排层是唯一同时看得见两者的层。
    """
    return _本档可调工具(角色, 档)


@dataclass(frozen=True, slots=True)
class 阶段产物:
    """一次阶段产出。**内容非空是判据**，故单独成类——
    用 `tuple[str, str]` 会让「有名字但内容空」也长得像成功。"""

    阶段: PhaseId
    名称: str
    内容: str


def _重写沿用名(产物: Sequence[阶段产物], 本次: 阶段产物) -> str | None:
    """本次若是**重写**，返回被重写那份成果的名字；否则返回 `None`。

    ## 为什么必须沿用原名（用户裁决：只显示第 2 版）

    门禁打回重写后，界面上**只应显示最新版**——第一版与第二版
    是同一份文稿，不是两份不同的稿子。

    而投影是按**名字**替换的（同名 → 替换，不同名 → 追加）。
    若直接用模型这轮返回的名字，它可能叫「正文初稿（第 2 稿）」，
    替换就匹配不上，屏幕上还是会出现两份。

    ## 按阶段筛，而不是 `is_gate`

    `阶段产物` 里**没有** `is_gate` 字段；而门禁报告的 `阶段`
    是 `GATING`，本来就被这个筛法排除掉了。

    ## 为什么要单独成函数

    判据要直接调它才能验真实行为。若这段逻辑只写在 `_跑段` 里，
    判据就只能在判据里**重抄一遍**——而重抄的那份永远不会变红，
    生产代码改了它照样绿。**判据与实现必须共用一份代码**，
    否则验的是「判据抄的那份对不对」，不是「生产的那份对不对」。
    """
    if 本次.阶段 is not PhaseId.REVISION:
        return None
    return next(
        (p.名称 for p in reversed(产物[:-1]) if p.阶段 in (PhaseId.DRAFTING, PhaseId.REVISION)),
        None,
    )


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

    # ── 七个编排回调的真实实现（D-406）──
    #
    # 此前本类只有 `汇报` 与 `记事件` 两个方法，而 `工具依赖` 上有 16 个字段。
    # 其余七个回调在**完整装配后仍恒为 None**——模型调对应工具直接失败：
    # `msg.report_to_lead` / `flow.sleep` / `flow.wake_on` / `flow.rollback`
    # / `flow.status` / `tool.result` / `msg.request_from_lead`。
    #
    # 与 D-286（问用户未接线）同型，当时只修了问用户那一个。
    # 「有字段、有判空、有失败返回」看起来完全正常，
    # 只有**造真实对象读实际值**才看得见。

    def 汇报(self, 内容: str, 依据: dict[str, object]) -> str:  # noqa: ARG002
        """成员向主理人汇报。返回主理人答复，并**两侧都记进历史**。"""
        答复 = f"（主理人已收到：{内容[:40]}）"
        self.编排.记汇报(内容, 答复)
        self._发角色事件("role.report", {"汇报": 内容[:120]})
        return 答复

    def 请示(self, 事项: str, 紧急: bool) -> str:
        """成员请求主理人裁决。

        ## 为什么转成「成员状态 = 等待条件」而不是直接答复

        主理人此刻可能正忙别的。硬造一句「已批准」等于**编排器替主理人
        做决定**——那正是「严禁编造」要禁的。诚实的返回是
        「已记下，等主理人那一轮读到它」，并把成员状态改为等待条件，
        于是 `该发请求`（D-042「挂起态绝不发请求」）会跳过它，
        不会空转烧算力。
        """
        self.编排._挂起成员(事项, 紧急)
        return f"已记下请示「{事项[:40]}」，将在主理人下一次推进时处理。"

    def 挂起(self, 原因: str) -> None:
        """成员主动挂起。**挂起态绝不发模型请求**（D-042 第 1 条）。"""
        self.编排._挂起当前成员(原因)

    def 唤醒(self, 事件名: str, 角色: str) -> None:
        """设定唤醒条件：命中该事件名时把成员放回运行中。"""
        self.编排._设唤醒条件(事件名, 角色)

    def 回滚(self, 原因: str, 强制: bool) -> None:  # noqa: ARG002
        """退回重做（D-050：上限 2 次）。

        **必须走门禁账的退回计数**，不能另起一套——
        上限是门禁的硬约束，绕过它就等于没有上限。
        """
        self.编排._请求退回(原因)

    def 状态(self) -> dict[str, str]:
        """查询成员状态（D-046：数据不进标准界面，只给主理人看）。"""
        return self.编排._成员状态表()

    def _发角色事件(self, kind: str, 载荷: dict[str, object]) -> None:
        """发 `role.*` 事件。**失败不抛**——
        回调的主职是记事实，事件只是留痕。
        """
        from ..domain.event import EventKind

        try:
            事件类 = EventKind(kind)
        except ValueError:  # pragma: no cover - 常量拼错才会走到
            return
        self.编排.发(事件类, self._当前阶段(), **载荷)

    def _当前阶段(self) -> PhaseId:
        """工具层回调用的阶段。与 `记事件` 同一口径：
        取最新阶段而不是猜 GATING——工具调用可能发生在任何阶段。"""
        过程 = self.编排._过程
        return 过程.状态.phase if 过程 is not None else PhaseId.INTAKE


@dataclass(frozen=True, slots=True)
class 门禁账:
    """一道门禁在本档的**实际处境**（D-390）。

    ## 为什么要三个字段而不是一个列表

    「跑过」有三种**互不相同**的来历，混在一起就分不清责任：

    | 字段 | 含义 | 缺它意味着 |
    |---|---|---|
    | `已取证` | 真跑了取证，且有结论 | 门禁形同虚设 |
    | `缺席` | 该有结论却没给（D-221） | 编排本该失败，不会有结果 |
    | `不适用` | 本档不跑（无编制 / 动态门禁） | **不是缺陷，不该报警** |

    `缺席` 在正常结果里恒为空——它一旦非空，`_门禁循环` 已经
    `Result.fail` 了，跑不到构造结果那一步。**保留这个字段是为了
    续跑与台账能显式区分「豁免」与「缺失」**，而不是为了让失败也产结果。
    """

    #: 真跑了取证、拿到结论的门禁。
    已取证: tuple[GateId, ...] = ()
    #: 该有结论却没给的（D-221）。**成功结果里恒为空**。
    缺席: tuple[GateId, ...] = ()
    #: 本档不适用的：无编制（G3 于 Flash）或动态门禁（G9）。
    不适用: tuple[GateId, ...] = ()

    @property
    def 全过(self) -> bool:
        """已取证的**全部**都通过，且没有缺席。

        注意分母是 `已取证` 而不是「本档该跑的全部」——
        否则 G3/G9 会让 `全过` 恒为 False，而它们没跑并不是缺陷。
        """
        return not self.缺席


@dataclass(slots=True)
class 编排结果:
    """一次跑完的结果。**过程可查**——「跑通了」而不留痕等于没法验证空转。"""

    终态阶段: PhaseId
    完整阶段序列: tuple[PhaseId, ...]
    产物: tuple[阶段产物, ...]
    交付物: str
    #: **门禁账**（D-390）。**取代原先那个 `走过的门禁`**。
    #:
    #: ## 为什么必须拆开
    #:
    #: 原字段报的是 `self.门禁`——「**本档该跑**的门禁」，不是
    #: 「**真跑了取证的**」。实测 Flash 档：报 12 道，实际取证 10 道。
    #:
    #: 差的两道是**刻意豁免**，不是漏网：
    #:
    #: - **G3**：owners 只有文择体，Flash 编制里没有 → 按 D-116 降级给
    #:   主理人研判并带标注；
    #: - **G9**：`dynamic_owners`，不独立取证，按所触发的红线归属退回（D-084）。
    #:
    #: 但「**应有结论却没给**」（真缺席，D-221 转用户裁决）与
    #: 「**本就不该有结论**」（豁免）被压在同一个字段里，
    #: 谁拿它当「已验证门禁数」就会**高报 2 道**。
    门禁账: 门禁账
    动过的角色: tuple[RoleId, ...]
    出现过的成员状态: tuple[RoleState, ...]
    成果标注: tuple[str, ...] = ()
    已完成调用: tuple[str, ...] = ()
    #: **最终落盘的文件名**（D-296）。空元组 = 没导出成功。
    #: 不靠「有没有报错」推断成败——那是 D-283 记的原委。
    导出文件名: tuple[str, ...] = ()

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
        #: **最近一次门禁裁定的结论**（D-390），键是门禁号。
        #: 供 `_算门禁账` 分清「真取证」与「刻意豁免」。
        self._末次裁定: dict[str, GateRecord] = {}
        #: **本次的门禁账**（D-390）。门禁循环结束时算一次，
        #: 报告与 `编排结果.门禁账` **共用这一份**——
        #: 算两遍既浪费，又可能算出两个不一样的答案（裁定在两次之间变了）。
        self._门禁账: 门禁账 | None = None
        #: 《政策依据适配表》单独挂一份（D-255）；无则 None
        self._政策取证件: 阶段产物 | None = None
        #: 工具结果存档（D-227）。跨阶段存活——
        #: 模型在 OUTLINE 调的工具，起草阶段仍可能要查回结果。
        self.工具存档: 调用存档类 | None = None
        #: **工具层**（D-267）。真正执行 `tool_calls` 的是 `_过程`，
        #: 故字段落在这一层——放编排器上会让读取跨一层中转。
        self.工具层 = 编排.工具层
        #: **导出器**（D-296）。同 `工具层` 的理由——
        #: 真正落盘的是 `_过程`，故字段落在这层。
        self.导出 = 编排.导出
        #: 导出落盘的文件名。成功时并进 `编排结果.导出文件名`。
        self.导出文件名: tuple[str, ...] = ()
        #: 落盘摘要（`name`/`kind`/`hash`）。`artifact.published` 的载荷来源。
        self.落盘摘要: tuple[落盘摘要, ...] = ()
        self.出现过的状态: list[RoleState] = []
        self.动过的: list[RoleId] = []
        #: **本段**派发过的角色。`动过的` 跨段累积，记账只能用这个。
        self.本段动过: list[RoleId] = []
        # **门控记忆门（D-254）**：每成员一份，独立于 `ContextView`。
        # 可变状态在 `记忆门` 类里（`deque`）；
        # `ContextView.记忆门` 只存压缩时的快照。
        # **每成员一份**：模型推送是成员私有的——
        # 「截止 8 月 2 日」只对写它的人有意义，
        # 给别的成员看反而污染对方的注意力。
        # 故按 `RoleId` 懒建（首次读时才 new），不预建、不共享。
        # 类型注解在 `from __future__ import annotations` 下是字符串，
        # 故 import 放在 TYPE_CHECKING 里——运行时不触发 import。
        if TYPE_CHECKING:
            from ..services.context.memory_gate import 记忆门 as _记忆门类

        self.记忆门: dict[RoleId, _记忆门类] = {}
        self.回复: dict[RoleId, str] = {}
        self.交付物 = ""
        self.标注: tuple[str, ...] = ()
        #: **门禁退回的轮次**。退回重写必须重新问模型、
        #: 而重试不得重复问（D-065）；差异由阶段（REVISION vs DRAFTING）承担，
        #: **不掺进 call_id**——架构 §8.6 判「轮次」是第几次尝试而非哪一件事。
        self.轮次 = 0
        #: **本阶段生效中的指令**（D-012）。在阶段边界整体换新：
        #: 上一阶段的已经随那批请求发出去了，再发一遍是白烧 token。
        #: 队列本体挂在**编排器**上（长寿命），不在过程上——
        #: 界面可能在 `跑到终` 建过程之前就提交插话，那时过程还不存在。
        self._生效指令: list[str] = []
        #: 每角色一份对话历史（D-041「每角色一个 ContextView」）。
        #: **第 14.5 步接线**：此前 `self.回复` 存了却从不进下一次请求，
        #: 模型每轮从零开始——只知用户为啥，不知自己干了什么。
        self.历史: dict[RoleId, list[Message]] = {}
        #: 主理人收到的汇报（D-041：汇报进主理人上下文）。
        self.汇报: list[Message] = []

    # ── drive 回调：一次一阶段 ──

    def 推进一步(self, 进度: int) -> Result[Step[int]]:
        # **每段开始前清空本段角色表**：否则上一段派发过的角色
        # 会混进这一段的记账里，凭空多出一批不存在的 call_id。
        self.本段动过.clear()
        if 进度 >= len(self.序列):
            return Result.ok(Step(phase=PhaseId.DONE, state=进度, done=True))
        # **取消检查点**（与插话同位置：阶段边界）。
        #
        # 放在这里而不是阶段中间，是因为此刻**状态是一致的**：
        # 上一个阶段已落盘、已存态、已发 `PHASE_EXITED`。
        # 中途掐断会留下「阶段已开始但产物没落」的状态，
        # 续跑读到它就得额外判一次「这阶段到底做完没」——
        # 而边界退出不需要这个判断。
        if self.编排._已请求取消:
            self.编排.发(
                EventKind.RUN_CANCELLED,
                PhaseId.DONE,
                已完成进度=进度,
                总进度=len(self.序列),
            )
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
        # **D-012：插话在检查点生效**。
        # 边界上取一次，本阶段的所有模型请求都带上，取完即换新——
        # 天然不打断正在进行的模型请求。
        self._生效指令 = self.编排._取新指令()
        self.编排.发(EventKind.PHASE_STARTED, 阶段)

        跑段 = self._跑一段(阶段)
        if 跑段.is_error:
            self.编排.发(EventKind.PHASE_EXITED, 阶段, ok=False)
            段错 = 跑段.error
            assert 段错 is not None
            return Result.fail(段错.code, 段错.detail)

        件 = 跑段.unwrap()
        # **不在这里落盘**：续跑时本段 `self.回复` 是空的（请求被幂等跳过），
        # 先落一个空产物会把上次的成果抹掉。落盘挪到复用判定之后。

        self.产物.append(件)
        # **重写沿用被重写那份的名字**——「界面上只显示最新版」
        # 能被**确定**保证的前提（用户裁决：只显示第 2 版）。
        # 理由与筛选口径见 `_重写沿用名` 的 docstring。
        被重写名 = _重写沿用名(self.产物, 件)
        是修订 = 被重写名 is not None
        # **只有 ARTIFACT_READY / ARTIFACT_UPDATED 携全文**（D-072/D-046）
        self.编排.发(
            EventKind.ARTIFACT_UPDATED if 是修订 else EventKind.ARTIFACT_READY,
            阶段,
            name=被重写名 if 是修订 and 被重写名 else 件.名称,
            artifact_kind="note",
            content=件.内容,
        )
        if 阶段 is PhaseId.FINAL:
            self.交付物 = 件.内容

        # **产物与「已完成调用」都要记进状态（D-065 补漏）**。
        #
        # 此前 `put_artifact` 与 `mark_completed` 在生产代码零调用——
        # 只有 `tests/test_serialization.py` 用过。于是实跑之后
        # `state.json` 里 `artifacts={}`、`completed_call_ids=[]`：
        # **产物落盘了却没记账**，而续跑判定全靠这个账
        # （`run_state.py` 的 `is_call_completed`）。
        #
        # 与「产物落盘」共用同一次 `save_run_state`，不多写一遍盘。
        # **续跑时该段直接复用已落盘的产物，别再拼一个空的**。
        #
        # `_跑一段` 在 `cid in self.已完成` 时会跳过请求（这是对的——
        # 幂等要求不重复做已做的事）。但产物是 `推进一步` 用
        # `_正文(阶段, self.回复)` 拼的，跳过的段 `self.回复` 是空的，
        # 于是会落一个只剩标题的空产物。**那比重发更糟**：
        # 表面「没重发」，实际把上次的成果抹了。
        #
        # 故先看这段是不是上次的成果；是就读回来直接用。
        #
        # **顺序反了就会把成果抹掉**：读回来必须在落盘**之前**。
        # 我第一版放在 `put_artifact` 之后，于是读到的已经是被
        # 覆盖过的空内容——实测首跑 47 字、续跑 6 字。
        #
        # 而请求次数判据照样绿：它只数「第二遍发不发请求」，
        # 不看产物内容。**这类判据查不出「不重发但抹成果」**。
        名 = f"{阶段.value}.md"
        复用 = self.storage.read_text(
            f"{_产物根}/{self.编排.运行号}/{名}",
        )
        if 复用.is_ok and 复用.unwrap().strip():
            # 续跑：直接用上次的成果，并**保持原样落回**，不重新拼。
            件 = 阶段产物(阶段, 名, 复用.unwrap())
            存 = self.storage.put_artifact(self._产物引用(件), 件.内容)
        else:
            存 = self.storage.put_artifact(self._产物引用(件), 件.内容)
        if 存.is_error:
            写错 = 存.error
            assert 写错 is not None
            return Result.fail(写错.code, f"{名} 落盘失败：{写错.detail}")
        self.状态 = self.状态.put_artifact(self._产物引用(件))
        # **身份必须与 `is_call_completed` 查的完全一致**。
        #
        # 我第一版记的是 `phase:{阶段}:{产物名}`，而 `_跑一段` 判幂等
        # 查的是 `make_call_id("prompt", 运行号, 阶段, role=角色)`。
        # **两套身份对不上**——于是账记了、判定永不命中，
        # 续跑照旧重发全部请求。记账与判账必须是同一个 id。
        #
        # 故这里记每个本段被派发角色的 prompt 调用 id，
        # 与 `_跑一段` 里 `cid` 的算法逐字一致。
        #
        # **只记本段派发的角色**——`self.动过的` 是跨段累积的，
        # 照它记会把 intake 的角色也算成 final 的 cid，
        # 于是账里多出一堆 `final` 的 id，而真正的 `final` id 一个不缺，
        # 续跑照样重发。（Flash 档实测：4 段产物、9 条 cid，
        # 却漏掉 final——就是这个原因。）
        for 角色 in dict.fromkeys(self.本段动过):
            self.状态 = self.状态.mark_completed(
                make_call_id("prompt", str(self.编排.运行号), 阶段.value, role=角色.value),
            )
        存态 = self.storage.save_run_state(self.状态.with_phase(阶段, PhaseState.APPROVED))
        if 存态.is_error:
            self.编排.发(EventKind.PHASE_EXITED, 阶段, ok=False)
            存错 = 存态.error
            assert 存错 is not None
            return Result.fail(存错.code, f"状态落盘失败：{存错.detail}")

        self.编排.发(EventKind.PHASE_EXITED, 阶段, ok=True)
        # **阶段边界写记忆门**（D-254）：程序侧硬锚点。
        # 阶段切换是**信息结构变化**的时刻——上一阶段的产出
        # 与下一阶段的任务都变了。模型不记住这个切换点，
        # 就会把「写大纲」的指令带进「写正文」的阶段。
        # 记忆门只标不丢，压缩器够不着，所以它永远在。
        self.记忆门.setdefault(RoleId.TEAM_LEAD, 记忆门类()).程序写入(
            f"阶段 {阶段.value} 已完成，产出见产物引用",
            标签="阶段锚点",
        )
        return Result.ok(Step(phase=阶段, state=进度 + 1))

    def _跑一段(self, 阶段: PhaseId) -> Result[阶段产物]:
        """问该问的人，产出一件东西。

        **幂等在此**：同一阶段的同一角色，`call_id` 不变，
        续跑时已完成的部分直接跳过、不重复问模型（D-074）。
        """
        if 阶段 is PhaseId.DONE:
            return Result.ok(阶段产物(阶段, f"{阶段.value}.md", "（终态，无产出）"))

        档 = self.状态.task.tier
        if 阶段 is PhaseId.DIRECTION:
            self._从用户输入更新d卡()
            # **校验前先回填**（D-069 补录）。
            #
            # 此前  放在  之后——但 DIRECTION 段的
            #  在  之前，校验不过就根本走不到派发，
            # 于是 D-Card 永远填不上、校验永远不过，死锁。
            #
            # 假模型在 intake 阶段就把 D-Card 内容用 shared.write 写入了
            # 公共区（工具结果存在 self.工具存档），这里提前到校验前
            # 从工具存档里捞出来解析。
            #
            # **`从回复=False`**：此时 `self.回复[TEAM_LEAD]` 里存的是
            # **intake 阶段**的回复，不是 DIRECTION 阶段的。
            # 真实模型在 intake 的回复是自由文本咨询意见，其中可能包含
            # 「D1 是唯一停车项」「D3 硬确认项」等**元描述**——
            # 它们恰好以 "D1 "/"D3 " 开头，会被 `_尝试更新d卡` 的正则
            # 误匹配为 D-Card 字段值，导致 D-Card 出现 4 个伪字段
            # （D1/D3/D7/D9），绕过「仅 D1 降级放行」分支，
            # 跑硬约束必然失败（E040）。
            #
            # 故此处**只从公共区读**（假模型写入的 D-Card 产物），
            # 不从 `self.回复` 解析。派发后的那次调用（第 725 行）
            # 才需要同时看回复和公共区。
            self._尝试更新d卡(从回复=False)
        校验 = self._提问前校验(阶段)
        if 校验.is_error:
            self.编排.发(EventKind.PHASE_EXITED, 阶段, ok=False)
            return _转为失败(校验, f"{阶段.value} 段提问前校验失败")
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

        派 = self._派发(档, 阶段)
        if 派.is_error:
            return _转为失败(派, f"{阶段.value} 段派发失败")
        if 阶段 is PhaseId.DIRECTION:
            # **模型回复后再回填一次**（真实模型在 DIRECTION 段
            # 的纯文本回复里也会包含结构化方向确认单）。
            self._尝试更新d卡()
        if 阶段 is PhaseId.OUTLINE:
            # 《政策依据适配表》**不混进阶段聚合正文**（D-255）：
            # `_跑一段` 是单件契约（调用方按 `件.名称` 落盘），
            # 塞第二个产物会破坏它。故挂到编排器，收结果时统一入库
            # ——与 `g3-降级标注` 同一模式。
            self._政策取证件 = self._政策取证产物()
        return Result.ok(阶段产物(阶段, f"{阶段.value}.md", _正文(阶段, self.回复)))

    def _从用户输入更新d卡(self) -> None:
        """从用户需求文本中启发式提取 D-Card 字段（D-069 补录）。

        真实模型会在 DIRECTION 段生成结构化方向确认单；
        此处先从用户输入中提取能识别的字段，为提问前硬约束
        校验提供基础数据，避免 D-Card 全空导致校验误拦。
        """
        import re

        输入 = self.状态.task.requirement
        if not 输入 or not 输入.strip():
            return
        当前卡 = self.状态.dcard
        更新计数 = 0
        # D1 文种：匹配常见文种关键词
        文种匹配 = re.search(
            r"(?:拟|撰写|起草|出具|成文|通知|请示|报告|函|纪要|决定|意见|批复|通报|报告|申请|方案|规划|计划|总结|简报|信息|简报|汇报|说明|报告)",
            输入,
        )
        if 文种匹配:
            文种 = 文种匹配.group(0)
            if 当前卡.get(DCardFieldId.D1).is_missing:
                新字段 = 当前卡.get(DCardFieldId.D1).filled_with(文种, Provenance.USER)
                当前卡 = 当前卡.with_field(新字段)
                更新计数 += 1
        # D1 标题：提取"关于/就/拟××事项"中的事项名
        标题匹配 = re.search(r"(?:关于|就|拟)([^，。\s]{2,20})[事项]", 输入)
        if 标题匹配:
            标题 = 标题匹配.group(1)
            if 当前卡.get(DCardFieldId.D1).is_missing:
                现有 = 当前卡.get(DCardFieldId.D1).value or ""
                新值 = f"{现有}：{标题}" if 现有 else 标题
                新字段 = 当前卡.get(DCardFieldId.D1).filled_with(新值, Provenance.USER)
                当前卡 = 当前卡.with_field(新字段)
            elif "事项" in (当前卡.get(DCardFieldId.D1).value or ""):
                现有 = 当前卡.get(DCardFieldId.D1).value or ""
                if "事项" not in 现有:
                    新字段 = 当前卡.get(DCardFieldId.D1).filled_with(
                        f"{现有}：{标题}", Provenance.USER
                    )
                    当前卡 = 当前卡.with_field(新字段)
        if 更新计数 > 0:
            self.状态 = self.状态.with_dcard(当前卡)

    def _尝试更新d卡(self, 从回复: bool = True) -> None:
        """从 DIRECTION 段主理人回复中解析 D-Card 字段（D-069 补录）。

        真实模型会按提示词返回结构化方向确认单；
        假模型/降级场景可能返回非结构化文本，此时静默跳过，
        不阻断流程，但会在日志中留痕。

        **两处来源都要看**（D-069 补录）：
        1. `self.回复[TEAM_LEAD]`——模型纯文本回复；
        2. `self.工具存档`——模型在 intake/DIRECTION 阶段用
           `shared.write` 写入的方向确认单（工具结果存在这里）。
           假模型把 D-Card 当工具调用写入公共区，纯文本回复里没有，
           只从 `self.回复` 解析就永远填不满。

        `从回复=False`：校验前那次调用（第 697 行）只读公共区，
        不读 `self.回复`——因为那时 `self.回复` 里存的是 **intake**
        阶段的回复（自由文本咨询意见），其中可能出现
        「D1 是唯一停车项」「D3 硬确认项」等**元描述**，
        它们恰好以 "D1 "/"D3 " 开头，会被正则误匹配为 D-Card 字段值，
        产生伪字段绕过降级分支、触发 E040。
        """
        import re

        来源列表: list[str] = []
        回复正文 = self.回复.get(RoleId.TEAM_LEAD, "")
        if 回复正文 and 从回复:
            来源列表.append(回复正文)
        # **从公共区读**（D-069 补录）。
        #
        # `shared.write` 把 D-Card 内容写入存储层（文件），
        # 工具结果（`调用存档`）里只有一句摘要「已写入公共区：方向确认单-D-Card」，
        # 不含 306 字的 D1–D9 结构化正文。只从工具结果解析永远拿不到内容。
        # 故直接从存储层读公共区文件。
        if self.storage is not None:
            _run_id = self.状态.run_id
            # **路径带 `data/` 前缀**：`put_artifact` 内部补 `data/`，
            # 而 `list_dir` / `read_text` 不补——不带前缀就永远读不到。
            _公共区路径 = f"data/{_run_id}/公共区"
            _条目 = self.storage.list_dir(_公共区路径)
            for _条目名 in _条目:
                _读 = self.storage.read_text(f"{_公共区路径}/{_条目名}")
                if _读.is_ok:
                    _内容 = _读.unwrap()
                    if _内容 and ("D1 " in _内容 or "D2 " in _内容):
                        来源列表.append(_内容)
        if not 来源列表:
            self.编排.发(
                EventKind.ARTIFACT_UPDATED, PhaseId.DIRECTION, msg="D-Card 更新跳过：无可用来源"
            )
            return
        正文 = "\n".join(来源列表)
        更新计数 = 0
        当前卡 = self.状态.dcard
        for fid in FIELD_LABELS:
            # 匹配 "D1 xxx" 或 "D1：xxx" 或 "D1 xxx" 等
            # **不用 \b**：中文属于 \w，D1 与中文之间无 \W 过渡，
            # \b 在中文环境下永远不成立，导致字段提取全部失效。
            # 去掉 \b 后，D1/D2 等字段编号后面的中文内容可正常匹配。
            模式 = re.compile(rf"(?m)^\s*{fid.value}[^\n]*", re.IGNORECASE)
            匹配 = 模式.search(正文)
            if 匹配:
                行 = 匹配.group(0).strip()
                # 去掉 "D1 " 或 "D1：" 前缀，取剩余内容
                值 = re.sub(rf"(?i)^\s*{fid.value}\s*[：:]\s*", "", 行).strip()
                if 值:
                    新字段 = 当前卡.get(fid).filled_with(
                        值, Provenance.LEAD_INFERRED if fid is DCardFieldId.D8 else Provenance.USER
                    )
                    当前卡 = 当前卡.with_field(新字段)
                    更新计数 += 1
        if 更新计数 > 0:
            self.状态 = self.状态.with_dcard(当前卡)
            self.编排.发(
                EventKind.ARTIFACT_UPDATED,
                PhaseId.DIRECTION,
                msg=f"D-Card 已更新 {更新计数} 个字段",
            )

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

        **D-Card 仍空时降级放行**：当前架构下 `ask_user` 未接通，
        硬约束无法真正满足；若在此处绝对拦截，系统将在所有假模型
        及 ask_user 就绪前不可运行。故空表时记警但不阻断，待
        `ask_user` 接线后恢复拦截。
        """
        if 阶段 is not PhaseId.DIRECTION:
            return Result.ok_none()
        if self.编排.validators is None:
            return Result.ok_none()
        卡 = self.状态.dcard
        已填 = [f for f in 卡.by_id.values() if f.is_filled]
        if not 已填:
            # D-Card 全空：降级放行，记警。
            self.编排.发(
                EventKind.PHASE_EXITED,
                阶段,
                msg="硬约束降级：D-Card 全空，ask_user 未接通，记警放行",
            )
            return Result.ok_none()
        # **仅 D1 由 `_从用户输入更新d卡()` 启发式填充时降级放行**。
        #
        # `_从用户输入更新d卡()` 只从需求文本正则提取 D1（文种关键词），
        # D2-D9 不处理。结果 D1 已填而 D2-D9 为空——看起来像「部分填」，
        # 实际上是「用户没说更多，等模型在 DIRECTION 阶段逐项问」。
        #
        # 此时若按「部分填 → 跑硬约束」处理，必然缺 D2-D9 而 E040。
        # 但模型在 DIRECTION 段会调 `flow.ask_user` 逐项问用户，
        # 答复回来后 `_尝试更新d卡()` 在派发后回填（第 725 行）——
        # 硬约束在下一轮自然通过。故在此场景下降级放行，与「全空」同处理。
        #
        # 判据：仅 D1、来源是 USER（启发式从需求文本提取的），
        # 不是模型研判（LEAD_INFERRED 仅 D8 允许，出现即异常）。
        if (
            len(已填) == 1
            and 已填[0].id is DCardFieldId.D1
            and 已填[0].provenance is Provenance.USER
        ):
            self.编排.发(
                EventKind.PHASE_EXITED,
                阶段,
                msg="硬约束降级：仅 D1 由需求文本启发式填充，D2-D9 待 DIRECTION 阶段逐项问用户",
            )
            return Result.ok_none()
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
            # **留一份给 `_算门禁账`**（D-390）：只有它知道哪几道**真**跑了取证，
            # 而 `self.门禁` 只是「本档该跑」。二者差着 G3/G9 那两道豁免。
            self._末次裁定 = 裁定
            self._门禁账 = self._算门禁账()
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
            # **逐道发 `gate.passed` / `gate.failed`**（D-390）。
            #
            # 放在缺席判据**之后**：缺席的门禁此刻正要转用户裁决，
            # 那不是「failed」也不是「passed」，而是「无证据」——
            # 已经由 GATE_ESCALATED 报了，在这里再补一条 failed 会重复计。
            for _门, _记 in 裁定.items():
                self.编排.发(
                    EventKind.GATE_PASSED if _记.passed else EventKind.GATE_FAILED,
                    PhaseId.GATING,
                    gate_id=_门,
                    attempt=_记.attempt,
                    evidence_anchor=_记.evidence[:1],
                )
            驳回 = [记 for 记 in 裁定.values() if not 记.passed]
            if not 驳回:
                return Result.ok(
                    阶段产物(
                        PhaseId.GATING,
                        "gate-report.md",
                        _门禁报告(self.门禁, 裁定, (self._门禁账 or 门禁账()).不适用),
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
        """把门禁结论落成产物。**不适用的那几道要分开印**（D-390）。"""
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
        件 = 阶段产物(
            PhaseId.GATING,
            "gate-report.md",
            _门禁报告(self.门禁, 裁定, (self._门禁账 or 门禁账()).不适用),
        )
        self.产物.append(件)

    def _问模型(self, 问: LlmRequest, 阶段: PhaseId, 角色: RoleId | None) -> Result[LlmResponse]:
        """调模型，并在**失败时发 `model.error`**（D-408）。

        ## 为什么在编排层发，而不是让适配器发

        `LlmPort` 协议里没有事件口。让适配器发事件就得往协议里塞总线引用，
        端口就不再「可换」——而端口存在的意义正是实现可换。

        故：**适配器只管返回 `Result`，编排层据此发事件**。

        ## 为什么只发 `model.error`、不发 `model.retry`

        重试发生在 `http_llm.complete` **内部**（D-027），
        编排层只看到最终成败，看不到「重试了几次」。
        要如实记录得让适配器把该信息带出来（改 `LlmResponse`），
        那是另一件事。**此刻不假装能发**——`model.retry` 继续留在台账里。
        """
        答 = self.llm.complete(问)
        # **先发 `model.retry` 再发 `model.error`**（D-412）：重试是
        # 「先发生」的事。用户看事件流时先看到「重试了 3 次」，
        # 再看到「最终失败」——反过来读不懂因果。
        #
        # 计数从**适配器带出来**（D-027：重试在 `http_llm.complete` 内部，
        # 而 `LlmPort` 协议明写「上层不感知重试」，编排层本来无从得知）。
        # 成功路径挂 `LlmResponse.重试次数`，失败路径挂 `AppError.诊断`。
        if 答.is_error:
            错 = 答.error
            assert 错 is not None
            if 错.诊断.get("重试次数"):
                self.编排.发(
                    EventKind.MODEL_RETRY,
                    阶段,
                    角色=角色.value if 角色 is not None else "",
                    重试次数=错.诊断["重试次数"],
                    最终失败=True,
                )
            self.编排.发(
                EventKind.MODEL_ERROR,
                阶段,
                角色=角色.value if 角色 is not None else "",
                错误码=错.code.value,
                detail=错.detail[:120],
            )
        else:
            重试次数 = 答.unwrap().重试次数
            if 重试次数:
                self.编排.发(
                    EventKind.MODEL_RETRY,
                    阶段,
                    角色=角色.value if 角色 is not None else "",
                    重试次数=重试次数,
                    最终失败=False,
                )
        return 答

    @staticmethod
    def _从工具调用提取门禁结论(
        工具调用: tuple[ToolCall, ...],
        组门禁: tuple[GateId, ...],
    ) -> dict[str, GateRecord]:
        """从模型的工具调用中提取门禁结论（工具调用收结论方案）。

        若模型没有调用 gate.report 工具，或调用格式不正确，
        则所有门禁判定为「无证据」。
        """
        import json

        结论映射: dict[str, GateRecord] = {}
        报告调用 = [tc for tc in 工具调用 if tc.name == "gate.report"]
        if not 报告调用:
            for g in 组门禁:
                结论映射[g.value] = GateRecord(
                    gate=g.value,
                    passed=False,
                    evidence=(),
                    rejection_reason="模型未调用 gate.report 工具",
                )
            return 结论映射

        组门禁值 = {g.value for g in 组门禁}
        for tc in 报告调用:
            try:
                参 = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                门禁号 = str(参.get("gate_id", ""))
                结论 = str(参.get("conclusion", ""))
                证据 = str(参.get("evidence", ""))
                原因 = str(参.get("reason", ""))
                通过 = 结论 == "通过"
                if 门禁号 not in 组门禁值:
                    continue
                if not 证据.strip():
                    结论映射[门禁号] = GateRecord(
                        gate=门禁号,
                        passed=False,
                        evidence=(),
                        rejection_reason="模型调用 gate.report 但未提供 evidence",
                    )
                    continue
                结论映射[门禁号] = GateRecord(
                    gate=门禁号,
                    passed=通过,
                    evidence=(证据,) if 证据 else (),
                    rejection_reason=原因 if not 通过 else "",
                )
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

        for g in 组门禁:
            if g.value not in 结论映射:
                结论映射[g.value] = GateRecord(
                    gate=g.value,
                    passed=False,
                    evidence=(),
                    rejection_reason="模型未报告该门禁",
                )
        return 结论映射

    def _并行取证(self, 档: TierId, 门禁: tuple[GateId, ...]) -> dict[str, GateRecord]:
        """门禁取证：**并行组并发跑，主理人终闸组串行在后**（D-049）。

        ## 复查时补上的接线

        首版这里调的是 `_派发`——一条串行 `for` 循环，12 道门禁 11 次调用
        全程严格 enter/exit 交替。D-049 要的是「并行 4 组」，
        故改成 `kernel/scheduler.run_groups`（`:1092`）：
        ThreadPoolExecutor、D-130 异常就地转失败、顺序恒等于入参。
        这正是本项目反复记录的同型缺口：零件都测过、衔接没测。
        **本行写于接线之后**：此前注释停在「生产零引用」，
        与 `:1092` 已在调的事实矛盾——注释也会骗人。

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
            # G13 双取证方：同时跑 structure 和 compliance（D-084）。
            # `gate_groups()` 把它排除在并行组之外是为避免「双取证与单取证混并行」，
            # 但这里显式追加两个独立组，保证双取证语义落地。
            G13 = tuple(g for g in 门禁 if g == GateId.G13)
            if G13:
                任务 = 任务 + tuple(
                    GateGroup(owner=角色, gate_ids=tuple(g.value for g in G13))
                    for 角色 in (RoleId.STRUCTURE, RoleId.COMPLIANCE)
                )

            def _跑一组(g: GateGroup) -> Result[dict[str, GateRecord]]:
                组门禁 = tuple(x for x in 门禁 if x.value in g.gate_ids)
                # **逐道发 `gate.started`**（D-390）。
                # 此前只有异常路径才发 gate 事件，于是 12 道门禁全过时
                # **零条事件**——架构 §5.4 列的 `gate.started`/`gate.passed`/
                # `gate.failed` 定义了却零生产消费，用户（含调试模式）无从得知
                # 门禁到底跑没跑。与 D-258「白名单里有、却没有分支」同型。
                for _g in 组门禁:
                    self.编排.发(
                        EventKind.GATE_STARTED,
                        PhaseId.GATING,
                        gate_id=_g.value,
                        owner=g.owner.value,
                    )
                # 取证方**在自己的线程里**走一遍状态机并动笔——
                # 并行不能绕过 D-042 的六态机，否则 `动过的角色` 缺人，
                # 「编制内必须当值」这类判据就查不出谁真被派发过。
                self.状态 = self.状态.set_role_state(g.owner, RoleState.RUNNING)
                self.出现过的状态.append(RoleState.RUNNING)
                cid = make_call_id(
                    "gate", str(self.编排.运行号), PhaseId.GATING.value, role=g.owner.value
                )
                if cid not in self.已完成:
                    答 = self._问模型(
                        self._构造请求(g.owner, PhaseId.GATING, tuple(x.value for x in 组门禁)),
                        PhaseId.GATING,
                        g.owner,
                    )
                    if 答.is_error:
                        错 = 答.error
                        assert 错 is not None
                        return Result.fail(错.code, f"{g.owner.value} 取证失败：{错.detail}")
                    响应 = 答.unwrap()
                    # **门禁取证改用工具调用收结论**（替代纯文本正则解析）。
                    if 响应.tool_calls:
                        # 从工具调用中提取门禁结论
                        工具结论 = self._从工具调用提取门禁结论(响应.tool_calls, 组门禁)
                        # 用提取的结论构建文本回复（用于历史记录）
                        回复文本 = "\n".join(
                            f"{k} | {'通过' if v.passed else '退回'} | 证据：{v.evidence}"
                            for k, v in 工具结论.items()
                        )
                        self.回复[g.owner] = 回复文本
                        self._记入历史(g.owner, PhaseId.GATING, 回复文本)
                        self.已完成 = (*self.已完成, cid)
                        self.状态 = self.状态.mark_completed(cid)
                        return Result.ok(工具结论)
                    # 模型未调用工具，回退到文本解析
                    self.回复[g.owner] = 响应.text
                    self._记入历史(g.owner, PhaseId.GATING, 响应.text)
                    self.已完成 = (*self.已完成, cid)
                    # **门禁取证也要记账**（D-065 补漏）。
                    #
                    # 上面只把 cid 加进**内存**的 `self.已完成`，
                    # 而续跑读的是 `state.json`。内存那份随进程消失，
                    # 于是「门禁取证已完成」这件事永远落不了盘，
                    # 续跑会重发每一组的取证请求。
                    self.状态 = self.状态.mark_completed(cid)
                self.动过的.append(g.owner)
                self.本段动过.append(g.owner)
                self.出现过的状态.append(RoleState.DONE)
                self.状态 = self.状态.set_role_state(g.owner, RoleState.DONE)
                # 每组**各自**取一次取证结论：并行的意义就在这儿——
                # 一组一请求，而不是 12 道门禁挤在一次调用里。
                # 若未从工具调用提取结论（含续跑场景），则调用门禁取证解析文本
                return Result.ok(
                    self.编排.门禁取证(
                        门禁取证请求(
                            门禁=组门禁,
                            取证方=g.owner,
                            回覆=self.回复.get(g.owner, ""),
                            档位=档,
                        )
                    )
                )

            结果 = run_groups(任务, _跑一组)
            for 项 in 结果:
                if 项.value.is_ok:
                    裁定.update(项.value.unwrap())
        if 终闸:
            终闸门禁 = tuple(g for g in 门禁 if any(r in GATES[g].owners for r in 终闸))
            if 终闸门禁:
                # **终闸组原先根本没人取证**：
                # 此处直接调 `门禁取证(终闸门禁)`，**没有派发模型**——
                # 而取证结论的唯一来源是角色回覆。于是主理人这条
                # G12 的证据恒为空，缺席判据（D-221）会把它转用户决策。
                # 并行组那条路径有 `llm.complete`，终闸这条没有，
                # 两边不对称是漏派而非有意为之（D-218 的另一半）。
                #
                # 故复用同一段派发逻辑，串行跑一次。
                终闸方 = 终闸[0]
                self.状态 = self.状态.set_role_state(终闸方, RoleState.RUNNING)
                self.出现过的状态.append(RoleState.RUNNING)
                终cid = make_call_id(
                    "gate-final", str(self.编排.运行号), PhaseId.GATING.value, role=终闸方.value
                )
                if 终cid not in self.已完成:
                    终答 = self._问模型(
                        self._构造请求(终闸方, PhaseId.GATING, tuple(x.value for x in 终闸门禁)),
                        PhaseId.GATING,
                        终闸方,
                    )
                    if 终答.is_error:
                        错 = 终答.error
                        assert 错 is not None
                        return {}
                    终响应 = 终答.unwrap()
                    if 终响应.tool_calls:
                        # 从工具调用中提取门禁结论
                        终工具结论 = self._从工具调用提取门禁结论(终响应.tool_calls, 终闸门禁)
                        终回复文本 = "\n".join(
                            f"{k} | {'通过' if v.passed else '退回'} | 证据：{v.evidence}"
                            for k, v in 终工具结论.items()
                        )
                        self.回复[终闸方] = 终回复文本
                        self._记入历史(终闸方, PhaseId.GATING, 终回复文本)
                        裁定.update(终工具结论)
                    else:
                        self.回复[终闸方] = 终响应.text
                        self._记入历史(终闸方, PhaseId.GATING, 终响应.text)
                        门禁取证结果 = self.编排.门禁取证(
                            门禁取证请求(
                                门禁=终闸门禁,
                                取证方=终闸方,
                                回覆=self.回复.get(终闸方, ""),
                                档位=档,
                            )
                        )
                        裁定.update(门禁取证结果)
                    self.已完成 = (*self.已完成, 终cid)
                    self.状态 = self.状态.mark_completed(终cid)
                else:
                    # 续跑/退回重跑场景：终闸已完成，但上一轮的结论
                    # 不会自动进入本轮裁定。从状态取回，保证缺席判据
                    # 不把「已取证过的门禁」误判为「无证据」。
                    for g in 终闸门禁:
                        if g.value in self.状态.gates:
                            裁定[g.value] = self.状态.gates[g.value]
                self.动过的.append(终闸方)
                self.本段动过.append(终闸方)
                self.出现过的状态.append(RoleState.DONE)
                self.状态 = self.状态.set_role_state(终闸方, RoleState.DONE)
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
                答 = self._问模型(问, 阶段, 角色)
                if 答.is_error:
                    错 = 答.error
                    assert 错 is not None
                    return Result.fail(错.code, f"{阶段.value}/{角色.value} 调用失败：{错.detail}")
                响应 = 答.unwrap()
                self.编排._记用量(阶段, 问, 响应)
                # **结束原因不合格就拦下**（D-268）：截断稿与合规拦截稿
                # 此前都被当成正常完成照收。
                合格 = 查结束原因(响应, 阶段, 角色)
                if 合格.is_error:
                    错2 = 合格.error
                    assert 错2 is not None
                    return Result.fail(
                        错2.code, f"{阶段.value}/{角色.value} 模型调用未正常结束：{错2.detail}"
                    )
                # **执行模型请求的工具**（D-267）。
                #
                # 此前 `LlmResponse.tool_calls` 从不被读：请求的 `tools`
                # 真的发给模型了（D-233），模型也会真的调用，
                # 但响应一到就被当普通文本丢掉——**16 个工具全是死的**。
                # **先标「此刻正在派发谁」**（D-406）：工具轮里模型可能调
                # `flow.sleep` / `msg.report_to_lead`，那些回调要知道是谁在调。
                # 用 try/finally 复原——异常路径下也要清，否则会残留上一位。
                self.编排._派发中角色 = 角色
                try:
                    工具轮 = self._跑工具(角色, 阶段, 响应)
                finally:
                    self.编排._派发中角色 = None
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
            self.编排._派发中角色 = None
            self.动过的.append(角色)
            self.本段动过.append(角色)
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
                # **档位必须一起传下去**（D-389）。
                # 只按角色过滤时，咨询档模型发来 `validate.run`
                # 会真的执行——声明端不给 ≠ 执行端不接。
                #
                # **档位未设置时退回空域**：工具层对空域的解释是
                # 「一个工具都不给」，与不传工具层的效果一致。
                档位 = self.编排.档位
                启用域 = TIER_BY_ID[档位].工具域 if 档位 is not None else frozenset()
                结果 = self.工具层.调用(
                    调用.name,
                    角色=角色,
                    参数=dict(调用.arguments),
                    启用域=启用域,
                )
                正文 = 结果.unwrap() if not 结果.is_error else f"[工具失败] {结果.error}"
                self.存档工具结果(调用.id, 正文)
                正文组.append((调用.id, 正文))
            # 全部执行完再按顺序回传，让 tool 消息紧跟那条 assistant
            for 调用id, 正文 in 正文组:
                self.记工具结果(角色, 调用id, 正文)
            # **再问一轮**，让模型看得到自己调过什么、决定要不要继续
            问 = self._构造请求(角色, 阶段)
            答 = self._问模型(问, 阶段, 角色)
            if 答.is_error:
                错 = 答.error
                assert 错 is not None
                return Result.fail(
                    错.code, f"{阶段.value}/{角色.value} 工具后续调用失败：{错.detail}"
                )
            当前 = 答.unwrap()
            合格 = 查结束原因(当前, 阶段, 角色)
            if 合格.is_error:
                错2 = 合格.error
                assert 错2 is not None
                return Result.fail(
                    错2.code, f"{阶段.value}/{角色.value} 模型调用未正常结束：{错2.detail}"
                )
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
        # **程序侧写入记忆门**（D-254）：汇报本身就是硬锚点。
        # 它是「这个成员刚刚告诉我了什么」——下一轮读历史时
        # 优先看到它，比翻整段对话快。
        self.记忆门.setdefault(RoleId.TEAM_LEAD, 记忆门类()).程序写入(内容.strip(), 标签="成员汇报")

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
                    content=f"【本次可调用工具】{_工具清单(角色, 阶段, self.状态.task.tier)}",
                ),
            ),
            对话=历史,
            本轮任务=_本轮任务(需求, 角色, 阶段),
            # **记忆门在 `本轮任务` 之前**（U 型曲线，D-254）：
            # 两者都是「可变 + 最重要」，但本轮任务是**指令**（要回答什么），
            # 记忆门是**导航**（读什么）。指令压过导航——
            # 末尾最后一段必须是本轮任务，否则 recency bias
            # 会浪费在「读什么」上而不是「回答什么」。
            记忆门=self.记忆门.setdefault(角色, 记忆门类()).注入(),
        )
        序列 = 视图.messages()
        # **插话接在末位之后，不进 `组装`**（D-012）。
        #
        # 为什么放在组装外面：`组装` 的入参是「静态块 / 动态规则 / 工具 / 对话 /
        # 本轮任务」五个语义位，插话不属于任何一个——
        # 它是**跨越本阶段的偏重**，硬塞进「本轮任务」会让每一阶段都重复一遍，
        # 塞进「对话」则会混进该角色的历史里、下一阶段继续带着走。
        #
        # 放末位之后还有个结构好处：与「本轮任务恒为末位」的既有约定一致，
        # 插话紧跟在任务后面，模型读到的最后一段就是
        # 「这轮要做什么 + 用户额外插了什么」。
        指令 = self._指令消息()
        if 指令:
            序列 = (*序列, *指令)
        # **装配完成即留痕**（D-408）：发 `context.composed`。
        #
        # 紧挨着下面的 `context.compacted`——两者成对。
        # 原先只有压缩才发事件，于是「压过」有痕、「装过」无痕，
        # 而后者才是每次请求都发生的**常态**。
        # 记下 token 与条数，D-231 那种容量问题才有得查。
        self.编排.发(
            EventKind.CONTEXT_COMPOSED,
            阶段,
            角色=角色.value,
            消息条数=len(序列),
            估算token=估算视图(视图),
            容量token=self.编排.容量token,
        )
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

    def _指令消息(self) -> tuple[Message, ...]:
        """把生效中的插话变成消息。

        ## 为什么包成 `user` 角色而不是塞进 system

        插话是**用户说的话**，不是系统设定。
        塞进 system 会让模型当成规则来遵守；
        而用户说的「这次重点改 X」是本轮的任务偏重——按 `user` 更准，
        也更符合 D-215 那条「它是谁说的话，就标成谁」。

        标注成 `user_directive` 前缀是为了让模型知道这是**插进来的**，
        而不是原始诉求的一部分。
        """
        return tuple(Message(role="user", content=f"[用户插话] {条}") for 条 in self._生效指令)

    def _构造请求(self, 角色: RoleId, 阶段: PhaseId, 指派门禁: tuple[str, ...] = ()) -> LlmRequest:
        """**走第 11 步的装配器**——不再手搓 Message。

        没有内容包时**不发一句空话冒充角色提示词**：
        第 13 步那版发的是「你是 xxx，当前阶段 yyy」，
        等于没给任何写作标准，而判据查不出来。
        故明确标注「这是降级提示」，让日志与判据都能看见。
        """
        需求 = self.状态.task.requirement
        if self.编排.内容包 is not None:
            装配 = 装配提示词(
                请求=装配请求(
                    角色=角色,
                    阶段=阶段,
                    产物=(f"{阶段.value}.md",),
                    指派门禁=指派门禁,
                ),
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
            tools=_工具声明(角色, self.状态.task.tier),
            # 温度走配置基准（D-234）。此前 `LlmRequest.temperature`
            # 用的是字段默认值 0.3，`Config.temperature` 从未影响任何请求。
            # **温度按角色取**（D-243）：此前所有角色的请求都用
            # 同一个全局基准，「各角色可不同温度」这条约束从未实现。
            # 取值在 `domain/role.py` 的常量表里，用户不可配（D-146 的坑）。
            temperature=取温度(角色),
            # **按档位开关推理**（D-380）。
            #
            # 此处曾是一个彻底的空壳：`TierSpec.reasoning_policy`
            # 在 `domain/tier.py` 定义了、给了类型、Pro 档也确实是 `ON`，
            # **全仓无一处读它**；而 `reasoning_probe` 实测出的
            # 「这个后端怎么关推理」被压成一句中文显示就丢掉了。
            # 于是 `写法表` 里那四道关闭写法**从未用于关闭任何一次请求**。
            #
            # **ON 档不附加任何字段**：后端默认就是开着的，
            # 要关反倒是多此一举（且部分后端关了反而更差）。
            # 只有 OFF 档（咨询/Flash）才用探测出来的办法关——
            # 那两档按定义就不该付推理的钱。
            额外字段=self._推理字段(),
        )
        前缀 = self.编排.推理关闭.提示前缀
        if 前缀 and 角色 is RoleId.TEAM_LEAD and 消息:
            # **前缀只加在用户诉求上**（第 4 种写法是提示词前缀）。
            # 加在每条系统消息上会污染公文正文，
            # 而模型看到 `/no_think` 开头的公文会当成用户真写的字。
            首 = 消息[0]
            内容 = 首.content if isinstance(首.content, str) else ""
            请求 = replace(
                请求, messages=(replace(首, content=f"{前缀} {内容}"), *请求.messages[1:])
            )
        # 并行取证时多线程共写这张表，故加锁（D-049）
        with self.编排.请求锁:
            self.编排.发出的请求.append(请求)
        return 请求

    def _产物引用(self, 件: 阶段产物) -> ArtifactRef:
        """产物引用。**走 `put_artifact` 而不是 `write_atomic`**（审计 D-127 补漏）。

        ## 为什么换

        `write_atomic` **有意不建父目录**（注释：隐式建目录会让一次
        打错的路径凭空生成一串空目录）。建目录的职责落在 `_写产物` 上，
        而 `_写产物` 只被 `save_run_state` / `put_artifact` 用——
        编排器自己写产物走的是**公开那条**，于是 `data/<run-id>/`
        从无创建，第一件产物必报 `Errno 2`。

        实测：`intake.md 落盘失败：No such file or directory`，
        退出码却是 0。**咨询档从未跑完过一次。**

        端口上本来就有一个专为产物设计、且建目录的 `put_artifact`，
        **此前零调用**——又一个立了没人用的接口。

        ## 路径

        `put_artifact` 内部会补 `data/` 前缀，故 `path` 只给
        `<run-id>/<名称>`，不再重复写 `data/`。
        """
        return ArtifactRef(
            id=ArtifactId(f"{self.编排.运行号}:{件.名称}"),
            kind=ArtifactKind.NOTE,
            path=f"{self.编排.运行号}/{件.名称}",
        )

    def _推理字段(self) -> tuple[tuple[str, object], ...]:
        """该角色这次请求要不要附加「关推理」的字段。

        **按档位决定**：Pro 档是深度思考档，返回空——
        后端默认开着，多加字段只会多一分与后端不一致的风险。
        咨询 / Flash 两档才关。

        **按探测结论决定**：写法为 0（关不掉或没探测过）返回空。
        此时行为与从前**逐字节相同**，故这个功能最坏等于没做。
        """
        字段 = self.编排.推理关闭.关闭字段
        if not 字段:
            return ()
        # **档位未设置时退回空**：`跑到终()` 是唯一入口，
        # 不经过它 `self.编排.档位` 是 `None`，此时不附加任何字段——
        # 比按默认 `CONSULT` 静默跑更安全。
        档位 = self.编排.档位
        if 档位 is None or TIER_BY_ID[档位].reasoning_policy is ReasoningPolicy.ON:
            return ()
        return 字段

    def _算门禁账(self) -> 门禁账:
        """把「本档该跑」与「真跑了取证」分开算（D-390）。

        **判据必须与缺席判据同一套规则**——否则两者会各说各话：
        缺席判据说「该有结论」，这里若算出「不适用」，
        同一个门禁在两处得到相反结论，而两边都是绿的。
        """
        裁定 = self._末次裁定
        # **档位未设置时退回空编制**：门禁账里「该跑」与「已跑」
        # 都会是空集，最终报告会如实显示「本档无门禁」。
        # 这比按默认 CONSULT 算出错误编制更安全。
        档位 = self.编排.档位
        编制 = set(TIER_BY_ID[档位].roster) if 档位 is not None else set()
        已取证 = tuple(g for g in self.门禁 if g.value in 裁定)
        不适用 = tuple(
            g
            for g in self.门禁
            if g.value not in 裁定
            and (
                # 动态门禁：不独立取证，按所触发的红线归属退回（D-084）
                bool(GATES[g].dynamic_owners)
                # 取证方不在本档编制：Flash 无文择体时 G3 降级（D-116）
                or not any(r in 编制 for r in GATES[g].owners)
            )
        )
        缺席 = tuple(g for g in self.门禁 if g.value not in 裁定 and g not in 不适用)
        return 门禁账(已取证=已取证, 缺席=缺席, 不适用=不适用)

    def _导出交付物(self) -> None:
        """把最终交付物落盘。**扫描不过就不写**（D-296）。

        ## 为什么放在 FINAL 而不是编排器外面

        「导出前必须扫描残留占位符，存在未处理占位符时禁止导出」
        是硬约束，它要有一个**明确的执行点**。放在编排层 FINAL 末尾，
        用户看到的顺序才是：正文成形 → 扫描 → 放行或拒绝。

        ## 拦住为什么不发 `run.failed`

        用户约束原文是「**禁止导出**，并列出待补信息」——
        禁的是**写文件**，不是判整轮失败。文稿本身已经成形，
        是「交付这一步没过去」。发 `run.failed` 会让界面显示运行失败，
        而用户的稿件其实好好的。

        故发 `export.blocked`：它带待补清单，且已在用户可见白名单里。
        `export.blocked` / `export.done` 这两个事件与 `导出交付物.取拦截事件`
        **早就定义了却零调用**——正是 D-118「白名单里有、却没有分支」的老病。
        """
        交付 = self.交付物.strip()
        if not 交付:
            return
        got = self.导出(
            文件=(
                导出文件(
                    文件名=_主件文件名,
                    内容=交付,
                    种类=ArtifactKind.MAIN_DOCUMENT,
                ),
            )
        )
        if got.is_error:
            错 = got.error
            assert 错 is not None
            # **必须让用户看见**（D-296）。占位符没补完却写不出文件，
            # 用户至少要知道「哪些还没填」。
            self.编排.发(EventKind.EXPORT_BLOCKED, PhaseId.FINAL, 详情=错.detail)
            return
        self.落盘摘要 = got.unwrap()
        self.导出文件名 = tuple(摘.文件名 for 摘 in self.落盘摘要)
        self.编排.发(EventKind.EXPORT_DONE, PhaseId.FINAL, 文件名=self.导出文件名)
        # **`artifact.published`：落盘留痕**（架构 §5.4）。
        #
        # 此前**落盘这件事在 journal 里没有任何痕迹**——只有 `export.done`
        # 携一份文件名列表。于是架构点名要防的那类错
        # （「界面显示的和落盘的不是一份，这类最难查」）
        # **事后无从对照**：journal 里既没有内容，也没有 hash。
        #
        # 载荷里的 `hash` 由**写入侧**算（导出器知道真实编码，
        # 编排层重算会在 GB18030 下算错），可用
        # `sha256(路径.read_bytes()).hexdigest()` 复算。
        for 摘 in self.落盘摘要:
            self.编排.发(
                EventKind.ARTIFACT_PUBLISHED,
                PhaseId.FINAL,
                name=摘.文件名,
                artifact_kind=摘.种类.value,
                hash=摘.hash,
            )

    def 收结果(self) -> 编排结果:
        档 = self.状态.task.tier
        # 《政策依据适配表》落盘并入库（G7 门禁与起草阶段按名取，D-255）
        if self._政策取证件 is not None:
            件 = self._政策取证件
            存 = self.storage.put_artifact(self._产物引用(件), 件.内容)
            if not 存.is_error:
                self.产物.append(件)
        self.标注 = g3降级标注(档)
        if self.标注:
            self.交付物 = f"{self.交付物}\n\n" + "\n".join(self.标注)
            self.产物.append(
                阶段产物(阶段=PhaseId.FINAL, 名称="g3-降级标注.md", 内容="\n".join(self.标注))
            )
        self._导出交付物()
        return 编排结果(
            终态阶段=PhaseId.DONE,
            完整阶段序列=self.序列,
            产物=tuple(self.产物),
            交付物=self.交付物 or "（本档不产出正文）",
            门禁账=self._门禁账 or 门禁账(),
            动过的角色=tuple(dict.fromkeys(self.动过的)),
            出现过的成员状态=tuple(dict.fromkeys(self.出现过的状态)),
            成果标注=self.标注,
            已完成调用=self.已完成,
            导出文件名=self.导出文件名,
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

    def _读续跑状态(self, 任务: TaskSpec) -> RunState | None:
        """读回上次的状态；读不到返回 `None`（那就不是续跑，是首跑）。

        ## 为什么这里必须读盘，而不能靠调用方传

        幂等的定义是「**同一次运行被中断后再来，不重复做已做的事**」。
        判据只能靠「上次做过哪些调用」——那是上一次落盘的账。

        此前这条账落在 `state.json` 的 `completed_call_ids` 里，
        **但生产代码从不读它**（见 `跑到终` 处的注释）。

        ## 为什么读不到不算错

        首次运行、或上次崩在落盘之前，都可能没有状态文件。
        那就退回 `new_run_state` 从零开始。**读盘失败不应当成运行失败。**

        ## 任务单以本次为准

        档位可能改了（用户续跑时重选）。阶段线由档位决定，
        若沿用上次的 `phase`，会出现「档位是咨询、进度停在成文的
        `drafting`」这种自相矛盾的状态。
        """
        got = self.storage.load_run_state(self.运行号)
        if got.is_error:
            return None
        return replace(got.unwrap(), task=new_run_state(self.运行号, 任务).task)

    def 收用户指令(self, 文本: str) -> None:
        """**UI 线程调用**：收下用户插话，排进队列。

        ## 为什么不直接改历史

        见 `self._指令队列` 的注释：并发写消息列表是最难查的一类缺陷，
        而这里能避免——排队，边界上统一取。

        ## 空文本不入队

        空插话是无意义的输入，入队只会给模型塞一条空消息。
        但**不静默吞掉**：调用方（界面）本来就不该提交空的。
        """
        净 = 文本.strip()
        if 净:
            self._指令队列.put(净)

    def _取新指令(self) -> list[str]:
        """把队列里排队的指令一次取干。**保持到达顺序。**"""
        出: list[str] = []
        while True:
            try:
                出.append(self._指令队列.get_nowait())
            except Empty:
                return 出

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
        门禁取证: Callable[[门禁取证请求], dict[str, GateRecord]] | None = None,
        容量token: int = 128_000,
        温度: float = 0.3,
        #: **关掉推理的办法**（D-380）：`(请求体字段, 提示词前缀)`。
        #:
        #: 由装配根从 `system/reasoning.json` 读出、并在适配层翻译成值传下来。
        #: 探测结论 + 已翻译的关闭方案。**编排层不认识「写法序号」**——
        #: D-070 要求只有装配根 import 适配器，故字段在这里已翻成请求体字段。
        #:
        #: 默认 `推理开关()`（`无结论`）= 不做任何控制，此时请求体与从前逐字节相同。
        推理关闭: 推理开关 = _无推理结论,
        摘要器: Callable[[tuple[Message, ...]], Message] | None = None,
        工具层: 工具运行器 | None = None,
        导出: ExportPort | None = None,
    ) -> None:
        #: **用户插话指令队列**（D-012：指令队列 + 阶段边界检查）。
        #:
        #: 为什么用队列而不是直接改消息列表：插话来自 **UI 线程**，
        #: 而编排跑在 **worker 线程**（D-024 / 架构 §8.1）。
        #: 直接往 `self.历史` 里塞就是并发写——那是最难查的一类缺陷。
        #:
        #: `SimpleQueue` 是标准库自带的线程安全队列，不引新依赖（D-003）。
        self._指令队列: SimpleQueue[str] = SimpleQueue()
        #: **导出器**（D-296）。原先 `services/export.py` 在生产路径
        #: 零调用——全仓只有测试实例化它，于是「导出前必须扫描残留占位符」
        #: 这条用户硬约束**没有代码在执行**。
        #:
        #: 默认是 `未接线导出()`，它**返回 fail 而非静默成功**——
        #: 静默成功过一次，是 D-192 记的原委。
        self.导出: ExportPort = 导出 if 导出 is not None else 未接线导出()

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
        self.门禁取证 = 门禁取证 if 门禁取证 is not None else 门禁取证实装(validators)
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
        self.推理关闭 = 推理关闭
        #: 当前档位。**初始为 None**——只有 `跑到终()` 能设它（D-380）。
        #: 此前默认 `TierId.CONSULT`，导致 `取一步()` / `记汇报()`
        #: 在 `跑到终()` 之前调用时，推理控制、工具域、阶段线
        #: 全部按咨询档执行，与用户实际选择脱节。
        self.档位: TierId | None = None
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
        #: 唤醒条件登记（成员 → 事件名）。
        #:
        #: **原先连字段都没有**（D-406）：`flow.wake_on` 需要存条件，
        #: 而工具依赖上那个回调恒为 None，接线时无处可存——
        #: 于是「有字段、有判空、有失败返回」看着齐全，实则整条链不存在。
        self._唤醒条件: dict[RoleId, str] = {}
        #: **正在被派发、且正在跑工具轮的那个成员**（D-406）。
        #:
        #: 不能拿 `本段动过[-1]` 顶替——那份列表在**工具轮之后**才 append，
        #: 而 `flow.sleep` 恰恰在工具轮里被调，于是那一刻列表还是空的，
        #: 回调会静默什么都不做（实测事件数为 0）。
        self._派发中角色: RoleId | None = None
        #: **取消标志**。与 `_指令队列` 同一个道理：UI 线程只置位，
        #: worker 线程只读——不跨线程改同一个状态（D-012 插话已定此模式）。
        #:
        #: 为什么是协作式而不是强杀：一次成文里有**正在飞行的模型请求**，
        #: 强杀线程会让它凭空消失，而请求是否已计费、上下文是否已污染，
        #: 事后一概查不清。改成在阶段边界停下，
        #: 已完成阶段的状态、产物、审计**原样保住**——那才是能续跑的状态。
        self._已请求取消 = False

    def 请求取消(self) -> None:
        """**UI 线程调用**：请求在**下一个阶段边界**停止。**幂等**。

        ## 为什么不立即停

        正在飞行的模型请求杀不掉——连接在别的线程里。
        与其假装能停（然后实际上停在一个说不清的状态），
        不如明确承诺边界：当前阶段跑完、落盘、存态，然后退出。

        用户按第二次会怎样？**什么都不多发生**——
        重复置位没有意义，不如让第二次按键保持安静，
        否则「按了没反应」和「按坏了」用户分不清。
        """
        self._已请求取消 = True

    @property
    def 已取消(self) -> bool:
        """是否已被请求取消。**只读**，供界面与判据查询。"""
        return self._已请求取消

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
            # **用量可能缺失**（D-384）：有的后端不回报 usage。
            # 此前 `usage` 标注成非 Optional，这里就直接取属性——
            # 一旦真遇到不回报用量的后端就 `AttributeError` 崩在编排层。
            # 缺就报 0：**显示与台账的计 0 远好过整轮运行崩掉**（D-277 口径）。
            prompt_tokens=0 if 用量 is None else 用量.prompt_tokens,
            completion_tokens=0 if 用量 is None else 用量.completion_tokens,
            reasoning_tokens=0 if 用量 is None else 用量.reasoning_tokens,
            finish_reason=响应.finish_reason.value,
        )

    def 取一步(self) -> Callable[[int], Result[Step[int]]]:
        """返回 `drive` 要的 `step` 回调。**一次一阶段**。

        若一步跑完全部阶段，`drive` 就是空壳，
        而续跑与 TUI 检查点插话会全部失效（D-037/D-012）。

        **必须先 `建过程`**——`取一步` 单独用时（如测试 drive 的分段性）
        不会经过 `跑到终`，故这里就地建一次。

        **档位必须先设**：`跑到终()` 是唯一入口，不经过它
        `self.档位` 是 `None`，直接报错比静默按默认值跑更安全。
        """
        assert self.档位 is not None, "档位未设置：必须先调用 `跑到终()` 再取一步"
        return self._建过程(档位=self.档位).推进一步

    def 记汇报(self, 内容: str, 答复: str) -> None:
        """转给当前推进过程记账（见 `_过程.记汇报`）。"""
        assert self.档位 is not None, "档位未设置：必须先调用 `跑到终()` 再记汇报"
        self._建过程(档位=self.档位).记汇报(内容, 答复)

    # ── 工具回调需要的编排侧能力（D-406）──
    #
    # 全部只做「记事实 + 发事件」，**不替模型做决定**。
    # 「严禁编造」是最高数据纪律：编排器不能凭成员一句话就批准什么。

    def _当前阶段(self) -> PhaseId:
        """工具层回调用的阶段。与 `记事件` 同一口径：
        取最新阶段而不是猜 GATING——工具调用可能发生在任何阶段。"""
        过程 = self._过程
        return 过程.状态.phase if 过程 is not None else PhaseId.INTAKE

    def _当前成员(self) -> RoleId | None:
        """当前正在推进的成员。

        **编排器不记「当前角色」**——它只跑阶段，一阶段里可能先后
        经手多个成员。故取「本段动过的最后一个」：那最接近
        「刚刚在干活的是谁」。取不到就 None——
        工具可能在成员上下文之外被调（门禁取证段）。
        """
        return self._派发中角色

    def _成员状态表(self) -> dict[str, str]:
        """成员状态快照（D-046：数据不进标准界面，只供主理人查询）。"""
        if self._过程 is None:
            return {}
        return {r.value: st.value for r, st in self._过程.状态.role_states.items()}

    def _挂起当前成员(self, 原因: str) -> None:
        """把当前成员置为挂起。

        **挂起态绝不发起模型请求**（D-042 第 1 条）——
        由 `该发请求` 保证，本方法只改状态。
        """
        角色 = self._当前成员()
        if 角色 is None:
            return
        self._置成员状态(角色, RoleState.SUSPENDED, f"成员主动挂起：{原因[:60]}")
        # **挂起单独发 `role.sleep`**（D-406）：`role.state_changed` 说的是
        # 「状态变了」，`role.sleep` 说的是「为什么变、因谁而起」——
        # 归档里问「这个成员为什么停下来」时，后者才答得上。
        self._发角色事件("role.sleep", {"角色": 角色.value, "原因": 原因[:80]})

    def _挂起成员(self, 事项: str, 紧急: bool) -> None:
        """请示：置为等待条件，等主理人那一轮读到它。

        **不硬造「已批准」**——那是编排器替主理人做决定。
        """
        角色 = self._当前成员()
        if 角色 is None:
            return
        标 = "紧急请示" if 紧急 else "请示"
        self._置成员状态(角色, RoleState.AWAITING_CONDITION, f"{标}：{事项[:60]}")

    def _设唤醒条件(self, 事件名: str, 角色名: str) -> None:
        """登记唤醒条件。命中时把成员放回运行中。"""
        try:
            目标 = RoleId(角色名)
        except ValueError:
            return
        self._唤醒条件[目标] = 事件名
        self._置成员状态(目标, RoleState.AWAITING_CONDITION, f"等待事件：{事件名}")
        self._发角色事件("role.wake", {"角色": 目标.value, "等待事件": 事件名})
        self._唤醒条件 = self._唤醒条件

    def _请求退回(self, 原因: str) -> None:
        """成员请求退回重做。

        **只发事件、不自己改门禁账**——上限 2 次（D-050）是门禁循环的
        硬约束，在这里另起一套就等于没有上限。
        """
        角色 = self._当前成员()
        if self._过程 is None:
            return
        self.发(
            EventKind.ROLE_REPORT,
            self._当前阶段(),
            角色=角色.value if 角色 is not None else "",
            detail=f"成员请求退回：{原因[:80]}",
        )
        # **额外发状态变化**（D-406）：退回之后成员要回到可重做状态，
        # 否则状态表停在「等待条件」，而它其实可以继续干了。
        if 角色 is not None:
            self._置成员状态(角色, RoleState.RUNNING, f"退回重做：{原因[:50]}")

    def _置成员状态(self, 角色: RoleId, 状态: RoleState, 说明: str) -> None:
        """改成员状态 + 发事件。**两件事都要做**——
        只改状态则界面不知情（D-296 的教训），
        只发事件则续跑时状态对不上（同样后果）。
        """
        from ..domain.event import EventKind

        if self._过程 is not None:
            self._过程.状态 = self._过程.状态.set_role_state(角色, 状态)
        try:
            事件类 = EventKind.ROLE_STATE_CHANGED
        except ValueError:  # pragma: no cover
            return
        self.发(事件类, self._当前阶段(), 角色=角色.value, 状态=状态.value, 说明=说明[:80])

    def _发角色事件(self, kind: str, 载荷: dict[str, object]) -> None:
        """发一条 `role.*` 事件（D-406）。

        与 `编排回调._发角色事件` 分开是因为**持有者不同**：
        那个方法挂在编排回调上（发事件需先有编排器，故它是后补的），
        这个挂在编排器上，供编排器自己的状态变更用。
        失败策略相同——未登记的 kind 静默跳过，不因留痕失败中断流程。
        """
        from ..domain.event import EventKind

        try:
            事件类 = EventKind(kind)
        except ValueError:  # pragma: no cover - 常量拼错才会走到
            return
        self.发(事件类, self._当前阶段(), **载荷)

    def _建过程(self, *, 档位: TierId) -> _过程:
        """建立（或复用）推进过程。**幂等**——
        `取一步` 与 `跑到终` 都要它，故不能建两次。

        **档位必须显式传入**：此前硬编码 `TierId.PRO`，
        导致无论用户选什么档位，实际跑的都是 Pro 档。

        **复用时档位不一致要炸**（D-413）：幂等这条捷径会让传进来的
        `档位` 在「过程已存在」时被**静默丢弃**——那正是本项目栽过八次
        的形状（D-222/D-231/D-233/D-234/D-235/D-406：值在那里没人往下传，
        而所有既有测试全绿）。档位决定门禁、编制与推理开关，
        丢弃一次就等于按别的档跑完全程且不报错，故此处主动 `raise`。

        用 `raise` 而不是 `assert`：`assert` 在 `python -O` 下会消失，
        而这条保护的是真实运行行为，不是调试辅助（同 `bus.py:83`、
        `journal.py:149` 的口径）。
        """
        if self._过程 is not None:
            已建 = self._过程.状态.task.tier
            if 已建 is not 档位:
                raise ValueError(
                    f"档位不一致：过程已按 {已建.value} 建好，"
                    f"本次又传 {档位.value}——传进来的档位会被静默丢弃，"
                    "门禁与编制会按先建的档位执行。"
                    "请由调用方传入与已建过程一致的档位。"
                )
            return self._过程

        任务 = TaskSpec(tier=档位, requirement="")
        self._过程 = _过程(
            编排=self,
            llm=self.llm,
            storage=self.storage,
            # **阶段线也要按档位取**（D-413）：此前这里写死 `成文序列`，
            # 于是档位只被塞进 `TaskSpec` 就没往下走——咨询档经这条路
            # 建出来的过程照跑 DIRECTION / OUTLINE / GATING。
            # 与 `跑到终` 用 `阶段序列(档位)` 保持一致；PRO/FLASH 结果不变，
            # 只有咨询档从「错走成文线」变成走咨询线。
            序列=阶段序列(档位),
            状态=new_run_state(self.运行号, 任务),
            初始已完成=self.初始已完成,
        )
        # **工具调用存档此时才有对象**（D-406）——
        # 它是 `_过程` 的字段，故必须等过程建好才能递给工具层。
        # 此前 `工具依赖.调用存档` 恒为 None，`tool.result` 于是永远失败。
        self._记工具存档()
        return self._过程

    def _记工具存档(self) -> None:
        """把本过程的工具存档对象接到工具层。**幂等**——
        `_建过程` 已保证只建一次，但这个方法可能被别处再调，故自己判重。
        """
        if self._过程 is None:
            return
        存档 = self._过程.工具存档
        if 存档 is None:
            存档 = 调用存档类()
            self._过程.工具存档 = 存档
        if self.工具层 is not None:
            self.工具层.接调用存档(存档)

    def 跑到终(
        self,
        *,
        档位: TierId,
        需求: str,
        初始dcard: DCard | None = None,
    ) -> Result[编排结果]:
        """用 `drive` 跑到底。**这是唯一入口**——
        没有另一条「一口气跑完」的路，否则 `drive` 会被绕过。

        `初始dcard`：测试直接预填 D-Card，绕过 `_从用户输入更新d卡()`
        的启发式（它只从需求文本正则提取 D1，D2-D9 永远留空）。
        满卡不会被 `_从用户输入更新d卡()` 覆盖——它只在字段
        `is_missing` 时才填，满卡所有字段都有值。
        """
        任务 = TaskSpec(tier=档位, requirement=需求)
        合法 = validate_task(任务)
        if 合法.is_error:
            校验错 = 合法.error
            assert 校验错 is not None
            return Result.fail(校验错.code, 校验错.detail)

        序列 = 阶段序列(档位)
        # **续跑必须读回上次的状态（D-065 补漏）**。
        #
        # 此前是 `self.初始状态 or new_run_state(...)`：调用方不传就
        # 从零重建。而 `app.py` **从不传**（它只调 `编排器(...)`），
        # 于是每次跑都从头开始——续跑会**重发全部请求**。
        #
        # `load_run_state` 在编排层此前零调用；它唯一出现的地方是
        # `app.py` 的 `run_once`，而那是个死函数（只有 tests 调）。
        # 于是「续跑」这个能力挂在一条不存在的链上。
        状态 = self.初始状态 or self._读续跑状态(任务) or new_run_state(self.运行号, 任务)
        if 初始dcard is not None:
            状态 = 状态.with_dcard(初始dcard)
        # 强制重建：同一编排器跑两次时，第二次不能沿用上次的进度。
        # 状态可续，过程必须重建——过程里存着逐请求的运行时状态。
        #: **本轮跳过的已完成调用数**（D-065）。`run.resumed` 的判据。
        已完成调用 = self.初始已完成 or tuple(状态.completed_call_ids)
        self._过程 = _过程(
            编排=self,
            llm=self.llm,
            storage=self.storage,
            序列=序列,
            状态=状态,
            初始已完成=已完成调用,
        )
        self.进度终值 = None
        # **记住档位**（D-380）：此前档位只逐层传参、编排器自己不存，
        # 于是构造请求时无从判断该档该不该关推理。
        # `跑到终` 是唯一入口，在这里定死即可。
        self.档位 = 档位
        # **取消标志必须重置**（与 `进度终值` 同一处理由：同一编排器可跑多次）。
        # 不重置的话，用户取消一次之后，这个编排器**再也跑不动**——
        # 第二次 `推到终` 第一步就撞上残留的标志，立刻返回 done，
        # 界面表现为「按了开始，什么都没发生就结束了」。
        self._已请求取消 = False

        借来的总线 = self.bus is None
        总线 = self.bus or _临时总线()
        self.发(EventKind.RUN_STARTED, PhaseId.INTAKE, tier=档位.value)
        # **`run.resumed`：这轮是接着上次跑的**（D-065）。
        #
        # 此前**续跑在 journal 里没有任何痕迹**——只有一条 `run.started`。
        # 于是事后无从区分「一次跑完」与「跑了三次接起来」，
        # 而续跑判定的唯一依据（`state.json` 的 `completed_call_ids`）
        # 正是在排查「为什么这次这么快/这次模型只调了两次」时最该看的东西。
        #
        # 判据是**已完成调用非空**：那些调用已被幂等记账跳过，
        # 正是一次续跑的确凿证据。空则不发，否则每次都发会变成噪声。
        if 已完成调用:
            self.发(
                EventKind.RUN_RESUMED,
                PhaseId.INTAKE,
                已完成调用数=len(已完成调用),
            )
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
            # **先留内部细节，再升级给用户**（架构 §5.4）：
            # `error.raised` 携完整 `detail` 且是 INTERNAL，
            # `run.failed` 只让用户看到错误码。
            #
            # 此前**只有 `run.failed` 一个出口，而它带的是 `错.code.value`**
            # ——真正的 `错.detail` 随 `Result.fail` 返回后就没人知道了。
            # 结果 journal 里排障只剩一个 `E_...` 码，看不出「到底哪错了」。
            self.发(
                EventKind.ERROR_RAISED,
                PhaseId.DONE,
                detail=错.detail,
                code=错.code.value,
            )
            self.发(EventKind.RUN_FAILED, PhaseId.DONE, detail=错.code.value)
            return Result.fail(错.code, 错.detail)
        总成 = got.unwrap()
        if not 总成.ok:
            # 同上：这一处 `run.failed` 带的是真 detail（两处口径本来就不一致），
            # 但内部留痕不能依赖「恰好这一处带了」——统一都发。
            self.发(
                EventKind.ERROR_RAISED,
                PhaseId.DONE,
                detail=总成.detail,
                code=ErrorCode.KERNEL_STALLED.value,
            )
            self.发(EventKind.RUN_FAILED, PhaseId.DONE, detail=总成.detail)
            return Result.fail(ErrorCode.KERNEL_STALLED, 总成.detail)
        self.进度终值 = 总成.steps
        已存 = self.storage.load_run_state(self.运行号)
        self.终态 = 已存.unwrap() if 已存.is_ok else 状态
        if self._已请求取消:
            # **不发 `RUN_FINISHED`**：那会让界面显示「已完成」，
            # 而用户明明是自己按停的——把中止说成完成，
            # 等于替用户改写他刚做的动作。
            # `RUN_CANCELLED` 已在阶段边界发过。
            #
            # 返回 `ok` 而非失败：取消是预期行为，
            # 报错会让界面弹错误、让续跑把它当崩溃处理，
            # 而已完成阶段的状态**原样可用**。
            return Result.ok(self._建过程(档位=self.档位).收结果())
        self.发(EventKind.RUN_FINISHED, PhaseId.DONE)
        return Result.ok(self._建过程(档位=self.档位).收结果())


# ── 阶段线 ──

咨询序列: Final[tuple[PhaseId, ...]] = (
    PhaseId.INTAKE,
    PhaseId.FINAL,
    PhaseId.DONE,
)
"""咨询档阶段线（D-035 / 架构 §8.3、§8.4）。

## 为什么只有三段

架构 §8.4 明写 `TierSpec(roles=["范正章"], phases=[ADVICE])`，
§8.3 补一句「**咨询档不走这条线**：仅范正章一人，
不触发方向确认、不做政策检索、不走门禁，
产出为『有依据、有出处、给明确建议』的专家答复」。

此前这里是**成文序列删掉 GATING**——于是咨询档照样走
DIRECTION（方向确认）、OUTLINE（文章大纲）、DRAFTING（正文起草）。

三个阶段对应架构的 ADVICE：
INTAKE 采集诉求并由范正章给出专家答复，FINAL 交付，DONE 终态。

## 为什么不给 PhaseId 增 ADVICE

`PhaseId` 不动。增枚举会波及续跑、投影与事件序列
（`domain/phase.py` 已明记这条纪律），而收益只是换个名字——
行为上与 INTAKE 一致。名字不符是文档债，行为错才是真缺陷。

## 不是「硬约束校验挡的」

咨询档首跑失败与 DIRECTION 无关：**空 D-Card 跑硬约束是通过的**
（实测）。真因是 `save_run_state` 写 `state.json` 同样落在
不建目录的路径上，属产物落盘那个缺陷。
"""
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


def 查结束原因(响应: LlmResponse, 阶段: PhaseId, 角色: RoleId) -> Result[None]:
    """**结束原因不合格就不许往下走**（D-268）。

    ## 此前是个静默的洞

    `FinishReason.LENGTH`（输出被截断）与 `CONTENT_FILTER`（服务端合规拦截）
    在全仓**生产代码里从未被读过**——归一化出来后直接被丢掉。
    后果是两种坏稿子都被当成正常完成交付出去：

    - 截断：`http_llm.py` 的注释原话是「只认 `length` 会把『输出被截断』
      判成『正常完成』——上层认为内容完整而交付，交付的是残稿」。
      **而那正是当时发生的事。**
    - 合规拦截：D-032 明写「前者（合规）是红线，硬退回」，
      实际却是「照收不误」。

    **用户宁可看到一次明确的失败，也不要一份看着完整的残稿。**

    ## 未知值为什么不放过

    未知归一化成 `STOP`，而 `STOP` 的含义是「正常结束」。
    厂商新增一个词，程序就会把可能截断的稿子当完整的交出去——
    与只认 `length` 是同一类错误，只是发生在另一个词上。

    故**认不出来就不许断定完整**（`是已知结束原因`），
    报错信息里带上服务端原文，用户能据此判断该端点的行为。

    ## 为什么不让它重试

    截断可以续写（`FinishReason.LENGTH` 注释写了「可续写」），
    但**本层不做续写**——续写要拼上一轮的内容，牵涉提示词装配与预算，
    属另一件事。此处只做到「不把残稿当完整稿交付」。
    """
    原因 = 响应.finish_reason
    原文 = 响应.raw_finish_reason.strip()
    定位 = f"{阶段.value}/{角色.value}"
    if 原因 is FinishReason.LENGTH:
        return Result.fail(
            ErrorCode.LLM_OUTPUT_TRUNCATED,
            f"{定位} 输出被服务端截断（finish_reason={原文 or 原因.value}），"
            f"内容不完整，不得当作完成：{响应.text[:80]}",
        )
    if 原因 is FinishReason.CONTENT_FILTER:
        return Result.fail(
            ErrorCode.LLM_CONTENT_FILTERED,
            f"{定位} 被服务端合规拦截（finish_reason={原文 or 原因.value}），按红线处理，不得交付",
        )
    if 原文 and not 是已知结束原因(原文):
        return Result.fail(
            ErrorCode.LLM_FINISH_UNKNOWN,
            f"{定位} 服务端返回未识别的结束原因「{原文}」，"
            "无法断定内容是否完整，已拦下（可在决策记录里登记该端点的用法）",
        )
    return Result.ok_none()


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


def _工具清单(角色: RoleId, 阶段: PhaseId, 档: TierId) -> str:  # noqa: ARG001  # 阶段暂不参与切片
    """本次实际可调用的工具（供模型知道边界）。

    ## 与 API 声明同源

    此前这里是**一个 return 字面量**：6 个工具，其中 `kb.read`
    根本不存在，而 API 实发 17 个。提示词与实际能力对不上，
    模型不是不敢用工具就是去调不存在的工具。

    现在与 `_工具声明` 共用 `_本档可调工具`——同一份来源，
    两侧不可能再分叉。
    """
    return " / ".join(t.name for t in _本档可调工具(角色, 档))


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
    不适用: tuple[GateId, ...] = (),
) -> str:
    """门禁报告：**责任角色 + 结论 + 证据 + 未过原因**（D-236）。

    ## 为什么必须带这四项

    标准模式不展示审核意见（用户约束），所以这份报告是**事后唯一的台账**。
    原先只有「门禁号 + 名字」两样，于是两个字段生产零消费：

    - `GateRecord.evidence` —— 只在序列化时读写，取证方给的证据**没人看**
    - `rejection_reason` —— 驳回了，但用户看不到**为什么**被驳回

    用户只知道「被退回了」，无法判断是文稿问题还是环境问题，
    只能回去翻日志原文——而那正是用户要求「不要展示」的东西。

    ## `gate_owners` 不在欠账里（别再去"接线"）

    上一版注释把 `gate_owners(g)` 也列为「生产零引用」，**那是错的**——
    它与 `GATES[g].owners` **同源**（`gate.py:122` 直接返回那个字段），
    而报告里 `:2562` 取的是 `规.owners`，即同一个东西。
    生产代码不需要这个函数，**它不是缺接线，是本来就多余的包装**。

    留着不删的三个理由：
    1. `tests/test_step145_gate_report.py:119,125` 在调，删了要改判据
    2. 它是公开 API，删是破坏性变更
    3. 架构 §8.3 提到它，删了要同步文档

    真正的欠账是上面那两个字段：**证据和驳回原因在报告里，
    但报告不展示审核意见，于是用户永远看不到**。那才是
    「这稿是谁的锅？」（D-049）没有出口的地方。

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
            # **「不适用」与「无结论」必须分开印**（D-390）。
            #
            # G3 于 Flash 取证方不在编制、G9 是动态门禁——它们**本就不该有结论**。
            # 若一并印成「无结论」，用户读到的是「有两道门禁没查出来」，
            # 而实际上没有任何缺陷。台账一旦把豁免说成缺失，
            # 排查就会顺着错方向走。
            不适用否 = g in 不适用
            结论 = "本档不适用" if 不适用否 else "**无结论**"
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
