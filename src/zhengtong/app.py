"""装配根（第 4 步）。

**全项目唯一的装配点**（D-070）。TUI 与 doctor 两者
共用 `build_app()`（D-063），因此装配错误只需在这一处暴露一次。

**这里只做装配，不做编排。** 具体流程的推进逻辑属 L5，
本模块的职责是把零件凑齐并保证接得上：

- 造 `PortRegistry`（8 个端口全注入）
- 造 `EventBus`（带脱敏与日志落盘）
- 造两个投影（D-023：一个循环、两种显示）
- 提供 `run_once()`：跑一次完整流程，**装配与驱动都验证到**

**第 4 步的判据是"能装配并跑起来"，但"跑起来"的诚实标准要说清楚**：
本步用全假实现，所以它验证的是**管线接通**，不是公文质量。
桩数据带 `【桩】` 标记，就是为了让这一点无法被误认。
"""

# 本文件放宽 Unknown 类规则，理由同 domain/errors.py 的 D-085：
# `Result.ok(...)` 返回 `Result[Any]`（三方案实测后确认唯一可行解），
# 故各端口返回值的成员访问类型为 Unknown。只影响静态提示，不影响运行行为。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .adapters.config import Config
from .adapters.config import load as load_config
from .adapters.content_pack import 本地内容包
from .adapters.fakes import (
    STUB_MARK,
    FakeClock,
    FakeContentPack,
    FakeLlm,
    FakeRetriever,
    FakeSecrets,
    FakeStorage,
    FakeUserFiles,
    FakeValidators,
)
from .adapters.fs_storage import FsStorage
from .adapters.http_llm import HttpLlm
from .adapters.httpx_transport import HttpxTransport
from .adapters.local_secrets import LocalSecrets
from .adapters.paths import Workspace, ensure_dirs
from .adapters.system_clock import SystemClock
from .adapters.userfiles.scanner import LocalUserFiles
from .adapters.validators import 硬约束校验器
from .domain.artifact import ArtifactKind, ArtifactRef
from .domain.dcard import DCard
from .domain.errors import ErrorCode, Result
from .domain.event import Event, EventKind, Level
from .domain.ids import ArtifactId, RunId
from .domain.phase import PhaseId
from .domain.role import RoleId
from .kernel.bus import EventBus
from .kernel.driver import DriveOutcome, Step, drive
from .kernel.journal import JsonlJournal
from .kernel.projection import DebugProjection, StandardProjection
from .kernel.redactor import Redactor
from .orchestration.orchestrate import 编排器
from .ports.clock import ClockPort
from .ports.content_pack import ContentPackPort
from .ports.export import ExportPort, 未接线导出
from .ports.llm import LlmPort, LlmRequest, Message
from .ports.registry import PortRegistry
from .ports.retriever import RetrieverPort
from .ports.secrets import SecretsPort
from .ports.storage import StoragePort
from .ports.user_files import UserFilesPort
from .ports.validators import ValidatorPort
from .presentation.gate import 问答闸门
from .services.export import 导出交付物
from .services.retrieval.search import 中文检索
from .services.tools import 工具运行器, 建工具运行器

#: 三个阶段成果的名字（D-030）。标准模式只显示这三样（D-046）。
#: 内容包目录名。**与程序放在一起、视为只读**（架构 §9）——
#: `system/` 只放配置与日志，删掉不损失知识内容。
内容包目录名 = "content"


def 内容包根() -> Path:
    """定位内容包根。**从包的安装位置往上找**——
    内容包不在工作区里（工作区只有 data/user/system 三目录），
    它与程序同级，故不能走 `ws.dir(...)`。

    找法：`zhengtong/adapters/app.py` → 上溯到包根 → 取其父目录下的 `content/`。
    找不到就返回该路径（后续 `verify_manifest` 会报「缺 manifest」，
    **不静默退化成假内容包**——那正是本条要防的事）。
    """
    return Path(__file__).resolve().parent.parent.parent / 内容包目录名


