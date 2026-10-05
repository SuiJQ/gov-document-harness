"""追加式 JSONL 事件日志。

**只追加，不改写**（架构 §5.2）：事件已经发生过，就不该被后来的状态覆盖。
这一条同时让 journal 天然幂等——重复回放同一段不会破坏历史。

**只写脱敏后的内容**：总线在调 `append` 之前就脱敏过了，
所以这里**不需要**再做一次。重复脱敏不会出错（幂等）但会掩盖问题——
真出现"journal 里有密钥"时，说明总线的脱敏被绕过了，
而二次脱敏会把证据洗掉，反而更难查。
"""

# 本文件放宽 reportAny / reportUnknown*：`json.loads` 官方返回 `Any`，
# 这是 JSON 的固有性质而非缺陷。已在 `_as_object` 这一唯一入口收窄为
# `dict[object, object]`，之后每个字段取值都过 `_req_str` / `_opt_str` 等
# 显式校验，缺字段会抛 ValueError 而非静默取到 None。
# （实测：`reportAny` 用 pyproject 的 overrides 无效，只有文件级注释生效。）
# pyright: reportAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnusedCallResult=false
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import IO, Protocol, cast, runtime_checkable

from ..domain.event import Event
from ..domain.ids import RunId


@runtime_checkable
class JournalPort(Protocol):
    """事件落盘。"""

    def append(self, event: Event) -> None:
        """追加一条。**内容必须已脱敏。**"""
        ...

    def read_all(self) -> tuple[Event, ...]:
        """读回全部。用于回放与排查。"""
        ...


