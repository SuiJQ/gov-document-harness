# 政通 Harness — 第 3 步 L1 事件表合并裁决

> 背景：架构 §5.4 有 29 个事件名，L0 代码只落地 18 个且仅 5 个交集。
> 第 1 步重设计事件体系后未回写架构，导致文档与实现分叉。
> 本文件是合并裁决，**据此统一架构文档与代码**。

## 裁决原则

1. **架构独有的全补**：每个都有明确下游依赖（遥测台账、导出拦截），
   删了后面 L2/L3 实现时必然补回来，等于先删后加。
2. **代码独有的保留**：多数是架构那一版的过度合并，拆开更利于排障。
3. **同一语义取并集，不取一方**：如门禁判定同时保留细粒度与裁决事件。
4. **命名以架构为准**：事件名是日志检索锚点，按架构文档 grep 必须搜得到。

## 合并后事件表（37 项）

| 组 | 事件 | 来源 | 携带 |
|---|---|---|---|
| 运行 | `run.started` | 两边一致 | `tier` / `run_id` |
| | `run.finished` | 两边一致 | — |
| | `run.failed` | **架构补** | 失败原因 |
| | `run.resumed` | 两边一致 | 续跑点 |
| 阶段 | `phase.started` | **代码** | `phase` |
| | `phase.advanced` | **代码** | `phase` |
| | `phase.exited` | **架构补** | `phase` |
| 阶段成果 | `phase.artifact_ready` | 两边一致（名字） | `name`/`kind`/**`content` 全文**/`hash`/`is_gate` |
| | `phase.artifact_updated` | 代码 | 同上 |
| 方向 | `direction.confirmed` | **代码** | 方向确认单已确认 |
| 角色 | `role.state_changed` | **代码** | 六态迁移（D-042） |
| | `role.turn` | **架构补** | `round_no` |
| | `role.tool_call` | 架构补（改名自 `tool.called`） | 工具名 |
| | `role.sleep` | **架构补** | 休眠 |
| | `role.wake` | **架构补** | 唤醒原因 |
| | `role.report` | **架构补** | 汇报内容引用 |
| 工具 | `tool.denied` | **代码** | 路径/权限拒绝 |
| 模型 | `model.request` | **架构补** | `call_id` |
| | `model.response` | **架构补** | token / 耗时 / `finish_reason` |
| | `model.error` | **架构补** | 错误详情 |
| | `model.retry` | **架构补** | 第几次重试（D-027） |
| 检索 | `retrieval.done` | **代码** | 命中数 / 耗时 |
| 门禁 | `gate.started` | 两边一致 | `gate_id` |
| | `gate.passed` | **代码** | 判定 |
| | `gate.failed` | **代码** | 判定 + 证据 |
| | `gate.rejected` | **代码** | 退回（D-049 责任映射） |
| | `gate.rework` | **架构补** | 退回后重新返工 |
| | `gate.escalated` | **架构补** | 升级给主理人 |
| 上下文 | `context.composed` | **架构补** | 占用率（D-039） |
| | `context.compacted` | **架构补** | 保留了哪几类（D-039） |
| 提问 | `user.question_raised` | **代码** | 字段清单（D1–D9），**只记字段码不记答案全文** |
| | `user.answered` | **架构补** | 同上 |
| 产物 | `artifact.published` | **架构补** | `name`/`kind`/`hash` |
| | `artifact.drafted` | **架构补** | `name`/`kind`/`hash` |
| 导出 | `export.blocked` | **架构补** | 被拦截的占位符清单（D-038） |
| | `export.done` | **架构补** | 文件列表 |
| 错误 | `error.raised` | **代码** | `detail` |

## 关键裁决说明

### `tool.called` → `role.tool_call`（采纳架构命名）
工具调用**一定**属于某个角色的某次派发，带 `role_id` 是必然的。
架构的命名 `role.tool_call` 语义更准，代码的 `tool.called` 漏了这层归属。

### `tool.denied` 保留独立（代码独有，采纳）
`tool.denied` 表达**路径/权限守卫拦截**（`WorkspaceGuard`），
这不是某次工具调用的属性，而是安全事件——
谁尝试越权是红线索，必须能独立检索，故不并入 `role.tool_call`。

### 门禁 5 项全留（两边合并）
代码的 `gate.passed/failed/rejected`（细粒度判定）+ 架构的
`gate.rework/escalated`（退回后的动作流）**语义不重叠**：
前者答"判成什么"，后者答"判完之后做了什么"。合并会让 `gate.rework`
无处安放——而"红线问题必须修正后才能交付"（D-038/D-046）正是靠它留痕。

### `user.answered` 补，答案仍不记全文（D-030）
架构明确"只记字段码不记答案全文"，代码也要照办：
用户答复可能含敏感信息，且与 `user.question_raised` 对称。

## 白名单（D-046 机械落点）

`USER_VISIBLE_KINDS` 仍为**四类**（对齐 D-023 用户只需知道四件事）：

- `phase.artifact_ready` —— 三份阶段成果
- `export.blocked` / `export.done` —— 导出结果
- `run.failed` —— 失败告知
- `user.question_raised` —— 需要用户答

代码当前用的是 `error.raised` 替代 `run.failed`，按架构统一为 `run.failed`。
`error.raised` 保留为 INTERNAL（它是实现细节，运行失败才升级为 `run.failed`）。

**不因为"事件变多了"就放宽白名单**：37 个事件里只有 5 个进标准界面，
这正是 D-046 的意义。

## 复核记录

初稿裁决表标题写"34 项"，与代码实际 37 项不符，由 `tests/test_kernel.py`
里的 `assert len(EventKind) == 37` 当场查出。已改正。
**教训同 D-086**：裁决表里的数字也要能机械校验，否则又是一个靠不住的依据。