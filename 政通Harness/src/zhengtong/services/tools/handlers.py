"""16 个工具的实现（架构 §7.4，D-042）。

## 三条贯穿全文件的纪律

**一、工具层不碰路径拼接。**
公共区与草稿纸的写入一律走 `StoragePort.put_artifact(ref, content)`，
路径由 `ArtifactRef` 携带、存储层解析。
理由是 D-076 定的「路径解析的唯一实现」——
工具层一旦自己拼路径，就多了一个不受 `WorkspaceGuard` 管的地方。

**二、编排依赖的工具走注入口，不在这里实现。**
`flow.*` 与 `msg.*` 要读写编排状态（六态、D-Card、用户答复），
而那些是 L5（第 13 步）的东西。故此处只定义**回调签名**并转发，
不实现状态机。**宁可留一个会炸的入口，也不要在这里造第二套状态。**

**三、草稿纸计数由调用方负责。**
`StoragePort.draft_paper_ok(role, current_count)` 的注释已定：
计数属编排层，存储层只判上限。工具层原样转发 `current_count`。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, cast

from ...domain.artifact import ArtifactKind, ArtifactRef
from ...domain.errors import ErrorCode, Result
from ...domain.event import EventKind
from ...domain.ids import ArtifactId, RunId
from ...domain.role import RoleId
from ...ports.content_pack import ContentPackPort
from ...ports.retriever import RetrieverPort
from ...ports.storage import StoragePort
from ...ports.user_files import UserFilesPort
from ...ports.validators import ValidatorPort
from .calllog import 调用存档 as 调用存档类

# ── 编排回调（由第 13 步 L5 注入；此处只定义签名）──

#: 向主理人汇报。返回主理人的答复文本。
汇报回调 = Callable[[str, dict[str, object]], str]
#: 请求主理人裁决。返回裁决文本。
请示回调 = Callable[[str, bool], str]
#: 主动挂起。
挂起回调 = Callable[[str], None]
#: 设定唤醒条件。
唤醒回调 = Callable[[str, str], None]
#: 向用户提问（仅主理人）。返回用户答复。
问用户回调 = Callable[[tuple[dict[str, object], ...]], str]
#: 退回重做（仅主理人）。
回滚回调 = Callable[[str, bool], None]
#: 转达用户插话（仅主理人）。
转达回调 = Callable[[str], None]
#: 查询成员状态（仅主理人，D-046：数据不进标准界面）。
状态回调 = Callable[[], dict[str, str]]


#: 工具层发事件用的回调签名（与 `EventBus.emit` 兼容）。
#:
#: **不传 `Event` 对象**：工具层在 L4、编排层在 L5，
#: 低层不该构造高层的值对象。故只发 `(kind, **载荷)`，
#: 由编排层组装成 `Event`。未接线时静默跳过（D-192）。
记事件回调 = Callable[..., None]


@dataclass(slots=True)
class 工具依赖:
    """工具层需要的全部外部依赖。**全部注入，工具层不 new 任何东西。**"""

    storage: StoragePort
    user_files: UserFilesPort
    retriever: RetrieverPort
    content_pack: ContentPackPort
    validators: ValidatorPort
    run_id: RunId
    # ── 编排回调（第 13 步接；未接的走 D-192「入口未接线」）──
    汇报: 汇报回调 | None = None
    请示: 请示回调 | None = None
    挂起: 挂起回调 | None = None
    唤醒: 唤醒回调 | None = None
    问用户: 问用户回调 | None = None
    回滚: 回滚回调 | None = None
    转达: 转达回调 | None = None
    状态: 状态回调 | None = None
    #: 各角色当前草稿纸文件数。**由编排层记账**（D-042，见 `draft_paper_ok` 注释）。
    草稿计数: dict[RoleId, int] = field(default_factory=dict)
    #: 工具调用存档（第 14.5 步）：`tool.result` 据此查回。
    #: **只在内存**——落盘会把一个查回的便利变成产物生命周期管理问题。
    调用存档: 调用存档类 | None = None
    #: **发 `role.tool_call` / `tool.denied` 事件**（D-266）。
    #:
    #: 架构 §7.4 要求「每次调用三重检查 + 一条审计记录，全部进 journal」，
    #: 而事件枚举 `ROLE_TOOL_CALL`/`TOOL_DENIED` 早已定义、注释也写清了语义，
    #: **却从未有过发射点**——审计条目只留在内存，进程一结束即消失。
    #:
    #: 签名与编排层 `EventBus.emit` 一致：`(kind, phase, detail=...)`。
    #: 工具层不该知道 Event 的完整结构，故只发最简载荷。
    记事件: 记事件回调 | None = None


def _未接线(工具名: str, 缺哪个: str) -> Result[str]:
    """编排回调未接线时的失败。**必须失败，不能静默成功**。

    静默成功是 D-165 的形态：零件都测过、测试全绿，
    但真跑起来这一条其实什么也没做。

    ## 为什么用 `TOOL_NOT_WIRED` 而不是 `TOOL_NOT_ALLOWED`（D-406）

    原先返回 E065「角色不在该工具白名单内」。实测三者：
    `STRUCTURE` 调用 `msg.report_to_lead` 时白名单**通过**，
    却仍报 E065——排障会一路查到角色配置去，而白名单没问题。

    「拼错名字 / 越权 / 缺回调」必须一眼可分。撞码的代价是
    每次都得读完调用链才知道真因，而这恰好是最难查的那类。
    """
    return Result.fail(ErrorCode.TOOL_NOT_WIRED, f"{工具名} 尚未接线（缺 {缺哪个}，第 13 步注入）")


# ── 公共区 ──


def shared_write(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    名称 = str(p.get("name", "")).strip()
    种类 = str(p.get("kind", ""))
    内容 = str(p.get("content", ""))
    if not 名称 or not 种类 or not 内容:
        return Result.fail(
            ErrorCode.REQUIREMENT_EMPTY, "shared.write 需要 name、kind、content 三项"
        )
    try:
        kind = ArtifactKind(种类)
    except ValueError:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, f"未知的产物种类：{种类}")
    # 路径由 ArtifactRef 携带、存储层解析——工具层不拼路径（D-076）。
    # **必须带 run 目录**：`put_artifact` 只补 `data/` 前缀，
    # 不补 `<run-id>/`，故若这里不带上，两次运行会共用
    # `data/公共区/` 互相覆盖，违反「一次任务一个 data/<run-id>/ 工作目录」。
    ref = ArtifactRef(
        id=ArtifactId(f"{kind.value}-{名称}"),
        kind=kind,
        path=f"{d.run_id}/公共区/{名称}.{kind.value}",
    )
    写 = d.storage.put_artifact(ref, 内容)
    if 写.is_error:
        return Result.fail(
            写.error.code if 写.error else ErrorCode.STORAGE_WRITE_FAILED, "写公共区失败"
        )
    return Result.ok(f"已写入公共区：{名称}")


def shared_read(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    名称 = str(p.get("name", "")).strip()
    if not 名称:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "shared.read 需要 name")
    条目 = d.storage.list_dir(_公共区路径(d))
    # **必须用实际找到的完整条目名去读，不能用传入的名称。**
    # 落盘名带 `.kind` 后缀（`大纲.outline`），
    # 第一版拿传入的 `大纲` 去拼路径 → 写得进、读不出来。
    命中名 = next((n for n in 条目 if n.split(".", 1)[0] == 名称), None)
    if 命中名 is None:
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"公共区没有名为 {名称} 的产物")
    内容 = d.storage.read_text(f"{_公共区路径(d)}/{命中名}")
    if 内容.is_error:
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"读取公共区产物 {名称} 失败")
    return Result.ok(f"{名称}：\n{内容.unwrap()}")


def shared_list(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    种类 = str(p.get("kind", "")).strip()
    条目 = d.storage.list_dir(_公共区路径(d))
    if 种类:
        条目 = tuple(n for n in 条目 if n.endswith(f".{种类}"))
    if not 条目:
        return Result.ok("公共区暂无此类产物")
    return Result.ok("公共区产物：\n" + "\n".join(f"- {n}" for n in sorted(条目)))


def _公共区路径(d: 工具依赖) -> str:
    """公共区的**存储层相对路径**，带 `data/` 前缀。

    ## 为什么写路径和读路径的前缀不一样

    - 写：`put_artifact(ref, ...)` 内部会补 `data/`，故 `ref.path` 只带 `<run-id>/公共区/...`
    - 读：`list_dir` / `read_text` **直接收相对路径，不补前缀**，
      故必须自己写全 `data/<run-id>/公共区`

    这处不对称很容易写错（写对读错、或反之）——
    第一版就漏了 `data/`，症状是「写得进去、读不出来」，
    由 `test_shared_write与read与list` 抓出。
    """
    return f"data/{d.run_id}/公共区"


# ── 知识库 ──


def kb_search(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    查询 = str(p.get("query", "")).strip()
    范围 = str(p.get("scope", "kb"))
    if not 查询:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "kb.search 需要 query")
    if 范围 not in ("kb", "user"):
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, f"scope 只能是 kb 或 user，实得 {范围}")
    命中 = d.retriever.search(查询)
    if 命中.is_error:
        return Result.fail(ErrorCode.RETRIEVAL_FAILED, f"检索「{查询}」失败")
    结果 = 命中.unwrap()
    if not 结果:
        return Result.ok(f"未找到与「{查询}」相关的内容")
    # **带相关度档位**（D-259）：此前只给 `ref` 与 `snippet`，
    # 模型看不出哪条值得读、哪条是碰巧命中——于是只能全读，
    # 或者读完也判断不出该不该继续检索。
    最高 = max(h.score for h in 结果)
    条目 = "\n".join(f"- [{_相关档(h.score, 最高)}] {h.ref}：{h.snippet}" for h in 结果)
    # **检索完成留痕**（D-408）：发 `retrieval.done`。
    #
    # 检索是模型**主动**发起的行为，次数与查询词全在模型那边；
    # 不发事件的话，journal 里只有「工具被调用过」的记录，
    # 无法复原「这一稿里到底查了什么、查了几次」——
    # 而那正是判断依据是否够用的唯一凭据（D-069）。
    if d.记事件 is not None:
        d.记事件(
            "retrieval.done",
            查询=查询[:60],
            范围=范围,
            命中数=len(结果),
            最高相关度=round(最高, 4) if 结果 else 0.0,
        )
    return Result.ok(f"检索「{查询}」命中 {len(结果)} 条（按相关度降序）：\n{条目}")


#: 相关度档位的分界，**相对于本次最高分**（D-259）。
#:
#: **给档位不给原始分数**，两个理由：
#:
#: 1. BM25 分数**依赖语料规模与文档分布**，跨查询不可比。
#:    把 0.8 与 12.0 直接摆给模型，它会当成「0.8 基本无关」。
#: 2. 原始浮点会诱发模型去算阈值、做加减——那是在一个
#:    **没有标定过的量纲**上做推理（D-031「不得编造数字」的同类）。
#:
#: 相对最高分则稳定：无论语料多大，最相关的那条永远是「强」。
#:
#: **这两条分界是初值，需真实运行后校准**（同 D-031 对耗时预估的态度：
#: 先有个能跑的，再据实测调，不要凭空断言哪条命中算强）。
_强档线 = 0.6
_中档线 = 0.25


def _相关档(分: float, 最高: float) -> str:
    """把分数映射成 `强` / `中` / `弱`。

    **最高分自己恒为「强」**，哪怕它很低——那说明本次查询整体匹配都很差，
    不是「这条也不强」。整体不匹配属「没检索到」，由空结果分支管。
    """
    if 最高 <= 0 or 分 >= 最高 * _强档线:
        return "强"
    if 分 >= 最高 * _中档线:
        return "中"
    return "弱"


def kb_read_pack(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    条目 = str(p.get("item", "")).strip()
    if not 条目:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "kb.read_pack 需要 item")
    return d.content_pack.load_reference(条目)


def kb_read_user_file(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    文件名 = str(p.get("filename", "")).strip()
    if not 文件名:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "kb.read_user_file 需要 filename")
    结果 = d.user_files.read_text(文件名)
    if 结果.is_error:
        # 不能直接 `return 结果`——那是 `Result[读到的文件]`，
        # 与本函数的 `Result[str]` 不兼容（类型检查器当场报出）。
        错 = 结果.error
        assert 错 is not None
        return Result.fail(错.code, 错.detail)
    文件 = 结果.unwrap()
    # D-053 的两条：截断 + **标明**。只给内容不给标记，
    # 模型会把残缺材料当完整材料用（D-007 严禁编造的反例）。
    正文 = 文件.text
    if 文件.已截断:
        正文 = f"【已截断：原 {文件.原始字数} 字，此处为前 {文件.完整字数} 字】\n{正文}"
    return Result.ok(正文)


# ── 汇报 ──


def msg_report_to_lead(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    摘要 = str(p.get("summary", "")).strip()
    if not 摘要:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "msg.report_to_lead 需要 summary")
    if d.汇报 is None:
        return _未接线("msg.report_to_lead", "汇报回调")
    载荷 = p.get("payload")
    return Result.ok(d.汇报(摘要, 载荷 if isinstance(载荷, dict) else {}))


def msg_request_from_lead(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    问题 = str(p.get("question", "")).strip()
    if not 问题:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "msg.request_from_lead 需要 question")
    if d.请示 is None:
        return _未接线("msg.request_from_lead", "请示回调")
    return Result.ok(d.请示(问题, bool(p.get("blocking", False))))


# ── 草稿 ──


def draft_submit(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:
    内容 = str(p.get("content", ""))
    if not 内容:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "draft.submit 需要 content")
    原始版本 = p.get("version", 1)
    版本 = 原始版本 if isinstance(原始版本, int) else 1
    当前 = d.草稿计数.get(role, 0)
    if not d.storage.draft_paper_ok(role, 当前):
        return Result.fail(
            ErrorCode.STORAGE_WRITE_FAILED,
            "草稿纸已满（D-042 上限 3 个），请先清理最早的",
        )
    ref = ArtifactRef(
        id=ArtifactId(f"草稿-{role.value}-{版本}"),
        kind=ArtifactKind.DRAFT,
        path=f"{d.run_id}/草稿纸/{role.value}/v{版本}",
    )
    写 = d.storage.put_artifact(ref, 内容)
    if 写.is_error:
        错 = 写.error
        assert 错 is not None
        return Result.fail(错.code, 错.detail)
    d.草稿计数[role] = 当前 + 1
    return Result.ok(f"已提交草稿 v{版本}（本人当前 {d.草稿计数[role]} 个，上限 3）")


# ── 校验 ──


def validate_run(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    校验器 = str(p.get("checker", "")).strip()
    if not 校验器:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "validate.run 需要 checker")
    载荷 = p.get("args")
    参数 = 载荷 if isinstance(载荷, dict) else {}
    if 校验器 == "hard_constraint":
        结果 = d.validators.run_hard_constraint(参数)
    elif 校验器 == "structure":
        结果 = d.validators.run_structure(参数)
    else:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, f"未知的校验器：{校验器}")
    if 结果.is_error:
        错 = 结果.error
        assert 错 is not None
        return Result.fail(错.code, 错.detail)
    详情 = 结果.unwrap()
    阻断 = 详情.blocking  # 属性，不是方法（写错会在这里炸 TypeError）
    if 阻断:
        # **带上规则号与锚点**（D-260、D-271）。此前只给 `i.message`，
        # 于是模型拿到「G10 无取证证据」却不知道是 D-034 哪一条、
        # 证据该挂在哪儿——而它正是要去补证据的那个角色。
        # 格式与用户侧出口共用 `ValidationIssue.定位`，不许各写一份。
        return Result.ok("校验未通过：\n" + "\n".join(f"- {i.定位}" for i in 阻断))
    return Result.ok("校验通过")


# ── 流程 ──


def flow_status(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    if d.状态 is None:
        return _未接线("flow.status", "状态回调")
    状态 = d.状态()
    return Result.ok("；".join(f"{k}={v}" for k, v in sorted(状态.items())))


def flow_sleep(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    原因 = str(p.get("reason", "")).strip()
    if not 原因:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "flow.sleep 需要 reason")
    if d.挂起 is None:
        return _未接线("flow.sleep", "挂起回调")
    d.挂起(原因)
    return Result.ok(f"已挂起：{原因}")


def flow_wake_on(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:
    事件 = str(p.get("event", "")).strip()
    if not 事件:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "flow.wake_on 需要 event")
    if d.唤醒 is None:
        return _未接线("flow.wake_on", "唤醒回调")
    d.唤醒(事件, str(p.get("role", role.value)))
    return Result.ok(f"已设定唤醒条件：{事件}")


def flow_ask_user(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    if d.问用户 is None:
        return _未接线("flow.ask_user", "问用户回调")
    字段 = _取字段(p.get("fields"))
    答复 = d.问用户(字段)
    # **`user.answered`：问答闭环的另一半**（架构 §5.4）。
    #
    # 此前事件流里只有 `user.question_raised`（工具层发），
    # **答复侧零留痕**——于是 journal 无法回答
    # 「这次提问到底有没有得到答复」「流程是不是卡在提问上了」。
    # 而这两种情况的现场症状都是「程序停住不动」，排障只能靠猜。
    #
    # **只记字段码，不记答案全文**（D-030：答案可能含用户不愿外传的实质）。
    #
    # 载荷里没有「拒答」：闸门超时/界面崩时 `问用户` **抛异常**，
    # 那条路径到不了这里。所以这里记的是「答复拿到了」这一事实，
    # 「没拿到」的那一侧由 journal 里 `question_raised` 后无 `answered`
    # 本身表达——**闭环缺口就是信号**，不需要另设一个字段。
    if d.记事件 is not None:
        d.记事件(
            EventKind.USER_ANSWERED,
            fields=",".join(str(f.get("code", "")) for f in 字段),
        )
    return Result.ok(答复)


def _取字段(原: object) -> tuple[dict[str, object], ...]:
    """把 `fields` 参数规整成字段元组。

    ## 此前写的是 `isinstance(原, tuple)`——**恒为假**（D-286）

    工具参数来自 `json.loads(原始参数)`（`http_llm.py:220`），
    **JSON 数组解析出来是 `list`，永远不可能是 `tuple`**。

    于是那一行等价于「无论传什么都传空元组」：
    模型问了一个问题，收到的却是**空问题的答案**。

    **今天没发作**，因为 `问用户` 回调在生产代码里零接线，
    每次都在这一行之前就 `_未接线(...)` 返回了。
    **一旦把回调接上，这个缺陷立刻激活。**

    > 这是本项目反复出现的形态：**被前置的失败挡住、因而从未执行的代码，
    > 里面藏着第二个缺陷。**接缝不接，下面的一切都验不到。

    ## 为什么要逐项 `isinstance(字段, dict)`

    字段来自**模型**，是敌意输入面。`["a", "b"]`、字符串、嵌套数组都可能出现。
    逐项过滤后丢掉坏的，好过把整个畸形结构塞进回调里——
    那会让「模型问了什么」和「用户答了什么」彻底对不上号。
    """
    # **两种都收**：真实模型来的参数经 `json.loads` 是 `list`（D-286 的由来），
    # 而程序内直接调用可能传 `tuple`（既有判据就是这么写的）。
    # 只认一种会逼另一条路径静默降级成空问题——那正是要修的毛病。
    if not isinstance(原, (list, tuple)):
        return ()
    return tuple(项 for 项 in cast("tuple[object, ...]", 原) if isinstance(项, dict))


def flow_rollback(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    步骤 = str(p.get("step", "")).strip()
    if not 步骤:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "flow.rollback 需要 step")
    if d.回滚 is None:
        return _未接线("flow.rollback", "回滚回调")
    d.回滚(步骤, bool(p.get("keep", True)))
    return Result.ok(f"已退回重做：{步骤}")


def flow_inject_directive(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    文本 = str(p.get("text", "")).strip()
    if not 文本:
        return Result.fail(ErrorCode.REQUIREMENT_EMPTY, "flow.inject_directive 需要 text")
    if d.转达 is None:
        return _未接线("flow.inject_directive", "转达回调")
    d.转达(文本)
    return Result.ok("已转达用户插话")


def tool_result(d: 工具依赖, role: RoleId, p: dict[str, object]) -> Result[str]:  # noqa: ARG001  # role 是统一签名的一部分，删了会让处理器表类型不一致
    """取回某次工具调用的完整返回（第 14.5 步）。

    对话序列里**只记动作不记结果**，模型需要一条查回途径。
    查不到就说查不到——**不得返回空串**：那会让模型以为
    「我查过了，确实没有」，于是不再重查（编造证据的另一种形态）。
    """
    call_id = str(p.get("call_id", "")).strip()
    if not call_id:
        return Result.fail(ErrorCode.TOOL_UNKNOWN, "tool.result 需要 call_id")
    if d.调用存档 is None:
        return _未接线("tool.result", "调用存档")
    结果 = d.调用存档.取(call_id)
    if 结果 is None:
        return Result.fail(ErrorCode.TOOL_UNKNOWN, f"没有 call_id={call_id} 的调用结果")
    return Result.ok(结果)


#: 工具名 → 实现。**注册表与实现必须一一对应**，由测试守住。
处理器表: Final[dict[str, Callable[[工具依赖, RoleId, dict[str, object]], Result[str]]]] = {
    "shared.write": shared_write,
    "shared.read": shared_read,
    "shared.list": shared_list,
    "kb.search": kb_search,
    "kb.read_pack": kb_read_pack,
    "kb.read_user_file": kb_read_user_file,
    "msg.report_to_lead": msg_report_to_lead,
    "msg.request_from_lead": msg_request_from_lead,
    "draft.submit": draft_submit,
    "validate.run": validate_run,
    "flow.status": flow_status,
    "flow.sleep": flow_sleep,
    "flow.wake_on": flow_wake_on,
    "flow.ask_user": flow_ask_user,
    "flow.rollback": flow_rollback,
    "flow.inject_directive": flow_inject_directive,
    "tool.result": tool_result,
}
