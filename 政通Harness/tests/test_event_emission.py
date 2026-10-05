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
