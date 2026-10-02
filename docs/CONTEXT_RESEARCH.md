# 上下文管理调研笔记（2026-10-02）

**用途**：为第 9 步（上下文管理，D-051）提供外部参考。
**性质**：调研笔记，**不是决策**。落地前须回 DECISIONS.md 立项。

---

## 一、OpenAI 那篇文章：未取得（如实记录）

用户提到「OpenAI 最初开发 Codex 用了 5 个月，产出了一篇文章」。
**本次未能找到或读到该文原文**，不凭印象转述其内容。

尝试与失败原因（留档，避免下次重复踩）：

| 途径 | 结果 |
|---|---|
| `web_search`（tavily） | HTTP 403，API 配额/封禁 |
| curl → `openai.com/index/introducing-codex/` | HTTP 403 |
| curl → `r.jina.ai` 代理 | OpenAI 侧同样拦截，代理返回 "This page couldn't load" |
| 真实浏览器 → openai.com | **Cloudflare Turnstile 挑战**，等待 8s 不自动放行，需人工点选（未自动化） |
| Wayback Machine（`archive.org/wayback/available`、CDX API） | 连接超时，本机不可达 |
| DuckDuckGo（html / lite） | 反爬，无有效结果 |
| Mojeek / searx.be | captcha |
| Bing（真实浏览器，含 `site:openai.com` 限定） | 可加载，但返回**泛化结果**（OpenAI 首页、GPT-4、维基、Codex 安装教程），从未surfacing目标文章 |
| Google（真实浏览器） | 直接返回 `google.com/sorry` 反爬页 |
| Codex 仓库 README | 未链接任何上下文管理设计文档 |

**结论**：本机网络对 openai.com 与主流搜索引擎不可用。
若需原文，需用户提供确切标题/URL（可自行粘贴正文），或换用可访问的网络环境。

---

## 二、实际拿到的：Codex 生产环境压缩实现（已核实）

来源：`https://raw.githubusercontent.com/openai/codex/main/codex-rs/core/src/compact.rs`
（HTTP 200，843 行，主分支真实源码）。
以下**均为源码中直接读到的事实**，非推测。

### 1. 初始上下文是**独立一层**，不参与压缩 ← 最可迁移的一条

压缩时用 `InitialContextInjection` 区分两种情形：

- `BeforeLastUserMessage { world_state, step_context }`
  ——**回合中途**压缩时，把初始上下文重新注入替换历史的「最后一条真实用户消息之上」。
  源码注释写明原因：
  > the model is trained to see the compaction summary as the last item in history
  after mid-turn compaction; we therefore inject initial context into the replacement
  history just above the last real user message.

- `DoNotInject`
  ——**回合前 / 手动**压缩：替换历史为摘要，并清空 `reference_context_item`，
  让下一次正常回合**完整重新注入**初始上下文。

初始上下文由 `sess.build_initial_context_with_world_state(...)` 现场构建。

**含义**：稳定的环境/规则上下文（Codex 的 `AGENTS.md` + world state）
不随历史一起被压缩掉，而是每回合重新注入。
这直接对应本项目「内容包是只读、稳定层」的设计。

### 2. 压缩路径按触发源分流，不是单一入口

- `CompactionTrigger::{Auto, Manual}`
- `CompactionReason::{UserRequested, ...}`
- `CompactionPhase::{StandaloneTurn, ...}`
- `run_inline_auto_compact_task(...)` vs `run_compact_task(...)`

自动压缩与手动压缩走同一个 `run_compact_task_inner`，但**注入策略不同**（见上）。

### 3. 摘要带**可识别前缀**

- `SUMMARY_PREFIX`，落盘为 `format!("{SUMMARY_PREFIX}\n{summary_suffix}")`
- 识别方式：`message.starts_with(SUMMARY_PREFIX)`

即「这段上下文是摘要」这件事是**可判定的**，不会与真实历史混淆。

### 4. 压缩 prompt **可配置**

`config.compact_prompt.as_deref().unwrap_or(SUMMARIZATION_PROMPT)`
——运营者可替换摘要指令，不必改代码。

### 5. 截断按 **token 预算**做，且有预算递减账本

- `COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000`
- `approx_token_count` / `truncate_text` / `TruncationPolicy::Tokens(remaining)`
- 逐条消息扣减 `remaining`，`remaining.saturating_sub(tokens)`

### 6. 压缩**本身**也会撞窗口，降级而非失败

日志文案：
> `Context window exceeded while compacting; removing oldest history item`

即压缩时若仍超窗口，丢最旧条目继续，而不是抛错终止。

### 7. 压缩**可被否决**（钩子）

`run_pre_compact_hooks` / `run_post_compact_hooks`，
`PreCompactHookOutcome::{Continue, Stopped}`——`Stopped` 直接 `CodexErr::TurnAborted`。

### 8. 窗口化压缩 + ID 追踪

`advance_auto_compact_window()`、`AutoCompactWindowIds`、`window_number`
——每次压缩有独立窗口号与 ID，不是全局单一计数器。

### 9. 压缩效果有**度量**

`active_context_tokens_before` / `_after`、`compaction_summary_tokens`、
`cached_input_tokens`、`cache_write_input_tokens`、`duration_ms`。
即「压完到底省了多少、摘要本身多大」是可测量的，不靠感觉。

### 未取到

`codex_prompts::SUMMARIZATION_PROMPT` 的**正文**——该 crate 路径未定位
（`core/prompt.md`、`core/src/codex-prompts.rs`、`core/src/prompts.rs` 等均 404）。
标识符与调用方式已确认，提示词原文缺失。

---

## 三、对第 9 步的启示（**已于 2026-10-02 逐条裁决，见 DECISIONS.md D-187 ~ D-189**）

以下六条已逐条对账当前实现，结论分四类：已由结构满足 / 立项 / 推迟 / 不适用。

| # | 启示 | 结论 |
|---|---|---|
| 1 | 初始上下文（内容包）与历史上下文**分层**：内容包不参与压缩，每回合重注入 | 已由结构满足——`system_and_role` 独立字段，压缩器碰不到 |
| 2 | 摘要**可识别**（带前缀），下游能区分「摘要」与「真实历史」 | **D-187，已实现**（唯一真代码缺口） |
| 3 | 压缩效果**以 token 度量**（压缩前/后/摘要大小） | 已满足；缓存计量不适用 |
| 4 | 压缩 prompt **可配置**，不必硬编码 | 已由**注入**满足（比配置字段更彻底） |
| 5 | 压缩路径按**触发源**分流 | **推迟**——当前只有自动触发，写分支是投机式 generality |
| 6 | 截断按 **token 预算**做 | **D-189**，立为前置约束，落点第 11 步 |

**教训**：六条里只有一条是真缺口，其余三条结构上已满足。
读完外部经验就逐条立决策，会写出一批描述既有设计的空决策——
而空决策比不写更坏，它让人以为那里有个被论证过的选择。

### 本次未能取到的部分（复看时不必重试）

- Codex 那篇博客原文：openai.com 有 Cloudflare，Wayback 本机不可达，搜索引擎反爬。
  若日后拿到原文，应回头核对本节结论是否有遗漏。
- `SUMMARIZATION_PROMPT` 正文：`codex_prompts` crate 路径未定位。
- 缓存计量（`cached_input_tokens` 等）：本项目不控制提示词缓存，不适用。