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

from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from ..domain.event import Event, EventKind, is_user_visible
from ..domain.role import RoleId, display_name


@runtime_checkable
class Projection(Protocol):
    """投影。一个事件进来，一个可显示项出去（或不产生任何东西）。"""

    def accept(self, event: Event) -> None: ...

    def lines(self) -> tuple[str, ...]:
        """已积累的显示行。**纯读，不消费。**"""
        ...


def _as_int(value: object) -> int:
    """把 `data` 里的计数字段安全转成非负整数（D-277）。

    与 `_as_names` 同一个理由：**投影层不能因为发射端写错类型就崩**。
    `data` 的类型是 `dict[str, object]`，而事件会从 journal 读回来
    （JSON 反序列化后 `int` 可能变成别的形态）。

    **负数归零**：token 不可能是负的。宁可少算也不显示一个
    「-1 token」——那会让用户以为程序坏了。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value.strip()))
        except ValueError:
            return 0
    return 0


def _as_names(value: object) -> tuple[str, ...]:
    """把 `data` 里的清单字段安全转成显示用字符串序列。

    `data` 的类型是 `dict[str, object]`，发射端可能给
    `list` / `tuple` / 单个 `str`，也可能是 `None`。
    **投影层不能因为发射端写错类型就崩**——它跑在 TUI 主循环里，
    一崩整个界面就没了。故这里做防御性收敛。
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple)):
        # 显式 cast：BP 无法从 `isinstance(value, (list, tuple))` 收窄元素类型
        out: list[str] = []
        for 元素 in cast("tuple[object, ...]", value):
            文 = str(元素)
            if 文:
                out.append(文)
        return tuple(out)
    return (str(value),)


#: 文件名一行的显示上限。用户要求「不要占太大空间」，
#: 故超过这个数只显示前几个 + 「等 N 份」。
_MAX_文件显示 = 5

#: 单个文件名的显示上限。超长名截断，避免一行刷屏。
_MAX_名单长 = 28


def _扫描摘要(data: dict[str, object]) -> str:
    """把 `RUN_STARTED` 的扫描结果压成**一行**。

    用户原话：
    > 「告诉用户扫描到了几个用户给 AI 的文件，相当于告诉用户它识别到的
    >  而不是不知道」+「不要占太大空间，讲一下识别到了几个文件和文件名即可」

    所以：**一行、限长、有名字**。不列大小不列类型——那是内部信息，
    且会让这行迅速变长，反而看不清"识别到了什么"。

    数量以发射端的 `user_files` 为准，名字列表可能因限长而不全，
    两者不一致时以数量为准（**数量是事实，名字是摘要**）。
    """
    数量 = data.get("user_files")
    名单 = _as_names(data.get("file_names"))
    if not isinstance(数量, int):
        数量 = len(名单)
    if 数量 <= 0:
        return "未发现用户材料"

    示: list[str] = []
    for 名 in 名单[:_MAX_文件显示]:
        文 = str(名)
        示.append(文 if len(文) <= _MAX_名单长 else 文[: _MAX_名单长 - 1] + "…")
    if 数量 > _MAX_文件显示:
        示.append(f"等 {数量} 份")
    return f"已识别用户材料 {数量} 份：" + "、".join(示)