ARTIFACT_DIRECTION = "写作方向确认单"
ARTIFACT_OUTLINE = "文章大纲"
ARTIFACT_DRAFT = "正文初稿"


#: 阶段 →（成果名, 成果种类, 负责角色）。**角色对应关系只放这里**，
#: 因为它属内容包的编排事实；代码别处不得另建角色对照（D-080）。
_PRODUCING: dict[PhaseId, tuple[str, str, RoleId]] = {
    PhaseId.DIRECTION: (ARTIFACT_DIRECTION, "direction_sheet", RoleId.TEAM_LEAD),
    PhaseId.OUTLINE: (ARTIFACT_OUTLINE, "outline", RoleId.STRUCTURE),
    PhaseId.DRAFTING: (ARTIFACT_DRAFT, "draft", RoleId.LANGUAGE),
}


@dataclass(slots=True)
class App:
    """装好的应用。**字段只读**：装配完不该再被改动。"""

    ports: PortRegistry
    bus: EventBus
    standard: StandardProjection
    debug: DebugProjection
    run_id: RunId
    #: 工具层。**接进 App 才是可达的**（D-195）：
    #: 第 10 步交付后 `services/tools/` 零引用——建好了、测过了，
    #: 但没有任何一处引用它，真实模型一个工具也调不到。
    tools: 工具运行器
    #: 问答闸门（D-287）。**界面侧要从 App 取这同一个实例**——
    #: 提问走工具层（`闸.问`）、答复走界面（`闸.挂答复`），
    #: 两侧必须握同一个闸，否则提问方永远等不到。
    问答: 问答闸门
    #: **导出器**（D-296）。与 `tools` 同理：装配时造好挂在这儿，
    #: `App.编排器()` 才拿得到——工作区根只有装配那一刻有，
    #: 事后无处可取。
    导出: ExportPort
    #: 运行期配置。**模型名只能从这里取**（D-082）——
    #: 代码不得自动填写，用户填什么就用什么。
    #: 此前这里是 `model="stub-model"` 写死的，
    #: 第 5 步接真配置时换掉。
    config: Config = field(default_factory=Config)
    #: 收集全部事件，供测试与调试
    collected: list[Event] = field(default_factory=list)

    def 编排器(self) -> 编排器:
        """从装配根取出编排器，**端口全是真件**。

        ## 为什么方法在 App 上而不是让调用方自己 new

        真实运行路径必须走装配根：假实现接在真路径上是本项目
        反复吃亏的地方（D-165 已九次，第 13 步第十次）。
        由调用方自己 `编排器(llm=..., storage=...)` 就等于
        「端口从哪来全凭调用方」——那正是接缝脱开的入口。

        预算取自配置的上下文容量（D-189）：
        **不给无上限**，无上限等于闸门不存在。
        """
        from .orchestration.orchestrate import 编排器 as 编排器类

        容量 = max(1, self.config.context_k) * 1024
        编排 = 编排器类(
            llm=self.ports.llm,
            storage=self.ports.storage,
            # **工具层必须一起传**（D-267）。此前 `App.tools` 备好了，
            # 但编排器工厂没往下传——于是模型发出的 `tool_calls`
            # 在编排层没有执行者，**16 个工具全是死的**。
            工具层=self.tools,
            # **导出器必须一起传**（D-296）。此前
            # `services/export.py` 在生产路径零调用——全仓只有测试实例化它，
            # 于是「导出前必须扫描残留占位符」这条用户硬约束
            # **没有代码在执行**，最终 TXT 也从不落盘。
            #
            # 与 D-267（工具层没往下传）是同一个形状：件备好了，
            # 工厂没传，于是那一层全是死的。
            导出=self.导出,
            bus=self.bus,
            content_pack=self.ports.content_pack,
            validators=self.ports.validators,
            模型名=self.config.model,
            预算=容量 // 2,  # 一半给本次装配，另一半留给工具返回与追加
            # **压缩用的容量必须来自用户配置**（第 14.5 步 D-231）。
            #
            # 此前只拿容量算装配预算，**没往下传**——编排器一直用默认 128000。
            # 于是 8K / 128K / 200K 三种配置的压缩触发线**完全相同**（都按
            # 128000×0.85 = 108800 触发）：填小了压缩永不触发（上下文早爆了
            # 它还在睡），填大了提前太多（白丢本该保留的上下文）。
            #
            # 本项目第五次同型缺口：一个参数被读了却没往下传。
            容量token=容量,
            # **温度必须来自用户配置**（D-234）。
            #
            # `Config.temperature` 在全仓**零消费**——
            # 请求吃的是 `LlmRequest` 的字段默认 0.3，
            # 用户改配置里的温度完全无效。
            # 与 D-231 同型：值在那里，没人往下传。
            温度=self.config.temperature,
            运行号=self.run_id,
        )
        # **工具调用与拒绝要进 journal**（D-266，架构 §7.4）。
        # 工具层在 `装配()` 里先建好、编排器后建，故此处**后补**回调——
        # 鸡生蛋问题：编排回调要持有编排器，工具层又要先于编排器存在。
        self.tools.接记事件(编排.工具回调.记事件)
        # **插话转达同样后补**（与上面同一个鸡生蛋）：
        # 工具层在 `装配()` 里先建好、编排器后建，
        # 而 `flow.inject_directive` 的回调要持有编排器的指令队列。
        #
        # 此前 `工具依赖.转达` **恒为 None**（D-012 定了机制却从没接线）：
        # 模型调插话只得到一句「未接线」，而它**不报错**——
        # 流程照走，用户的插话静默消失。与 D-286（问用户）同型。
        self.tools.接转达(编排.收用户指令)
        return 编排

    def close(self) -> None:
        """收尾。**必须排空总线**（D-087）——
        否则未处理的事件随进程退出消失，用户看不到、日志里也没有。

        **并释放端口持有的资源**（第 6 步接线补上）：
        真实 LLM 适配器持有 HTTP 连接池，**不关会耗尽**。
        而 `App` 并不拥有端口——它只是持有引用。
        故这里逐个「若能关则关」，而不是要求所有端口都实现 `close`
        （假实现没有连接可关，强行要求只会逼出一堆空方法）。
        """
        for 端口 in (self.ports.llm, self.ports.clock):
            关 = getattr(端口, "close", None)
            if callable(关):
                _ = 关()  # 有些 close 返回 client，丢弃即可
        self.bus.stop()

    def __enter__(self) -> App:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def 准备目录(根: Path) -> Result[Workspace]:
    """确保三目录存在并交出工作区（D-017）。**幂等，绝不删改已有内容。**

    ## 为什么放在装配根而不是让 CLI 自己 import 适配器

    架构 §3.4 的硬规则：**除 `adapters/` 与 `app.py` 外无人 import 适配器**
    ——它防的是「依赖注入失效」。而 CLI 若是第一个绕过它的层，
    破例就从入口开始，一路往下传染。

    故**门面给在装配根**：CLI 只跟 `app.py` 打交道，
    规则一个字都不用改。

    接入层要的东西本来也只有装配根——
    `build_app_from_workspace` 的 docstring 已经写着
    「真实运行必须走装配根」。
    """
    return ensure_dirs(根)


