"""第 4 批：零发射事件的补齐判据——**断言它们真的进了事件流**。

## 这组判据在防什么

架构 §5.4 把 37 个事件全列为契约，但有 7 个「定义即完成」：
`EventKind` 里有、架构表里有、**发射点与消费点都没有**。
读架构的人会以为它们存在。

补发射点只做了一半——**发了但没人验证，下一轮照样能悄悄退化**。
所以这里每条判据都断言「事件流里确实有它，且载荷对」。

## 前提：INTERNAL 事件也留痕

`kernel/journal.py:49` 的 `append` **不过白名单、全量落盘**。
所以补 INTERNAL 事件不是「补给空气」——它真能进
`system/journal.jsonl`，事后 grep 得到。这条前提一旦变了
（哪天 journal 加了过滤），本组判据不会响，但它就不再有意义；
判据本身断言的是**编排器发出的事件流**，与 journal 策略解耦。

## 第 1 批（本文件）：`error.raised` 与 `artifact.published`

这两个是**能立刻自证**的：现有代码的状态就足以让判据变红。

- `error.raised` 此前只有 `run.failed` 一个出口，而它带的是
  `错.code.value`——真正的 `错.detail` 随 `Result.fail` 返回后就没人知道，
  journal 里排障只剩一个 `E_...` 码。
- `artifact.published` 此前**零发射点**：落盘这件事在 journal 里
  没有任何痕迹，只有 `export.done` 携一份文件名列表。
  架构 §5.4 点名要防的「界面显示的和落盘的不是一份」因此无从对照。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

from zhengtong.adapters.fakes import FakeLlm, FakeStorage
from zhengtong.domain.artifact import ArtifactKind
from zhengtong.domain.errors import ErrorCode
from zhengtong.domain.event import Event, EventKind
from zhengtong.domain.role import RoleId
from zhengtong.domain.run_state import new_run_state
from zhengtong.domain.task import TaskSpec
from zhengtong.domain.tier import TierId

# `_过程` 是编排层的推进状态机，本就不该被外部构造；
# 判据要直接驱动导出这一步，只能碰它。这两处 ignore 是**明知故犯**，
# 且范围限定在「构造它」这一行，不是掩盖未知类型。
from zhengtong.orchestration.orchestrate import (
    _过程,  # pyright: ignore[reportPrivateUsage]
    编排器,
    阶段序列,
)
from zhengtong.services.export import 导出交付物
from zhengtong.services.tools.handlers import (
    flow_ask_user,
    工具依赖,
    记事件回调,
    问用户回调,
)


def _挑(事件们: list[Event], 种类: EventKind) -> list[Event]:
    """按种类筛事件。

    ## 为什么标注 `list[Event]` 而不是 `list[object]`

    第一版写 `list[object]`（外加两处 `pyright: ignore` 兜住 `.kind` / `.data`）。
    结果 BasedPyright 报 6 个 error：`list` 是**不变**容器，
    `list[Event]` 压根不能传给 `list[object]`。

    于是那两处 ignore 一点用没有——它们掩盖的是「类型写错了」，
    而类型一改好，ignore 就成了多余的噪音。**兜底层层相扣**。
    """
    return [e for e in 事件们 if e.kind is 种类]


def _造过程(编: 编排器) -> _过程:
    """造一个真实的 `_过程` 并挂到编排器上。

    ## 为什么不能直接 `编._过程`

    第一版判据写的是 `编._过程`，拿到的是 `None`——
    `_过程` **在 `跑到终` 里才创建**（`orchestrate.py:1880`）。
    于是判据报 `AttributeError`，看起来像生产代码坏了，
    实际是判据用错了入口。

    这里显式构造：它需要的六个参数都是真件
    （真编排器、真 llm、真存储、真阶段线、真状态），
    **没有一处替身**，所以测的确实是生产路径。
    """
    # **参数名按 `TaskSpec` 真实签名**（`tier` / `requirement`）。
    # 第一版凭印象写成 `需求=` + `tier_spec=`，报的是「不认这个字段」——
    # 那不是构造错，是判据没看签名。
    任务 = TaskSpec(tier=TierId.CONSULT, requirement="写一份通知")
    过 = _过程(
        编排=编,
        llm=编.llm,
        storage=编.storage,
        序列=阶段序列(TierId.CONSULT),
        状态=new_run_state(编.运行号, 任务),
        初始已完成=(),
    )
    编._过程 = 过  # pyright: ignore[reportPrivateUsage]
    return 过


# ── error.raised ────────────────────────────────────────────────


def test_运行失败时内部细节进事件流() -> None:
    """失败那一刻的**内部细节**必须留在事件流里。

    此前唯一的出口是 `run.failed`，而它带的是 `错.code.value`。
    `错.detail` 随 `Result.fail(...)` 返回给调用方之后就**再无留痕**——
    而调用方是 CLI/TUI，它转述的还是那句码。
    """
    llm = FakeLlm(fail_with=ErrorCode.LLM_REQUEST_FAILED)
    编 = 编排器(llm=llm, storage=FakeStorage(), 模型名="m")
    got = 编.跑到终(档位=TierId.CONSULT, 需求="写一份通知")

    assert got.is_error, "这个构造本该失败；不失败则本判据验不到东西"
    内部 = _挑(编.事件, EventKind.ERROR_RAISED)
    assert 内部, "失败时没有 error.raised：内部错误细节无处可查（架构 §5.4 的留痕机制从未存在）"
    细节 = " ".join(str(e.data.get("detail", "")) for e in 内部)
    assert "FakeLlm 被要求失败" in 细节, (
        f"error.raised 没带上真实的 detail，只有 {细节!r}——"
        f"那和没发一样：码能定位失败点，但看不出失败原因"
    )


def test_run_failed仍只给用户看错误码() -> None:
    """**补内部留痕不得改用户可见内容**。

    `run.failed` 是用户白名单事件，架构要求它给的是可读的结论。
    若顺手把内部 detail 也塞进去，用户会看到内部异常名——
    架构 §5.4 明确「`error.raised` 刻意不进白名单」正是这个道理。
    """
    llm = FakeLlm(fail_with=ErrorCode.LLM_REQUEST_FAILED)
    编 = 编排器(llm=llm, storage=FakeStorage(), 模型名="m")
    _ = 编.跑到终(档位=TierId.CONSULT, 需求="写一份通知")

    用户见 = _挑(编.事件, EventKind.RUN_FAILED)
    assert 用户见, "失败必须有 run.failed（D-192：不得静默失败）"
    给用户 = " ".join(str(e.data.get("detail", "")) for e in 用户见)
    assert "FakeLlm 被要求失败" not in 给用户, f"内部细节漏进了用户可见的 run.failed：{给用户!r}"


def test_error_raised不进用户白名单() -> None:
    """`error.raised` 必须是 INTERNAL。

    若哪天它进了白名单，`StandardProjection` 就会把它当用户可见内容显示
    ——架构明说「直接放行会让用户看到内部异常名」。
    """
    from zhengtong.domain.event import is_user_visible

    assert not is_user_visible(EventKind.ERROR_RAISED), (
        "error.raised 不该进用户白名单：它携的是内部异常细节"
    )


# ── artifact.published ──────────────────────────────────────────


def test_导出成功后有落盘留痕且hash可复算(tmp_path: Path) -> None:
    """**`hash` 的唯一价值是能被复算**，所以判据真的去读文件算一遍。

    架构 §5.4 说调试模式拿它与 `phase.artifact_ready` 对照，
    防「界面显示的和落盘的不是一份」。对照的前提是两侧都有可比的量——
    此前落盘侧只有文件名，等于没有可比量。

    注意用的是 `read_bytes()`：hash 定义为**写入字节**的 sha256。
    """
    导出 = 导出交付物(根=tmp_path, 运行号="r-1")
    编 = 编排器(
        llm=FakeLlm(),
        storage=FakeStorage(),
        导出=导出,
        模型名="m",
    )
    过 = _造过程(编)
    过.交付物 = "关于做好近期工作的通知"
    过._导出交付物()  # pyright: ignore[reportPrivateUsage]

    留痕 = _挑(编.事件, EventKind.ARTIFACT_PUBLISHED)
    assert 留痕, "导出成功后没有 artifact.published：落盘在 journal 里没有痕迹"
    assert len(留痕) == 1, f"一份主件应留一条痕，实得 {len(留痕)}"

    载荷 = 留痕[0].data
    落 = tmp_path / "r-1" / "output" / str(载荷["name"])
    assert 落.exists(), f"事件说落盘了，实际没有：{落}"
    复算 = hashlib.sha256(落.read_bytes()).hexdigest()
    assert 载荷["hash"] == 复算, (
        f"hash 复算不出来：事件里 {载荷['hash']!r}，文件实际 {复算!r}。"
        f"对不上就等于没有——架构要的那个对照会给出错误结论"
    )
    assert 载荷["artifact_kind"] == ArtifactKind.MAIN_DOCUMENT.value


def test_导出被拦时不发落盘留痕(tmp_path: Path) -> None:
    """**没落盘就不能说落过盘**。

    残留占位符被拦下时不发 `artifact.published`，
    否则 journal 会记下一条「已落盘」而磁盘上什么都没有——
    那是比不记更坏的假痕迹。
    """
    导出 = 导出交付物(根=tmp_path, 运行号="r-1")
    编 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 导出=导出, 模型名="m")
    过 = _造过程(编)
    过.交付物 = "标题：【待填 · D3】"  # 真实占位符格式（export.py:54）
    过._导出交付物()  # pyright: ignore[reportPrivateUsage]

    assert _挑(编.事件, EventKind.EXPORT_BLOCKED), "残留占位符必须拦下（用户硬约束）"
    assert not _挑(编.事件, EventKind.ARTIFACT_PUBLISHED), (
        "导出被拦却仍发了落盘留痕：journal 会记下一条磁盘上并不存在的落盘"
    )


def test_未接线导出器不发落盘留痕() -> None:
    """占位导出器返回 fail，因此**不得有任何落盘留痕**（D-192）。"""
    编 = 编排器(llm=FakeLlm(), storage=FakeStorage(), 模型名="m")
    过 = _造过程(编)
    过.交付物 = "正文"
    过._导出交付物()  # pyright: ignore[reportPrivateUsage]

    assert not _挑(编.事件, EventKind.ARTIFACT_PUBLISHED), (
        "未接线导出器不得发出落盘留痕：那会记下一条根本没发生的落盘"
    )


def test_落盘摘要类型在端口层就定死() -> None:
    """导出端口必须返回 `落盘摘要`，不是文件名——**结构判据**。

    若哪天有人把返回类型改回 `tuple[str, ...]`，
    `artifact.published` 就发不出 `kind` 与 `hash`（或干脆发不出事件）。
    这条判据在编译期之外再加一道机械保障。
    """
    from zhengtong.ports.export import ExportPort, 落盘摘要

    assert 落盘摘要.__dataclass_fields__.keys() == {"文件名", "种类", "hash"}, (
        "落盘摘要的字段就是架构 §5.4 规定的那三个，别多也别少"
    )
    # 协议签名与实现的签名必须一致：端口改了而实现没改，接线当场断。
    import inspect

    协议签 = inspect.signature(ExportPort.__call__)
    实现签 = inspect.signature(导出交付物.__call__)
    # **两侧都用类属性访问**，于是两边都带 `self`，直接比即可。
    #
    # 这里栽过两次：第一次拿**实例**的 `.__call__`（bound，无 self）去比
    # **类**的 `.__call__`（有 self），报了个假失败；第二次误以为
    # 类访问也不带 self，于是加了 `[1:]` 切片——又报一次假失败。
    # 两次都是判据自己错，而两次的错都长得像生产代码坏了。
    assert list(协议签.parameters) == list(实现签.parameters), (
        f"ExportPort 与实现的参数名不一致：协议 {list(协议签.parameters)} vs 实现 {list(实现签.parameters)}"
    )
    # `return_annotation` 是 `Any`——**判据自己用 Any 判类型**，
    # 那正是本项目要消灭的形态。故显式 cast 成 str 再比。
    协议返回 = cast("str", 协议签.return_annotation)
    实现返回 = cast("str", 实现签.return_annotation)
    assert 协议返回 == 实现返回, f"返回类型不一致：协议 {协议返回} vs 实现 {实现返回}"


# ── run.resumed ────────────────────────────────────────────────


def _跑到终(编: 编排器) -> None:
    """跑一次。**成不成都行**——`run.resumed` 在 `drive` 之前发，
    所以失败那轮照样能验它。强制成功只会让判据依赖模型脚本。"""
    _ = 编.跑到终(档位=TierId.CONSULT, 需求="写一份通知")


def test_有已完成调用时发续跑标记() -> None:
    """**续跑在 journal 里此前没有任何痕迹**——只有一条 `run.started`。

    于是事后无从区分「一次跑完」与「跑了三次接起来」。而排查
    「为什么这次这么快」「这次模型只调了两次」时，最该看的恰恰是
    续跑账（`state.json` 的 `completed_call_ids`）。

    判据用**调用方显式传入**的 `初始已完成`：那是 D-065 的幂等入口，
    非空即意味着本轮会跳过这些调用——一次续跑的确凿证据。
    """
    编 = 编排器(
        llm=FakeLlm(fail_with=ErrorCode.LLM_REQUEST_FAILED),
        storage=FakeStorage(),
        模型名="m",
        初始已完成=("call-a", "call-b"),
    )
    _跑到终(编)

    续跑 = _挑(编.事件, EventKind.RUN_RESUMED)
    assert 续跑, "带了已完成调用却不发 run.resumed：续跑在 journal 里没有痕迹"
    assert 续跑[0].data.get("已完成调用数") == 2, f"续跑标记必须带跳过的调用数：{续跑[0].data}"


def test_首次运行不发续跑标记() -> None:
    """**空则不发**——每次都发的话它就成了噪声。

    而噪声的代价很实在：journal 里到处都是 `run.resumed`，
    真出现一次续跑时反而看不见了。
    """
    编 = 编排器(
        llm=FakeLlm(fail_with=ErrorCode.LLM_REQUEST_FAILED),
        storage=FakeStorage(),
        模型名="m",
    )
    _跑到终(编)
    assert not _挑(编.事件, EventKind.RUN_RESUMED), (
        "首次运行（无已完成调用）也发了 run.resumed：该事件会退化成噪声"
    )


def test_续跑标记跟在开始事件之后() -> None:
    """**顺序是契约的一部分**：先 `run.started` 再 `run.resumed`。

    反过来看就说不通了——「这轮是接着跑的」是对已开始这件事的补充说明，
    不是对它的事前声明。
    """
    编 = 编排器(
        llm=FakeLlm(fail_with=ErrorCode.LLM_REQUEST_FAILED),
        storage=FakeStorage(),
        模型名="m",
        初始已完成=("call-a",),
    )
    _跑到终(编)
    序 = [e.kind for e in 编.事件]
    开始 = 序.index(EventKind.RUN_STARTED)
    续跑 = 序.index(EventKind.RUN_RESUMED)
    assert 开始 < 续跑, f"顺序反了：{序}"


# ── user.answered ──────────────────────────────────────────────


def _依赖(*, 问用户: object, 记事件: object = None) -> 工具依赖:
    from zhengtong.adapters.fakes import (
        FakeContentPack,
        FakeRetriever,
        FakeUserFiles,
        FakeValidators,
    )
    from zhengtong.domain.ids import RunId

    return 工具依赖(
        storage=FakeStorage(),
        user_files=FakeUserFiles(),
        retriever=FakeRetriever(),
        content_pack=FakeContentPack(),
        validators=FakeValidators(),
        run_id=RunId("r-1"),
        问用户=cast("问用户回调", 问用户),
        记事件=cast("记事件回调 | None", 记事件),
    )


def test_答复到手后发user_answered() -> None:
    """问答闭环的另一半。

    此前只有 `user.question_raised`，**答复侧零留痕**——
    journal 无法回答「这次提问到底有没有得到答复」。
    而「没答复」和「卡住了」的现场症状都是**程序停住不动**，排障只能靠猜。
    """
    记下的: list[tuple[str, dict[str, object]]] = []

    def 记(kind: EventKind, **载荷: object) -> None:
        记下的.append((kind.value, 载荷))

    def 答(_字段: tuple[dict[str, object], ...]) -> str:
        return "答复内容"

    d = _依赖(问用户=答, 记事件=记)
    got = flow_ask_user(d, RoleId.TEAM_LEAD, {"fields": [{"code": "D1"}]})
    assert got.is_ok, got.error.detail if got.error else ""
    assert any(k == "user.answered" for k, _ in 记下的), f"答复到手却没有 user.answered：{记下的}"


def test_user_answered只记字段码不记答案全文() -> None:
    """**答案不进 journal**（D-030：可能含用户不愿外传的实质）。

    载荷只留 `fields`（字段码），不含答复文本。
    这条判据存在的意义是：将来有人「顺手把答案也带上方便排查」，
    那等于把用户口述的实质内容写进日志文件。
    """
    记下的: list[tuple[str, dict[str, object]]] = []

    密文 = "我们决定下个月把预算砍到三千万"

    def 记(kind: EventKind, **载荷: object) -> None:
        记下的.append((kind.value, 载荷))

    def 答(_字段: tuple[dict[str, object], ...]) -> str:
        return 密文

    d = _依赖(问用户=答, 记事件=记)
    _ = flow_ask_user(d, RoleId.TEAM_LEAD, {"fields": [{"code": "D1"}, {"code": "D2"}]})

    答了 = [载 for k, 载 in 记下的 if k == "user.answered"]
    assert 答了, "先得有这条事件才能验载荷"
    assert 密文 not in str(答了), f"答复全文进了事件载荷：{答了}"
    assert 答了[0].get("fields") == "D1,D2", f"应只记字段码，实得 {答了[0]}"


def test_未接线的提问不发user_answered() -> None:
    """**没提问成功就不能有答复**。

    否则 journal 里出现一条 `user.answered` 而从无 `question_raised`，
    排障时那是一条假痕迹。
    """
    记下的: list[str] = []

    def 记(kind: EventKind, **载荷: object) -> None:
        del 载荷
        记下的.append(kind.value)

    d = _依赖(问用户=None, 记事件=记)
    got = flow_ask_user(d, RoleId.TEAM_LEAD, {"fields": [{"code": "D1"}]})
    assert got.is_error, "未接线的提问应当失败（D-192）"
    assert "user.answered" not in 记下的, f"未接线却发了 user.answered：{记下的}"


def test_没挂记事件时提问仍能成功() -> None:
    """**留痕不得影响主流程**。

    `工具依赖.记事件` 是可选的（`None`）。若 `user.answered` 在
    没挂回调时抛异常，提问功能会被「留痕」这条旁路拖垮——
    那正是 D-266 那类「插话转达恒为 None」的接线风险。
    """

    def 答(_字段: tuple[dict[str, object], ...]) -> str:
        return "答复"

    d = _依赖(问用户=答, 记事件=None)
    got = flow_ask_user(d, RoleId.TEAM_LEAD, {"fields": [{"code": "D1"}]})
    assert got.is_ok, f"没挂记事件不该让提问失败：{got.error.detail if got.error else ''}"
    assert got.unwrap() == "答复"
