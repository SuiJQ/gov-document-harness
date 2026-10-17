"""工具注册表。**工具全集的唯一权威**（架构 §7.4，D-042）。

## 已砍除且不允许复活的（D-042）

`fs.list` / `fs.write` / `fs.delete` 及公共区外的一切文件读写。

砍掉之后路径校验只需回答一个问题——**「能不能碰 `user/`」**，
不再需要拦截跨成员越界。
**这不是简化，是把复杂度消灭在设计里**（架构原话）。

本文件是**数据清单**：14 个工具的名字、描述、参数、角色白名单、副作用
都在这里登记。`handlers.py` 负责实现，`registry.py` 负责按角色给出可调子集。
三件事分开，是为了让「模型能看到什么」不需要读实现就知道。

**清单数量是硬约束**：架构 §7.4 定的就是 14 个，
多一个少一个都是「实现与架构不一致」。
故 `test_工具恰为十四个` 把它钉死。
"""

from __future__ import annotations

from typing import Final

from ...domain.role import RoleId
from .spec import Effect, ToolDomain, ToolSpec, _成员

# ── 公共区 ──

SHARED_WRITE = ToolSpec(
    name="shared.write",
    description="把内容写为公共区产物，供全体成员按需读取。name 与 kind 由主理人指定。",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "产物名称"},
            "kind": {
                "type": "string",
                "enum": ["outline", "draft", "gate-report", "main-document", "attachment", "note"],
                "description": "产物种类",
            },
            "content": {"type": "string", "description": "产物内容"},
        },
        "required": ["name", "kind", "content"],
    },
    effect=Effect.PUBLISH,
    requires_publish=True,
    域=ToolDomain.公共交换,
)

SHARED_READ = ToolSpec(
    name="shared.read",
    description="按名称读取公共区中已发布的产物。",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "产物名称"}},
        "required": ["name"],
    },
    域=ToolDomain.公共交换,
)

SHARED_LIST = ToolSpec(
    name="shared.list",
    description="列出公共区中某一类别的产物名称。",
    parameters={
        "type": "object",
        "properties": {"kind": {"type": "string", "description": "产物种类"}},
        "required": ["kind"],
    },
    域=ToolDomain.公共交换,
)

# ── 知识库 ──

KB_SEARCH = ToolSpec(
    name="kb.search",
    description="检索内容包与用户材料。scope 限 kb（内容包）或 user（用户材料）。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词"},
            "scope": {"type": "string", "enum": ["kb", "user"], "description": "检索范围"},
        },
        "required": ["query", "scope"],
    },
    域=ToolDomain.知识检索,
)

KB_READ_PACK = ToolSpec(
    name="kb.read_pack",
    description="读取内容包中的一条内容（如门禁细则、红线条目）。",
    parameters={
        "type": "object",
        "properties": {"item": {"type": "string", "description": "内容包条目标识"}},
        "required": ["item"],
    },
    域=ToolDomain.知识检索,
)

KB_READ_USER_FILE = ToolSpec(
    name="kb.read_user_file",
    description="按需读取一个用户材料的正文。超长按 2 万字截断并标明。",
    parameters={
        "type": "object",
        "properties": {"filename": {"type": "string", "description": "用户材料文件名"}},
        "required": ["filename"],
    },
    域=ToolDomain.知识检索,
)

# ── 汇报 ──

MSG_REPORT_TO_LEAD = ToolSpec(
    name="msg.report_to_lead",
    description="向主理人汇报。这是成员唯一的对外汇报通道（D-041）。",
    parameters={
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "汇报摘要"},
            "payload": {"type": "object", "description": "结构化内容"},
        },
        "required": ["summary"],
    },
    effect=Effect.SIGNAL,
    allowed_roles=_成员,
    域=ToolDomain.协作通信,
)

MSG_REQUEST_FROM_LEAD = ToolSpec(
    name="msg.request_from_lead",
    description="向主理人请示。blocking=true 表示等待用户答复，期间不再推进。",
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "请示内容"},
            "blocking": {"type": "boolean", "description": "是否阻塞等待"},
        },
        "required": ["question"],
    },
    effect=Effect.SIGNAL,
    allowed_roles=_成员,
    域=ToolDomain.协作通信,
)

# ── 草稿 ──

DRAFT_SUBMIT = ToolSpec(
    name="draft.submit",
    description="把正文提交到草稿纸。每次任务每人最多 3 个文件，超出自动清最早的（D-042）。",
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "稿件内容"},
            "version": {"type": "integer", "description": "版本号"},
        },
        "required": ["content", "version"],
    },
    effect=Effect.PUBLISH,
    requires_publish=True,
    域=ToolDomain.产出交付,
)

# ── 校验 ──

VALIDATE_RUN = ToolSpec(
    name="validate.run",
    description="运行一个校验器（如门禁校验、占位符扫描）。",
    parameters={
        "type": "object",
        "properties": {
            "checker": {"type": "string", "description": "校验器标识"},
            "args": {"type": "object", "description": "校验参数"},
        },
        "required": ["checker"],
    },
    effect=Effect.PURE,
    域=ToolDomain.门禁校验,
)

# ── 流程（仅主理人部分）──

FLOW_STATUS = ToolSpec(
    name="flow.status",
    description="查询各成员当前状态。仅主理人可用；数据仅供主理人与调试模式，不进标准界面（D-046）。",
    parameters={"type": "object", "properties": {}},
    allowed_roles=frozenset({RoleId.TEAM_LEAD}),
    effect=Effect.READ,
    域=ToolDomain.运行控制,
)