def 工作区(根: Path) -> Workspace:
    """把用户给的路径包成工作区。

    **只包，不建目录**——「看一眼目录」不该产生副作用（D-017 的分工）。
    要建目录用 `准备目录`。
    """
    return Workspace(root=根)


def build_app(
    run_id: RunId | None = None,
    *,
    log_path: Path | None = None,
    #: 运行期配置。**不给就用默认空配置**——
    #: 第 4 步的测试全是假端口，不需要真实端点，
    #: 强制要配置会把它们全变成「先造一份配置」的仪式。
    #: 真实运行走 `build_app(workspace=...)`。
    config: Config | None = None,
    # 端口参数一律用**协议类型**而非假实现类型。
    # 第 5 步实测：原来签名写死 `FakeSecrets | None`，
    # 结果 `LocalSecrets`（真适配器）**根本传不进去**——
    # 假实现阶段没暴露，一接真凭据就撞墙。
    # 端口存在的意义就是「实现可换」，签名写死实现等于废掉端口。
    secrets: SecretsPort | None = None,
    storage: StoragePort | None = None,
    validators: ValidatorPort | None = None,
    clock_override: ClockPort | None = None,
    user_files: UserFilesPort | None = None,
    content_pack: ContentPackPort | None = None,
    retriever: RetrieverPort | None = None,
    llm: LlmPort | None = None,
    导出: ExportPort | None = None,
    #: 问答闸门（D-287）。**不给就用一个新的**——
    #: 没闸门等于「无人应答」，而强制等待正是这个语义。
    问答: 问答闸门 | None = None,
) -> App:
    """装配一个应用。**每个端口都可注入**，便于测试定向场景。

    `log_path` 给定时才挂 journal：默认不落盘，
    免得"跑一下"就在工作区里留下文件。
    """
    sec = secrets if secrets is not None else FakeSecrets()
    st = storage if storage is not None else FakeStorage()
    val = validators if validators is not None else FakeValidators()

    ports = PortRegistry(
        llm=llm if llm is not None else FakeLlm(),
        storage=st,
        user_files=user_files if user_files is not None else FakeUserFiles(),
        content_pack=content_pack if content_pack is not None else FakeContentPack(),
        retriever=retriever if retriever is not None else FakeRetriever(),
        validators=val,
        secrets=sec,
        clock=clock_override if clock_override is not None else FakeClock(),
    )

    # 脱敏模式**取自 secrets 端口**：这是"密钥存哪、哪里就有它的片段"
    # 的机械落点（D-030）。注意传入的是片段，不是密钥全文。
    redactor = Redactor(extra_patterns=sec.redaction_patterns())
    journal = JsonlJournal(log_path) if log_path is not None else None
    bus = EventBus(redactor, journal)

    # 工具层接线（D-195）：**由已装好的端口组装，不另造一套依赖**。
    # 这样「App 用的端口」与「工具用的端口」必然是同一批——
    # 若工具层自己 new 一份假端口，就会出现「装配层测的是真检索、
    # 工具层用的还是假检索」这种同型盲区。
    生效run = run_id if run_id is not None else RunId("r-0001")
    # **问答闸门（D-287）**：装配时建一个，App 持有，界面侧也从 App 取同一个。
    #
    # 此前 `工具依赖.问用户` 在真实路径上恒为 None，模型一提问就得到
    # 「尚未接线」，而流程带着未填的 D-Card 继续走——
    # 不报错，但产出建立在空字段上（D-007 的反例）。
    闸 = 问答 if 问答 is not None else 问答闸门()

    tools = 建工具运行器(
        storage=st,
        user_files=ports.user_files,
        retriever=ports.retriever,
        content_pack=ports.content_pack,
        validators=val,
        run_id=生效run,
        问用户=闸.问,
    )

    # **不给默认真件，就用会失败的占位**（D-296）。
    # `build_app` 是测试入口、不持有工作区根，造不出真落盘路径；
    # 硬造一个临时目录只会在测试里写出一堆没人看的文件。
    # 占位**返回 fail 不静默成功**——静默成功过一次，是 D-192 记的原委。
    # 真实运行路径（`build_app_from_workspace`）传真件。
    导出件 = 导出 if 导出 is not None else 未接线导出()
    app = App(
        ports=ports,
        bus=bus,
        standard=StandardProjection(),
        debug=DebugProjection(),
        问答=闸,
        run_id=生效run,
        tools=tools,
        导出=导出件,
        config=config if config is not None else Config(),
    )
    # 订阅顺序即处理顺序：标准投影与调试投影互不影响（D-023）
    bus.subscribe(app.standard.accept)
    bus.subscribe(app.debug.accept)
    bus.subscribe(app.collected.append)
    return app