class JsonlJournal:
    """文件实现。**单线程调用**（只有泵线程写）。"""

    def __init__(self, path: Path) -> None:
        self._path: Path = path

    def append(self, event: Event) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 追加写 + 不缓冲刷新：事件日志的价值全在"崩了也留痕"，
        # 缓冲里的内容在进程被杀时就丢了。
        line = json.dumps(_to_json(event), ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as fh:
            _ = fh.write(line + "\n")
            # 审计 D-135：**append 也必须刷盘**。
            # 原实现只 write + close，而 close 只 flush 到 page cache，
            # **不是落盘**——断电或内核崩溃时最后若干条事件就没了。
            # 而 journal 恰恰是断点续跑（D-027/D-082）的唯一依据：
            # `append` 自己的注释写着「事件日志的价值全在『崩了也留痕』」，
            # 却是本仓库唯一没做 fsync 的持久化路径。
            # （`adapters/atomic.py` 早就为原子写加了这层保障，
            #   第 1 批审计已确认其必要。）
            #
            # 为什么不批量刷：`append` 每次 open/write/close，
            # 文件句柄不跨调用保留，**没有批量点可攒**。
            # 实测每条 fsync 约 0.39 ms，一次成文按 80 个事件算
            # 额外约 31 ms——远小于一次模型调用的耗时，可以全开。
            # 若将来事件量级上升，正确做法是让句柄常驻并按条刷，
            # 而不是退回不刷盘。
            _ = _刷盘(fh)

    def read_all(self) -> tuple[Event, ...]:
        """读回全部事件。**跳过损坏行，但一条好的都不丢**（审计 D-123）。

        ## 为什么必须容错

        追加写不是原子的：进程被强杀（断电、OOM、用户 Ctrl-C）
        时缓冲里最后那行往往只写了一半。

        **这不是异常情况，而是崩溃日志的常态**——
        而断点续跑（D-027/D-082）恰恰只在崩溃之后才会用到。
        架构 §8.6 承诺「内核是状态机而不是脚本，所以续跑天然是安全的」，
        若这里一读到半行就抛 `JSONDecodeError`，
        则**前 N-1 条完好记录也一并读不出来**，续跑在最需要它的时刻失效。

        故：坏行跳过，好的留下。
        解析本身仍走 `_from_json` 的严格校验——
        **不因为容错就把「字段缺失」也一并放过**，
        那是另一类问题（人为构造的坏数据），该暴露。
        """
        if not self._path.exists():
            return ()
        out: list[Event] = []
        for 行号, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                out.append(_from_json(_as_object(cast("object", json.loads(line)))))
            except (ValueError, TypeError):
                # ValueError 覆盖 JSONDecodeError 与「不是 JSON 对象」；
                # TypeError 覆盖缺字段时的取值错误。
                #
                # **只跳过这一行**：末行半写是物理事实，前面每一行都完好，
                # 丢掉它们等于让一次崩溃抹掉整段历史。
                _记损坏行(行号)
                continue
        return tuple(out)


def _刷盘(fh: IO[str]) -> None:
    """flush + fsync，**失败不抛**——刷盘失败不该让流程崩掉。

    取与 `adapters/atomic.py` 同一套策略：
    - flush 把 Python 缓冲推给 OS，返回值无用但调用必要
    - fsync 才真正落盘；POSIX 上没有它，断电就丢数据
    - fsync 在 FAT32/某些网络盘上不可用，那里做降级而非拒绝
      （第 1 批已在 `atomic.py` 实测并确认该降级是必要的）
    """
    _ = fh.flush()
    try:
        _ = os.fsync(fh.fileno())
    except OSError:
        # 记 debug 而非 warning：这是环境限制，不是数据损坏。
        # 且 D-123 已保证即便真丢了末行，好数据仍可读回。
        logging.getLogger(__name__).debug("fsync 不可用，事件仅落到页缓存", exc_info=True)


def _记损坏行(行号: int) -> None:
    """记一条 warning：**跳过坏行这件事本身要被看见**。

    静默跳过等于把数据损坏藏起来——用户以为续跑正常，
    实际丢了若干条事件。用项目既有的 `logging` 惯例（见 `atomic.py`），
    不另建一套。
    """
    logging.getLogger(__name__).warning(
        "事件日志第 %d 行损坏，已跳过（常见于进程被强杀时末行只写了一半）", 行号
    )


def _as_object(value: object) -> dict[str, object]:
    """把 `json.loads` 的结果收窄成对象。

    `json.loads` 官方返回 `Any`，不收窄则整条读取链的类型信息全断——
    这是 JSON 边界的固有性质，故在**唯一的入口**收一次，
    好过让 `Any` 传染到每个字段取值处。
    """
    if not isinstance(value, dict):
        raise ValueError(f"事件行不是 JSON 对象：{value!r}")
    mapping = cast("dict[object, object]", value)
    return {str(k): v for k, v in mapping.items()}


def _to_json(event: Event) -> dict[str, object]:
    """事件转可序列化结构。

    显式枚举字段而非通用 encoder：事件要长期存在日志里，
    字段顺序与命名必须稳定可 grep，通用 encoder 不保证这点。
    """
    return {
        "seq": event.seq,
        "kind": event.kind.value,
        "run_id": event.run_id,
        "phase": event.phase,
        "level": event.level.value,
        "audience": event.audience.value,
        "call_id": event.call_id,
        "span": (
            None
            if event.span is None
            else {"role": event.span.role, "gate": event.span.gate, "round_no": event.span.round_no}
        ),
        "data": dict(event.data),
    }


def _from_json(raw: dict[str, object]) -> Event:
    """从 JSON 还原。**注意 `seq` 与 `kind` 保留原值**，不改写。"""
    from ..domain.event import Audience, EventKind, Level, Span

    span_raw = raw.get("span")
    span = None
    if isinstance(span_raw, dict):
        fields = cast("dict[object, object]", span_raw)
        span = Span(
            role=_opt_str(fields.get("role")),
            gate=_opt_str(fields.get("gate")),
            round_no=_opt_int(fields.get("round_no")),
        )
    data = raw.get("data")
    if not isinstance(data, dict):
        data = {}
    payload = {str(k): v for k, v in cast("dict[object, object]", data).items()}
    return Event(
        seq=_req_int(raw, "seq"),
        kind=EventKind(_req_str(raw, "kind")),
        run_id=RunId(_req_str(raw, "run_id")),
        phase=_req_str(raw, "phase"),
        level=Level(_opt_str(raw.get("level")) or "info"),
        audience=Audience(_opt_str(raw.get("audience")) or "user"),
        call_id=_opt_str(raw.get("call_id")) or "",
        span=span,
        data=payload,
    )


def _req_str(raw: dict[str, object], key: str) -> str:
    """取必需字符串字段。**缺字段即报错**，不静默填空——
    日志读不出来时最怕的就是"拿到了一个看起来正常但其实是猜的值"。"""
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"事件字段 {key} 缺失或类型不对：{raw}")
    return value


def _req_int(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int):
        raise ValueError(f"事件字段 {key} 缺失或类型不对：{raw}")
    return value


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) else None
