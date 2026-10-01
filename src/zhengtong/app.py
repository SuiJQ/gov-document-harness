"""装配根（第 4 步）。

**全项目唯一的装配点**（D-070）。TUI、无界面模式、doctor 三者
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
from .domain.artifact import ArtifactKind, ArtifactRef
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
from .ports.llm import LlmRequest, Message
from .ports.registry import PortRegistry

#: 三个阶段成果的名字（D-030）。标准模式只显示这三样（D-046）。
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
    #: 收集全部事件，供测试与调试
    collected: list[Event] = field(default_factory=list)

    def close(self) -> None:
        """收尾。**必须排空总线**（D-087）——
        否则未处理的事件随进程退出消失，用户看不到、日志里也没有。"""
        self.bus.stop()

    def __enter__(self) -> App:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def build_app(
    run_id: RunId | None = None,
    *,
    log_path: Path | None = None,
    secrets: FakeSecrets | None = None,
    storage: FakeStorage | None = None,
    validators: FakeValidators | None = None,
    clock_override: FakeClock | None = None,
    user_files: FakeUserFiles | None = None,
    content_pack: FakeContentPack | None = None,
    retriever: FakeRetriever | None = None,
    llm: FakeLlm | None = None,
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

    app = App(
        ports=ports,
        bus=bus,
        standard=StandardProjection(),
        debug=DebugProjection(),
        run_id=run_id if run_id is not None else RunId("r-0001"),
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
            got_build = app.ports.retriever.build(tuple(f.name for f in files))
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
                    # 模型名由用户手填（D-039），此处不编造——
                    # 假实现阶段用固定占位名，第 5 步接真配置时替换。
                    model="stub-model",
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
            got = app.ports.validators.run_hard_constraint({"phase": phase.value})
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