def run_once(app: App) -> Result[DriveOutcome]:
    """跑一次完整流程（假实现）。

    **这是一个刻意简陋的流程**：只为验证"装配 + 驱动"这条管线接通，
    真实的阶段推进、门禁编排、退回修正属 L5。

    但它**不偷工减料地跳过约束**：

    - 三份阶段成果照发 `phase.artifact_ready` 并**载全文**（D-072）
    - 门禁照跑校验器；不通过则**停在 REVISION**，不进 FINAL（D-028/D-038）
    - 没有绕过出口：只有门禁过才 `done=True`
    """
    st = app.ports.storage

    def step(state: int) -> Result[Step[int]]:
        order = (
            PhaseId.INTAKE,
            PhaseId.DIRECTION,
            PhaseId.OUTLINE,
            PhaseId.DRAFTING,
            PhaseId.GATING,
            PhaseId.FINAL,
        )
        if state >= len(order):
            return Result.ok(Step(phase=PhaseId.DONE, done=True))

        phase = order[state]

        if phase is PhaseId.INTAKE:
            # 需求采集：登记 user/ 文件、建检索库、记起始时间。
            # 这三件事在本步都是"空转"（没有真文件、没有真语料），
            # **但必须真的调**——判据是"管线接通"，不是"这里有活干"。
            # 跳过它们的话，"能跑起来"就只是驱动循环自己在转。
            files = app.ports.user_files.scan_and_register()
            # **ref 用文件名**：只有文件名可用——此时材料还没读进来
            # （第 7 步的 D-023 铁律：AI 初始只拿元信息，内容按需读）。
            # 真正建库发生在编排层拿到内容之后，这里只保证链路通。
            got_build = app.ports.retriever.build(
                tuple((f.rel_path or f.name, f.name) for f in files)
            )
            if got_build.is_error:
                err = got_build.error
                assert err is not None
                return Result.fail(err.code, err.detail)
            _started_at = app.ports.clock.stamp()
            app.bus.emit(
                _ev(
                    app,
                    PhaseId.INTAKE,
                    EventKind.RUN_STARTED,
                    at=_started_at,
                    user_files=len(files),
                    # 用户要求「讲一下识别到几个文件和文件名即可」。
                    # **限长**：用户说「不要占太大空间」，故只带前若干个名字，
                    # 由投影层负责折行与省略号。发射端不做显示决策。
                    file_names=tuple(f.name for f in files),
                )
            )

        if phase in _PRODUCING:
            name, kind, role = _PRODUCING[phase]
            # 先取提示词（content_pack），再检索参考资料（retriever），
            # 最后调模型（llm）——顺序即 D-071 定的阶段内次序：
            # 没有提示词就没有角色，没有角色就不该产出。
            got_prompt = app.ports.content_pack.load_role_prompt(role)
            if got_prompt.is_error:
                err = got_prompt.error
                assert err is not None
                return Result.fail(err.code, err.detail)
            prompt = got_prompt.unwrap()

            got_hit = app.ports.retriever.search(name, top_k=4)
            if got_hit.is_error:
                err = got_hit.error
                assert err is not None
                return Result.fail(err.code, err.detail)

            got_llm = app.ports.llm.complete(
                LlmRequest(
                    # **模型名取自配置，用户手填**（D-082/D-039）。
                    # 此前写死 `model="stub-model"`——那在第 4 步是占位，
                    # 现在配置里没填就是空串，**代码不猜、不兜底**：
                    # 空模型名发出去会被服务端拒（`model_not_available`），
                    # 那正是用户该看到的真实反馈，比程序替用户编一个名字强。
                    model=app.config.model,
                    messages=(Message(role="user", content=prompt),),
                )
            )
            if got_llm.is_error:
                err = got_llm.error
                assert err is not None
                return Result.fail(err.code, err.detail)
            reply = got_llm.unwrap()

            _artifact(app, phase, name, kind, content=reply.text)

        if phase is PhaseId.GATING:
            # 先发 gate.started 再跑校验：门禁组是并行执行的（D-049），
            # 只发终局事件的话，日志里分不清"这次门禁跑了"与
            # "这次根本没轮到门禁"——而这两者的处置完全不同。
            app.bus.emit(_ev(app, PhaseId.GATING, EventKind.GATE_STARTED, script="hard"))
            # **D-196 接线补漏**：原先只传 `{"phase": ...}`，
            # 而真校验器要看的是 D-Card（硬约束零沉降 D-040）。
            # 假校验器不挑入参，于是这个缺口一直没人发现——
            # **接真件才暴露的接线缺口，正是 D-196 要还这笔债的理由**。
            状态 = st.load_run_state(app.run_id)
            卡 = 状态.unwrap().dcard if 状态.is_ok else DCard(by_id={})
            got = app.ports.validators.run_hard_constraint(
                {
                    "phase": phase.value,
                    "dcard": {
                        编号.value: (字段.value or "")
                        for 编号, 字段 in sorted(卡.by_id.items(), key=lambda kv: kv[0].value)
                    },
                }
            )
            if got.is_error:
                err = got.error
                assert err is not None
                # 直接把 err 整个传下去：code 与 detail 都在里面，
                # 不拆开重造，也就不可能丢掉其中之一
                return Result.fail(err.code, err.detail)
            result = got.unwrap()
            app.bus.emit(
                _ev(
                    app,
                    phase,
                    EventKind.GATE_PASSED if result.passed else EventKind.GATE_FAILED,
                    script="hard",
                )
            )
            if result.passed:
                # 门禁过则把终稿落库——顺手验证 storage 端口真的接得上，
                # 而不只是"构造时没报错"
                got_put = st.put_artifact(
                    ArtifactRef(
                        id=ArtifactId("main-document"),
                        kind=ArtifactKind.MAIN_DOCUMENT,
                        path="final.txt",
                    ),
                    "【桩】终稿（门禁通过后落库）",
                )
                if got_put.is_error:
                    err = got_put.error
                    assert err is not None
                    return Result.fail(err.code, err.detail)
            else:
                # 退回修正，不设绕过出口（D-028/D-038）
                app.bus.emit(
                    _ev(
                        app,
                        PhaseId.REVISION,
                        EventKind.GATE_REJECTED,
                        detail="硬约束不通过，退回修正",
                    )
                )
                # **结束本次运行并返回失败**，不原地重试。
                #
                # 曾写成 `state=state` 停下等下一轮重跑，但那个"下一轮"
                # 本步根本不存在（真实退回修正要重新起草，属 L5），
                # 于是状态机原地打转、一直撞 `max_steps` 保险丝——
                # 10000 次空转只为得到同一个结论。
                #
                # 退回是**异常结局而非正常结局**：真修好需要新的正文内容，
                # 那是人的动作，不该由驱动循环假装能做到。
                return Result.fail(ErrorCode.GATE_REWRITE_EXHAUSTED, "硬约束不通过，已退回修正")

        return Result.ok(Step(phase=phase, state=state + 1))

    return drive(run_id=app.run_id, bus=app.bus, initial_state=0, step=step)


