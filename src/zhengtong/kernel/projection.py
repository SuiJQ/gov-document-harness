"""投影层（架构 §5.2）。

**过滤发生在投影端，不在发射端**——这是本架构的一条硬决定。

反例（必须避免）：在每个 `logger.info()` 调用点判断"现在是不是调试模式"。
那样一定会漏，因为总有人忘记判断，而判断逻辑会散落在几百个调用点。
边缘过滤保证**只有一处**决定"给不给用户看"，且这处的规则是一张表。

两个投影：

- `StandardProjection`：只放行 `USER_VISIBLE_KINDS` 四类（D-023/D-046）
- `DebugProjection`：全量透传，且带时间戳与阶段标注（D-023 "有啥输出啥"）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..domain.event import Event, EventKind, is_user_visible
from ..domain.role import RoleId, display_name


@runtime_checkable
class Projection(Protocol):
    """投影。一个事件进来，一个可显示项出去（或不产生任何东西）。"""

    def accept(self, event: Event) -> None: ...

    def lines(self) -> tuple[str, ...]:
        """已积累的显示行。**纯读，不消费。**"""
        ...


@dataclass(slots=True)
class StandardProjection:
    """标准模式（D-046）。

    只显示：总体进度一行、阶段成果、最终成果、提问。
    **不显示成员状态、不显示审核意见、不显示模型间交互。**
    这三条不是靠"记得别显示"，而是靠白名单——那四类事件
    根本进不了这里（`USER_VISIBLE_KINDS`）。

    角色名用中文（D-080），且**只在这一处**出现角色相关文案。
    """

    progress: str = ""
    artifacts: tuple[tuple[str, str], ...] = ()
    questions: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    _seen: int = field(default=0, repr=False)

    def accept(self, event: Event) -> None:
        if not is_user_visible(event.kind):
            return
        self._seen += 1
        data = event.data
        name = str(data.get("name", ""))
        if event.kind is EventKind.ARTIFACT_READY:
            # D-072：content 装的是成果全文，界面直接渲染成成果卡。
            # 这里只记标题与全文长度——**不复制全文**，
            # 因为全文已经落盘且在事件流里，复制一份等于让内存翻倍。
            content = str(data.get("content", ""))
            self.artifacts = (*self.artifacts, (name, content))
            self.progress = f"已完成：{name}"
        elif event.kind is EventKind.USER_QUESTION_RAISED:
            fields = str(data.get("fields", ""))
            self.questions = (*self.questions, f"需要确认：{fields}")
        elif event.kind is EventKind.DIRECTION_CONFIRMED:
            self.progress = "方向已确认"
        elif event.kind is EventKind.ERROR_RAISED:
            self.errors = (*self.errors, str(data.get("detail", "")))

    def lines(self) -> tuple[str, ...]:
        """标准模式的全部输出。**顺序稳定**：进度、提问、成果、错误。"""
        out = [self.progress] if self.progress else []
        out += [f"  · {q}" for q in self.questions]
        out += [f"  ▣ {n}（{len(c)} 字）" for n, c in self.artifacts]
        out += [f"  ✗ {e}" for e in self.errors]
        return tuple(out)


@dataclass(slots=True)
class DebugProjection:
    """调试模式（D-023 "有啥输出啥"）。

    **全量透传**：一条都不过滤。成员状态、工具调用、门禁明细全都要。
    角色名在这里也用中文（D-080），但附上内部标识便于定位文件
    （D-080 明确允许调试模式用内部标识）。
    """

    prefix: str = "[调试]"
    records: tuple[str, ...] = ()

    def accept(self, event: Event) -> None:
        line = self._format(event)
        self.records = (*self.records, line)

    def _format(self, event: Event) -> str:
        parts = [f"#{event.seq}", event.kind.value, f"phase={event.phase}"]
        if event.span is not None and event.span.role is not None:
            # 调试模式允许用内部标识定位（D-080）
            try:
                role_id = RoleId(event.span.role)
                parts.append(f"角色={display_name(role_id)}({role_id.value})")
            except ValueError:
                parts.append(f"角色={event.span.role}")
        if event.span is not None and event.span.gate:
            parts.append(f"门禁={event.span.gate}")
        if event.call_id:
            parts.append(f"call={event.call_id}")
        if event.data:
            parts.append(f"data={event.data}")
        return f"{self.prefix} " + " ".join(parts)

    def lines(self) -> tuple[str, ...]:
        return self.records