FLOW_SLEEP = ToolSpec(
    name="flow.sleep",
    description="主动挂起当前角色，等待条件命中。挂起期间不再发任何模型请求。",
    parameters={
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "挂起原因"}},
        "required": ["reason"],
    },
    effect=Effect.SUSPEND,
    域=ToolDomain.运行控制,
)

FLOW_WAKE_ON = ToolSpec(
    name="flow.wake_on",
    description="设定唤醒条件，命中后自动回到运行中。",
    parameters={
        "type": "object",
        "properties": {
            "event": {"type": "string", "description": "事件类型"},
            "role": {"type": "string", "description": "目标角色"},
        },
        "required": ["event"],
    },
    effect=Effect.SUSPEND,
    域=ToolDomain.运行控制,
)

FLOW_ASK_USER = ToolSpec(
    name="flow.ask_user",
    description="向用户提问，弹结构化表单（D-040）。仅主理人可用。",
    parameters={
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {"type": "object"},
                "description": "待填字段定义",
            }
        },
        "required": ["fields"],
    },
    allowed_roles=frozenset({RoleId.TEAM_LEAD}),
    effect=Effect.ASK_USER,
    域=ToolDomain.方向提问,
)

FLOW_ROLLBACK = ToolSpec(
    name="flow.rollback",
    description="退回重做某一步，保留已确认的 D-Card（D-050）。仅主理人可用。",
    parameters={
        "type": "object",
        "properties": {
            "step": {"type": "string", "description": "要重做的步骤"},
            "keep": {"type": "boolean", "description": "是否保留已确认内容"},
        },
        "required": ["step"],
    },
    allowed_roles=frozenset({RoleId.TEAM_LEAD}),
    effect=Effect.REWORK,
    域=ToolDomain.运行控制,
)

FLOW_INJECT_DIRECTIVE = ToolSpec(
    name="flow.inject_directive",
    description="把用户插话转达为 user_directive。仅主理人可用。",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "指令文本"}},
        "required": ["text"],
    },
    allowed_roles=frozenset({RoleId.TEAM_LEAD}),
    effect=Effect.SIGNAL,
    域=ToolDomain.运行控制,
)

#: **工具全集**。顺序固定，便于测试逐个比对，也便于打印给人看。
#: 第 14.5 步新增（D-226）：对话序列只记工具**动作**不记结果，
#: 模型需要一条查回途径，否则要么撑爆上下文、要么重复查询。
TOOL_RESULT = ToolSpec(
    name="tool.result",
    description=(
        "取回某次工具调用的完整返回。call_id 见历史里的工具调用动作。"
        "对话里只记动作不记结果，需要原文时用本工具查回。"
    ),
    parameters={
        "type": "object",
        "properties": {"call_id": {"type": "string", "description": "那次调用的 id"}},
        "required": ["call_id"],
    },
    allowed_roles=frozenset(RoleId),
    effect=Effect.READ,
    域=ToolDomain.运行控制,
)

#: 门禁取证工具（GATING 阶段专用）。
#: 将「自由文本输出格式」改为「结构化工具调用」，彻底避免模型不遵从格式。
GATE_REPORT = ToolSpec(
    name="gate.report",
    description="报告一道门禁的取证结论。本轮只能调用本工具，不要输出自由文本。",
    parameters={
        "type": "object",
        "properties": {
            "gate_id": {"type": "string", "description": "门禁编号，如 G1"},
            "conclusion": {"type": "string", "enum": ["通过", "退回"], "description": "结论"},
            "evidence": {"type": "string", "description": "可定位的正文原句或位置"},
            "reason": {"type": "string", "description": "退回原因（仅退回时填写）"},
        },
        "required": ["gate_id", "conclusion", "evidence"],
    },
    effect=Effect.PURE,
    域=ToolDomain.门禁校验,
)

TOOL_SPECS: Final[tuple[ToolSpec, ...]] = (
    SHARED_WRITE,
    SHARED_READ,
    SHARED_LIST,
    KB_SEARCH,
    KB_READ_PACK,
    KB_READ_USER_FILE,
    MSG_REPORT_TO_LEAD,
    MSG_REQUEST_FROM_LEAD,
    DRAFT_SUBMIT,
    VALIDATE_RUN,
    FLOW_STATUS,
    FLOW_SLEEP,
    FLOW_WAKE_ON,
    FLOW_ASK_USER,
    FLOW_ROLLBACK,
    FLOW_INJECT_DIRECTIVE,
    TOOL_RESULT,
    GATE_REPORT,
)

_SPEC_BY_NAME: Final[dict[str, ToolSpec]] = {}
for _spec in TOOL_SPECS:
    if _spec.name in _SPEC_BY_NAME:
        raise ValueError(f"工具名重复：{_spec.name}")
    _SPEC_BY_NAME[_spec.name] = _spec


def 取工具(name: str) -> ToolSpec | None:
    """按名取工具声明。未知名字返回 `None`（调用方负责拒绝）。"""
    return _SPEC_BY_NAME.get(name)


def 该角色可调(role: RoleId) -> tuple[ToolSpec, ...]:
    """某角色实际可调的工具子集。**这是「双重白名单」的第一重**。"""
    return tuple(s for s in TOOL_SPECS if role in s.allowed_roles)
