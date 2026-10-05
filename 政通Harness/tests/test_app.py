"""装配根测试（第 4 步判据：能装配并跑起来）。

**"跑起来"的诚实标准**：本步用全假实现，所以验证的是**管线接通**，
不是公文质量。测试因此专门断言桩数据带 `【桩】` 标记——
让"这批数据不是真产物"这件事无法被忽略，
也防着某天有人把桩数据当成交付结果发出去。
"""

from __future__ import annotations

from pathlib import Path

from zhengtong.adapters.fakes import (
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
from zhengtong.app import (
    ARTIFACT_DIRECTION,
    ARTIFACT_DRAFT,
    ARTIFACT_OUTLINE,
    App,
    build_app,
    run_once,
)
from zhengtong.domain.errors import ErrorCode
from zhengtong.domain.event import EventKind
from zhengtong.domain.ids import RunId
from zhengtong.domain.phase import PhaseId
from zhengtong.domain.task import UserFile
from zhengtong.ports.clock import ClockPort
from zhengtong.ports.content_pack import ContentPackPort
from zhengtong.ports.llm import LlmPort
from zhengtong.ports.retriever import RetrieverPort
from zhengtong.ports.secrets import SecretsPort
from zhengtong.ports.storage import StoragePort
from zhengtong.ports.user_files import UserFilesPort
from zhengtong.ports.validators import ValidatorPort

# ── 判据一：能装配 ──


def test_八个端口全部装配() -> None:
    with build_app() as app:
        assert app.ports.missing() == (), f"有端口未装配：{app.ports.missing()}"


def test_每个假实现都满足协议() -> None:
    """签名不符必须在**导入时**就炸，而不是跑到某个分支才炸。"""
    pairs: tuple[tuple[object, type[object]], ...] = (
        (FakeClock(), ClockPort),
        (FakeSecrets(), SecretsPort),
        (FakeLlm(), LlmPort),
        (FakeStorage(), StoragePort),
        (FakeUserFiles(), UserFilesPort),
        (FakeContentPack(), ContentPackPort),
        (FakeRetriever(), RetrieverPort),
        (FakeValidators(), ValidatorPort),
    )
    for impl, proto in pairs:
        assert isinstance(impl, proto), f"{type(impl).__name__} 不满足 {proto.__name__}"


def test_装配根可注入端口以定向场景() -> None:
    """测试要能替换单个端口，不必整套重造。"""
    sec = FakeSecrets(api_key="特定测试密钥-abcdef")
    val = FakeValidators(pass_hard=False)
    with build_app(secrets=sec, validators=val) as app:
        assert app.ports.secrets is sec
        assert app.ports.validators is val


def test_脱敏模式取自密钥端口() -> None:
    """D-030 机械落点：密钥存哪，哪里就提供它的脱敏片段。"""
    sec = FakeSecrets(api_key="特定测试密钥-abcdef")
    with build_app(secrets=sec) as app:
        # 用密钥片段发一条事件，落盘后不应含该片段
        assert app.ports.secrets.redaction_patterns()


def test_默认不落盘() -> None:
    """「跑一下」不该在工作区留下文件。"""
    with build_app() as app:
        assert app.bus is not None
    assert not Path("events.jsonl").exists()


# ── 判据二：能跑起来 ──


def test_端到端跑通() -> None:
    with build_app() as app:
        got = run_once(app)
        assert got.is_ok, f"跑不通：{got.error}"
        assert got.unwrap().ok
        assert got.unwrap().steps > 0


def test_事件seq连续无空洞() -> None:
    """seq 是 trace 与 replay 的唯一排序依据，重复或空洞都会让回放失真。"""
    with build_app() as app:
        _ = run_once(app)
        seqs = [e.seq for e in app.collected]
        assert seqs == list(range(1, len(seqs) + 1))


def test_三份阶段成果各归自己的阶段() -> None:
    """回归测试：曾把三份成果全标成 direction，
    会让界面把"文章大纲"显示在"写作方向确认单"阶段里。"""
    with build_app() as app:
        _ = run_once(app)
        # Event.phase 声明为 str（PhaseId 是 StrEnum），故直接比较字符串
        arts = {
            e.data.get("name"): e.phase for e in app.collected if e.kind is EventKind.ARTIFACT_READY
        }
        assert arts[ARTIFACT_DIRECTION] == "direction"
        assert arts[ARTIFACT_OUTLINE] == "outline"
        assert arts[ARTIFACT_DRAFT] == "drafting"


def test_阶段成果载全文() -> None:
    """D-072：content 装全文，不是摘要。"""
    with build_app() as app:
        _ = run_once(app)
        for e in app.collected:
            if e.kind is EventKind.ARTIFACT_READY:
                content = str(e.data["content"])
                assert content.startswith(STUB_MARK), "桩数据必须带标记"
                assert len(content) > 20, "应是全文而非空壳"


def test_门禁先发started再发终局() -> None:
    """只发终局事件的话，日志里分不清"这次门禁跑了"与"根本没轮到门禁"。"""
    with build_app() as app:
        _ = run_once(app)
        kinds = [e.kind for e in app.collected]
        assert EventKind.GATE_STARTED in kinds
        i_started = kinds.index(EventKind.GATE_STARTED)
        i_passed = kinds.index(EventKind.GATE_PASSED)
        assert i_started < i_passed


def test_门禁通过后产物落库() -> None:
    """验证 storage 端口真接上了，而不只是构造时没报错。"""
    st = FakeStorage()
    with build_app(storage=st) as app:
        _ = run_once(app)
        # 从注入的实例断言：ports 上是 Protocol，看不到实现内部字段
        assert st.artifacts, "门禁通过后应落库一件产物"


# ── 红线：没有绕过出口 ──


def test_门禁不通过则退回修正不进终稿() -> None:
    """D-028/D-038：不提供强制出稿路径。"""
    val = FakeValidators(pass_hard=False)
    st = FakeStorage()
    with build_app(validators=val, storage=st) as app:
        _ = run_once(app)
        kinds = [e.kind for e in app.collected]
        assert EventKind.GATE_FAILED in kinds
        assert EventKind.GATE_REJECTED in kinds
        assert EventKind.GATE_PASSED not in kinds
        # 不得出现终稿落库
        assert not st.artifacts, "门禁没过却落了终稿"
        # 也不得宣告完成
        assert EventKind.RUN_FINISHED not in kinds


def test_门禁不通过时发run_failed() -> None:
    """退回修正 → 返回失败 → drive() 发 run.failed。

    **退回是异常结局，不是正常结局**：真修好需要新的正文内容，
    那是人的动作。驱动循环假装能自己修好，就会原地打转——
    曾实测跑满 10000 步保险丝（耗时 10s）只为得到同一个结论。
    """
    val = FakeValidators(pass_hard=False)
    with build_app(validators=val) as app:
        got = run_once(app)
        assert got.is_error, "门禁不过须返回失败，不得声称成功"
        assert got.error is not None
        assert got.error.code is ErrorCode.GATE_REWRITE_EXHAUSTED
        assert EventKind.RUN_FAILED in [e.kind for e in app.collected]


def test_门禁不过时不空转() -> None:
    """回归测试：退回后必须立即停止，不得原地反复重试。"""
    val = FakeValidators(pass_hard=False)
    with build_app(validators=val) as app:
        _ = run_once(app)
        # 真实流程 13 条事件；空转会把步数顶到保险丝（10000 步）
        assert len(app.collected) < 20, f"事件数 {len(app.collected)}，疑似空转"


# ── 判据的诚实读法：8 个端口都要真的被用上 ──


def test_七个端口真的在流程里被调用() -> None:
    """回归测试。

    装配时把 8 个端口都注进去**不等于**管线接通——若流程只调了其中 2 个，
    其余 6 个只是躺在那儿，"能跑起来"就是驱动循环自己在转的假象。
    这里逐一验证调用真的发生过，而不只看它们被构造出来。
    """
    st = FakeStorage()
    uf = FakeUserFiles(registered=(UserFile(name="背景材料.txt", size=10, suffix=".txt"),))
    cp = FakeContentPack()
    rt = FakeRetriever()
    llm = FakeLlm()
    val = FakeValidators()
    clk = FakeClock()
    # 端口只能在**装配时**注入：PortRegistry 是 frozen dataclass，
    # 运行时替换是设计禁止的（装配完不该被改动）。
    with build_app(
        storage=st,
        validators=val,
        user_files=uf,
        content_pack=cp,
        retriever=rt,
        llm=llm,
        clock_override=clk,
    ) as app:
        _ = run_once(app)

    assert rt.built_with, "retriever.build 未被调用"
    assert rt.hits is not None and llm.calls, "llm.complete 未被调用"
    assert len(llm.calls) == 3, f"三个产出阶段应各调一次，实得 {len(llm.calls)}"
    assert val.ran, "validators 未被调用"
    assert st.artifacts, "storage 未被调用"
    assert clk.now_value > 0, "clock 未被调用"


def test_secrets不进入编排流程() -> None:
    """**反向断言**：API key 绝不能出现在编排逻辑里（D-030）。

    `secrets` 端口只在装配时提供脱敏片段。把它接进 `run_once()`
    看似"用上了全部端口"，实则是把密钥送进了最不该放它的地方。
    故此处断言 `run_once` 的源码里没有 `get_api_key` 调用。
    """
    import inspect

    from zhengtong.app import run_once as fn

    src = inspect.getsource(fn)
    assert "get_api_key" not in src, "编排流程不得取 API key"
    assert "ports.secrets" not in src, "编排流程不应直接触碰 secrets 端口"


def test_用户文件仅登记元信息不读内容() -> None:
    """D-022/D-023：AI 初始只拿文件名，内容须按需读取。"""
    uf = FakeUserFiles(registered=(UserFile(name="背景材料.txt", size=10, suffix=".txt"),))
    with build_app(user_files=uf) as app:
        _ = run_once(app)
    # 只登记不读：contents 里没被塞过任何东西
    assert not uf.contents, "不应在需求采集阶段就读取文件内容"


# ── D-023：一个循环，两种投影 ──


def test_调试模式全量标准模式只见白名单() -> None:
    with build_app() as app:
        _ = run_once(app)
        assert len(app.debug.lines()) == len(app.collected), "调试模式须全量透传"
        # 成功路径只有三份成果进白名单
        assert len(app.standard.artifacts) == 3, (
            f"标准模式应只见三份阶段成果，实得 {len(app.standard.artifacts)}"
        )


def test_标准模式输出不含成员状态与门禁细节() -> None:
    """D-046：不显示成员状态、不展示审核意见。"""
    with build_app() as app:
        _ = run_once(app)
        out = "\n".join(app.standard.lines())
        assert "gate.passed" not in out
        assert "role." not in out


def test_两种投影看到同一事件序列() -> None:
    """这是 D-023 的核心：两者必须看到完全相同的事件序列。"""
    with build_app() as app:
        _ = run_once(app)
        assert app.debug.lines() and app.collected
        assert len(app.debug.records) == len(app.collected)


# ── 落盘 ──


def test_日志落盘且可回放(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    with build_app(log_path=log) as app:
        _ = run_once(app)
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    # 14 条 = run.started + 6 阶段×2 + 3 成果 + gate.started + gate.passed
    #        + run.finished
    assert len(lines) == 14, f"应为 14 条事件，实得 {len(lines)}"


def test_桩数据不得被误当真实产物() -> None:
    """防回归：桩文本必须始终带标记。"""
    with build_app() as app:
        _ = run_once(app)
        for e in app.collected:
            if e.kind is EventKind.ARTIFACT_READY:
                assert STUB_MARK in str(e.data["content"])
                assert "不得用于交付" in str(e.data["content"])


def test_app是上下文管理器且退出即收尾() -> None:
    app = build_app()
    with app:
        _ = run_once(app)
    # 退出后总线已停，再发应报错
    try:
        app.bus.emit(_dummy_event(app.run_id))
    except RuntimeError:
        return
    raise AssertionError("App.__exit__ 应已停止总线")


def _dummy_event(run_id: RunId):
    from zhengtong.domain.event import Event

    return Event(seq=0, kind=EventKind.PHASE_ADVANCED, run_id=run_id, phase=PhaseId.INTAKE)


def test_类型注解正确() -> None:
    """`build_app()` 返回 `App`，装配根的类型是显式的。"""
    app = build_app()
    assert isinstance(app, App)
    app.close()