@dataclass(slots=True)
class StandardProjection:
    """标准模式（D-046）。

    只显示：总体进度一行、阶段成果、最终成果、提问。
    **不显示成员状态、不显示审核意见、不显示模型间交互。**
    这三条不是靠"记得别显示"，而是靠白名单——那四类事件
    根本进不了这里（`USER_VISIBLE_KINDS`）。

    角色名用中文（D-080），且**只在这一处**出现角色相关文案。
    """

    scanned: str = ""
    progress: str = ""
    artifacts: tuple[tuple[str, str], ...] = ()
    questions: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    #: 真实 token 用量累计（D-277）。**只加不减**：模型调用只会发生更多次。
    token_入: int = 0
    token_出: int = 0

    def accept(self, event: Event) -> None:
        if not is_user_visible(event.kind):
            return
        data = event.data
        name = str(data.get("name", ""))
        if event.kind is EventKind.RUN_STARTED:
            self.scanned = _扫描摘要(data)
            return
        if event.kind is EventKind.ARTIFACT_READY:
            # D-072：content 装的就是成果全文，界面直接渲染成成果卡。
            #
            # 审计更正：原注释写「只记标题与全文长度——不复制全文」，
            # 但实现存的**确实是全文**（`content` 整份留下来）。
            # 查过 D-072 的机制落点：「`phase.artifact_ready` 携全文」——
            # **全文本就该在事件里**，所以实现是对的，注释是错的。
            #
            # 曾评估是否改为只存长度，结论是不改：
            # - 内存与性能均无实际问题（实测 30 份 × 8000 字常驻，
            #   `lines()` 调用 100 次耗时 0.4 ms）
            # - 而 `lines()` 要输出「（N 字）」，调用方拿不到正文就没法渲染成果卡
            # 故按实现更正注释，行为不动。
            content = str(data.get("content", ""))
            self.artifacts = (*self.artifacts, (name, content))
            self.progress = f"已完成：{name}"
        elif event.kind is EventKind.USER_QUESTION_RAISED:
            fields = str(data.get("fields", ""))
            self.questions = (*self.questions, f"需要确认：{fields}")
        elif event.kind is EventKind.RUN_FAILED:
            # 运行失败**要**让用户知道（白名单第 4 项）。
            # 架构 §5.4：「运行真的失败时，由编排层升级为 `run.failed` 再告知」——
            # 失败信息由 `run.failed` 承载，`error.raised` 只作内部细节留痕。
            #
            # 审计 D-118：原先此处挂的是 `ERROR_RAISED`，
            # 而它不在白名单里，分支**永不可达**，`errors` 因此永远是空的。
            self.errors = (*self.errors, str(data.get("detail", "")))
            self.progress = "运行失败"
        elif event.kind is EventKind.RUN_CANCELLED:
            # **取消不算失败**，故进 `progress` 而不是 `errors`——
            # 塞进 `errors` 会让标准界面显示成「运行失败」，
            # 把用户自己的动作说成系统出错。
            #
            # 带上进度数字：取消是「停在第几段」而不是「什么都没了」，
            # 用户需要知道已完成的成果在哪。
            已完成 = data.get("已完成进度", 0)
            总数 = data.get("总进度", 0)
            self.progress = f"已取消：完成 {已完成}/{总数} 段（已完成的内容保留）"
        elif event.kind is EventKind.EXPORT_BLOCKED:
            # 审计 D-118：原先**没有任何分支处理它**——在白名单里却毫无效果。
            # 后果：导出被残留占位符拦下时，用户看不到任何提示，
            # 门禁变成静默的（比没有门禁更危险）。
            # D-038 要求「列出待补信息」，故这里必须把清单显示出来。
            待补 = _as_names(data.get("placeholders"))
            self.questions = (*self.questions, f"还有待补信息，暂不能导出：{'、'.join(待补)}")
            # **出处单独一行**（D-263）：不挤占上一行的待补清单。
            #
            # 「D3 待填」只说缺什么，不说**去哪个文件的哪一行**——
            # 用户拿到清单仍要一份份翻。行号从 1 起（见 `export._行号`）。
            出处 = _as_names(data.get("原文出处"))
            if 出处:
                self.questions = (*self.questions, f"在这些位置：{'、'.join(出处)}")
            self.progress = "导出被拦：请补齐待补信息"
        elif event.kind is EventKind.EXPORT_DONE:
            # 同为白名单项，此前也无分支。导出成功是用户明确期待的结论，
            # 必须显式告知，且要点名交付了哪些文件。
            文件 = _as_names(data.get("files"))
            self.progress = f"已导出：{'、'.join(文件)}" if 文件 else "已导出"
        elif event.kind is EventKind.MODEL_RESPONSE:
            # D-277：token 用量是**账单**，用户有权知道。
            #
            # **刻意不动 `progress`**：那一行是「总体进度」（D-046），
            # 被每次模型调用覆写的话，进度会一闪一闪跳回去。
            #
            # **只读数字，不碰正文**：事件里带的是
            # `prompt_tokens` / `completion_tokens` 两个整数，
            # 没有文本字段可读——这正是「进白名单但不等于
            # 展示模型间交互」的机械保证。
            self.token_入 += _as_int(data.get("prompt_tokens"))
            self.token_出 += _as_int(data.get("completion_tokens"))
        # 审计 D-118 另删掉一处死分支 `DIRECTION_CONFIRMED`：
        # 它同样不在白名单里，永远进不来。
        # 方向确认单本就是三份阶段成果之一，走 `ARTIFACT_READY` 即可。

    def lines(self) -> tuple[str, ...]:
        """标准模式的全部输出。**顺序稳定**：进度、扫描、用量、提问、成果、错误。"""
        out = [self.progress] if self.progress else []
        out += [f"  {self.scanned}"] if self.scanned else []
        if self.token_入 or self.token_出:
            out.append(
                f"  · token 用量：{self.token_入 + self.token_出}"
                f"（输入 {self.token_入} · 输出 {self.token_出}）"
            )
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