def _artifact(app: App, phase: PhaseId, name: str, kind: str, *, content: str = "") -> None:
    """发一份阶段成果。

    **载全文**（D-072）：标准模式的成果卡直接由它渲染，
    界面拿到的与落盘的是同一份内容。
    """
    if not content:
        content = f"{STUB_MARK}{name}：由全假实现产出，仅用于验证管线接通，不得用于交付。"
    # 无论内容来自桩还是模型产出，**一律加桩标记**：
    # 假实现的产出同样是桩，分不清就会在第 5 步接真模型时误当真实结果。
    if not content.startswith(STUB_MARK):
        content = f"{STUB_MARK}{content}"
    # `phase` 用**本成果自己的阶段**，不用当前阶段常量：
    # 三份成果分属 direction / outline / drafting 三个阶段，
    # 混用会让界面把"文章大纲"显示在"写作方向确认单"阶段里，
    # 而这类归错在真实运行中极难从界面上看出来。
    app.bus.emit(_ev(app, phase, EventKind.ARTIFACT_READY, name=name, kind=kind, content=content))


def _ev(
    app: App,
    phase: PhaseId,
    kind_of_event: EventKind,
    *,
    level: Level = Level.INFO,
    **data: object,
) -> Event:
    """造一个事件。

    参数名用 `kind_of_event` 而非 `kind`：事件类型与成果种类
    （`phase.artifact_ready` 的 data 里带 `kind="outline"`）是两回事，
    同名会让调用点把两者混在一个位置参数与一个 data 键上，
    而 Python 允许、Pyright 报 `reportCallIssue`——
    这种"恰好撞名"正是最容易被误读为"我传对了"的写法。
    """
    return Event(seq=0, kind=kind_of_event, run_id=app.run_id, phase=phase, level=level, data=data)


def build_app_from_workspace(
    ws: Workspace,
    run_id: RunId | None = None,
    *,
    log_path: Path | None = None,
    llm: LlmPort | None = None,
    storage: StoragePort | None = None,
    validators: ValidatorPort | None = None,
    clock_override: ClockPort | None = None,
    user_files: UserFilesPort | None = None,
    content_pack: ContentPackPort | None = None,
    retriever: RetrieverPort | None = None,
) -> Result[App]:
    """从工作区装配 —— **真实运行的入口**（第 5 步）。

    与 `build_app` 的分工：
    `build_app` 是第 4 步的测试入口（全假端口，不碰配置）；
    本函数读 `system/config.toml`，并把**真实**密钥源接上。

    ## 为什么不直接让 `build_app` 读配置

    803 条测试全用假端口。若 `build_app` 一律读配置，
    每个测试都得先造一份 `config.toml`——那是纯仪式，
    且会让「测试用假配置」变成一种需要维护的假象。
    故拆成两个入口：**要真实配置的就调本函数**。

    ## 为什么不静默兜底

    配置读失败或未填时**返回错误，不代填**（D-082）：
    程序替用户编一个端点或模型名，比报错糟得多——
    用户会以为配置生效了，实际发出去的请求是错的。
    """
    got = load_config(ws)
    if got.is_error:
        err = got.error
        assert err is not None
        return Result.fail(err.code, err.detail)
    cfg = got.unwrap()

    checked = cfg.validate()
    if checked.is_error:
        err = checked.error
        assert err is not None
        return Result.fail(err.code, err.detail)

    # **不传就接真实现，传了尊重调用方。**
    #
    # 第 6 步零信任复查查出的缺口：原先这里把 `llm=None` 直接转发，
    # 而 `build_app` 见到 None 就装 `FakeLlm`——
    # 于是**用户配好了真端点，程序却拿假模型作答**。
    # 探测脚本能跑通，运行路径却是假的。
    #
    # 「真实运行入口」这个名字现在才名副其实。
    真密钥 = LocalSecrets.from_workspace(ws)
    真时钟 = clock_override if clock_override is not None else SystemClock()

    if llm is None:
        真传输 = HttpxTransport()
        llm = HttpLlm(
            endpoint=cfg.endpoint,
            model=cfg.model,
            transport=真传输,
            secrets=真密钥,
            clock=真时钟,
        )

    if retriever is None:
        # **同 D-165，第四次**：不传就接真实现。
        # 检索是「用户材料 → 可引用依据」的唯一通路，
        # 假实现意味着整条依据链是断的，而测试照样全绿。
        retriever = 中文检索()

    if user_files is None:
        # **同 D-165**：不传就接真适配器。原先这里直接转发 None，
        # `build_app` 见到 None 装 FakeUserFiles——
        # 用户放进 `user/` 的 docx 永远不会被读。
        # 「用户文件读取」是唯一让用户材料进入系统的入口，
        # 假实现意味着**整条材料链是断的**，而测试照样全绿。
        user_files = LocalUserFiles(根=ws.dir("user"))

    if storage is None:
        # **同 D-165，第 5 次**：本函数为 `llm`/`retriever`/`user_files`
        # 都写了「不传就接真实现」，**唯独漏了 storage**。
        # 于是 `build_app` 见到 None 装 `FakeStorage`——
        # 产物写进内存字典，**用户的工作区里一个文件都不会有**。
        # 症状与前几次完全一样：真配置、测试全绿、什么也没落盘。
        # 由 `tests/test_tool_path_guard.py::test_app装配出的工具层同样拦得住` 抓出。
        # **同型第 6 次（D-235）**：这里还得把产物编码传下去。
        # 用户要求「配置 `gb18030: true` 时产出改用 GB18030，
        # 但日志和界面始终 UTF-8」——`EncodingConfig.name()` 算得出来，
        # `FsStorage` 也有字段，但**没人往下传**，于是永远是默认 UTF-8。
        # 注意只喂 `产物编码`：**不能**喂 `encoding`，
        # 否则 `state.json` 与日志也变 GB18030，断点续跑与控制台会乱码。
        storage = FsStorage(ws=ws, 产物编码=cfg.encoding.name())

    if validators is None:
        # **D-196 的欠账到此还清**。原先这里把 None 直接转发，
        # `build_app` 见到 None 装 FakeValidators——
        # 症状与 D-165 五次完全一样：**真配置、测试全绿、校验根本没跑**。
        # 硬约束零沉降（D-040）是「缺则不得动笔」的唯一执行点，
        # 接假实现等于这条铁律在真实运行路径上不存在。
        validators = 硬约束校验器()

    # **D-210（第十一次 D-165）**：真内容包适配器此前**根本不存在**——
    # `adapters/` 里没有 `content_pack.py`，而本函数把 None 直接转发，
    # `build_app` 见到 None 装 `FakeContentPack`。
    # 于是真实运行路径上角色提示词是「【桩】xxx 的角色提示词」，
    # 而第 11 步装配器按需装载的规则条目**全是桩**。
    # 内容包本是「唯一不硬编码公文内容」的落点（D-055），它自己是假的。
    if content_pack is None:
        content_pack = 本地内容包(根=内容包根())

    return Result.ok(
        build_app(
            run_id,
            log_path=log_path,
            config=cfg,
            # 真实密钥源接在这里（D-030：唯一返回明文的端口）
            secrets=真密钥,
            llm=llm,
            storage=storage,
            validators=validators,
            clock_override=真时钟,
            user_files=user_files,
            content_pack=content_pack,
            retriever=retriever,
            # **导出器在真实运行路径上传真件**（D-296）。
            # 工作区根只有这一层拿得到（`build_app` 是测试入口，没有它），
            # 所以必须在这里造好往下传——
            # 否则「导出前扫残留占位符」那条硬约束仍然没有执行者。
            #
            # 编码只喂最终 TXT，**不能**让日志与界面跟着变 GB18030（D-235）。
            导出=导出交付物(
                根=ws.dir("data"),
                运行号=run_id if run_id is not None else RunId("r-0001"),
                编码=cfg.encoding.name(),
            ),
        )
    )
