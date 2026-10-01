# 政通 · 架构设计

> 本文是编码前的架构定稿。**每一条设计都注明它是被哪条决策逼出来的**，
> 没有决策依据的想法一律不写进来（对齐 D-015：借机制不抄代码，一切取舍的
> 最终判据只有一个 —— 能否产出高质量公文）。
>
> 决策唯一权威来源：`DECISIONS.md`。本文不重复决策，只把决策翻译成结构。
> 本文新增的架构决策登记为 D-061 ~ D-069；
> 两轮审阅修订登记为 D-070 ~ D-079（其中 D-073 修订了 D-013 的检索主次）。

---

## 0. 怎么读这份文档

| 你想知道 | 看哪节 |
|---|---|
| 整体长什么样 | §2 分层图、§14 模块树 |
| 为什么这么分层 | §1 目标与判据、§15 决策→机制对照表 |
| 出问题怎么查 | §5 事件脊柱（可观测性）、§12 错误与重试 |
| 以后加功能会不会崩 | §13 升级不破坏的机制、§3.4/§3.5 结构测试 |
| 提示词改了会不会坏 | §7.1 提示词装配、§4 领域模型 |
| 模型接口要换怎么办 | §4 端口、§6 适配器 |
| 写代码前先看哪节 | §2.4 反模式清单、§15 实现顺序 |

术语约定：文中"角色"指专家团成员（范正章等 5 人），"阶段"指 Phase，
"门禁"指 G0–G13 编号中的 12 道正式门禁。三者不等价，勿混。

---

## 1. 架构目标与判据

需求原文有七条硬要求：**可观测性强、可升级性强、升级无需破坏重构、
可维护性强、高内聚低耦合、各层解耦、精确设计**。

这七条不能靠"写好代码"自动获得，必须各有**机制**兜住，否则三个月后就退化。
下表把每条要求落到具体机制上，后文逐节展开。**这张表是本文的验收清单。**

| 要求 | 兜底机制 | 失效时的症状 | 对应节 |
|---|---|---|---|
| 可观测性强 | 单一事件流 + 两个投影 + 追加式日志 + `trace` 命令 | 出了问题只能靠猜 | §5 |
| 脱敏可证 | 密钥在**发射处**脱敏，不在显示处 | 调试模式把 key 打进日志 | §5.3 |
| 可升级性强 | 端口隔离依赖，换实现只动一个文件 | 换 HTTP 库要改 20 个文件 | §6 |
| 升级不破坏重构 | 6 条机制：版本化 schema / 只加不改 / 迁移链 / 稳定 API 面 / 契约测试 / 内容包解耦 | 升级后旧 `data/` 打不开、旧配置报错 | §12 |
| 可维护性强 | 分层约束由 **CI 测试强制**，不靠自觉 | 有人 import 了不该 import 的模块，没人发现 | §3.4 |
| 高内聚低耦合 | 依赖只向内，领域层与内核零第三方依赖 | 领域类型里冒出 `httpx.Response`，测试要联网 | §3.2 §4 |
| 各层解耦 | 内核不知道 TUI 存在，驱动不知道编排细节 | 换界面 = 重写编排 | §8.1 |
| 精确设计 | 每处设计标注决策编号；无依据的"以后可能要"不写 | 范围膨胀，投机性抽象 | 全文 |

### 1.1 一个必须先承认的约束

**流式被砍掉了**（D-026），这是本项目最大的架构简化红利：
模型调用是一次阻塞的"发出去—等回来"，没有增量、没有背压、没有半截输出。
所以整个系统里**不存在**流式状态机、不存在增量渲染、不存在输出打断。
所有看起来需要流式才能做 fancy 交互的想法，一律不做。

代价：D-048 探测到推理开启时，一次调用可能 67 秒（D-024 实测基线）。
这决定了 §8 的内核必须跑在独立线程上，界面不能被阻塞。

---

## 2. 分层架构

### 2.1 分层图

```
┌─────────────────────────────────────────────────────────────┐
│ L6 接入层  presentation/{tui, headless, doctor}               │
│    只做两件事：把事件流翻译成人话；把人话翻译成命令            │
│    ⬇ 只依赖 L5 暴露的 事件总线 / 命令接口                     │
├─────────────────────────────────────────────────────────────┤
│ L5 编排层  orchestration/                                    │
│    状态机 · 阶段推进 · 角色调度 · 门禁编排 · 断点续跑         │
│    ⬇ 只调用 L4 服务与 L3 端口，不认识 httpx / textual          │
├─────────────────────────────────────────────────────────────┤
│ L4 服务层  services/                                         │
│    上下文管理 · 检索 · 提示词装配 · 工具执行 · 校验 · 遥测     │
│    ⬇ 只依赖 L2 端口接口 + L1 内核 + L0 领域                   │
├─────────────────────────────────────────────────────────────┤
│ L3 端口层  ports/            ← 依赖倒置的落点                 │
│    LLM · 内容包 · 存储 · 用户文件 · 校验器 · 密钥 · 时钟      │
│    ⬇ 纯抽象，无实现，无第三方依赖                            │
├─────────────────────────────────────────────────────────────┤
│ L2 适配层  adapters/                                         │
│    openai_style 客户端 · 本地内容包 · 本地文件 · toml 存储      │
│    docx 读取器 · 两个校验器移植版                              │
│    ⬇ 依赖 L3 端口 + 第三方库（httpx / textual 都在这一层）    │
├─────────────────────────────────────────────────────────────┤
│ L1 内核层  kernel/                                            │
│    事件总线 · 投影 · 驱动契约 · 脱敏 · 标识 · 结果 · 错误      │
│    ⬇ 零第三方依赖                                            │
├─────────────────────────────────────────────────────────────┤
│ L0 领域层  domain/                                            │
│    任务 · 档位 · D-Card · 产物 · 门禁 · 角色 · 事件 · 错误     │
│    ⬇ 零依赖，连标准库都不用，只用 dataclass / enum / Literal  │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 为什么是七层而不是三层

常见的"MCP 那种 核心 / 工具 / 界面"分法对本项目不够用。
本项目有两处**独立演进的复杂度**必须各自隔离，否则必然互相污染：

1. **模型接入的不确定性**（D-004 只承诺 OpenAI 风格、D-048 要探测 4 种推理写法、
   D-051 的 `max_tokens` 陷阱）——这块要能整体替换甚至重写而不动别的。
2. **交互形态的不确定性**（D-012 全屏 TUI、D-037 无界面模式、
   D-023 两种可观测档位）——这块要能加第三个接入方式而不改编排。

所以在"核心"两侧各插一层：**L3 端口**把外部世界抽象掉（L2 只在这里碰第三方库），
**L5/L6 分开**让"编排"与"呈现"解耦。代价是多两个目录，收益是任何一层的重写
都不波及其他层——这就是 D-015 说的"提取机制层洞察"落到结构上的样子。

L0 与 L1 拆开，是因为事件类型会被**写盘、跨进程传、被外部工具读**，
它必须比内核其余部分更稳定；混在一起会导致改日志格式时顺手改了事件形状。

### 2.3 依赖规则（唯一一条，不可协商）

> **箭头只向内。L2 实现 L3，L4 使用 L3，L5 使用 L4+L3，L6 使用 L5+L1。**
> **L6 不得被 L5 及以下任何层 import。L3 不得 import L4 及以上。**
>
> **接入层（L6）的权限被刻意收窄为 `orchestration + kernel + domain`**：
> 它**不得直接 import `services`**。要调服务能力必须经编排层转发。
> 理由见下方"为什么接入层不许直连服务"。

这条规则的实际后果：

- L5 编排层里不会出现 `import httpx`。它只知道 `LLMPort`。
- L4 服务层里不会出现 `import textual`。界面死机不影响编排，编排可脱离界面单测。
- L0 领域层里不会出现任何 `import`。D-Card 就是 D-Card，不是一个 dict。

**验证方式不是自觉，是测试**（§3.4）。

#### 为什么接入层不许直连服务

§8.1 精心设计了"接入层只消费事件流、把指令交给内核"这条单向通道。
一旦允许接入层直接 `import services`，就等于在状态机旁边开了后门：
TUI 拿到事件后可以直接改上下文、改产物、甚至触发一次模型调用——
**这些动作全部绕过状态机**，于是断点续跑、幂等重放、门禁编排全部失效。
典型后果是界面为了"显示得更及时"而直接读一次产物，
这次读取不进 journal，于是 `zhengtong trace` 少一段，可观测性立刻打折。

所以接入层只有三种权力：读事件、递指令、渲染。
**它不能自己做事，只能请内核做事。**

### 2.4 反模式清单（评审时逐条打钩）

| 反模式 | 为什么禁 | 正确做法 |
|---|---|---|
| 在编排层 `import httpx` 直接发请求 | 换后端要改编排 | 走 `LLMPort` |
| 在 TUI 里 `await` 模型调用 | 界面会被 67 秒请求冻住（D-024 实测） | 内核在 worker 线程，TUI 只收事件 |
| 工具处理器里 `open(path)` 拿裸路径 | 绕过路径白名单，D-042 的安全边界形同虚设 | 走 `WorkspaceGuard` 解析 |
| 角色之间直接函数调用传产物 | 违反 D-041 独立上下文，产物无法追溯 | 只能经工具写公共区 |
| 门禁失败"打个 warning 放行" | 违反 D-028 无绕过出口 | 按 D-016 三档处置，红线必须退回 |
| 配置项散落在各层 `os.environ.get` | 删配置就找不到谁在读 | 只经 `config` 单一入口 |
| **接入层直接 `import services`** | 绕开状态机，破坏断点续跑与可观测性（见 §2.3） | 经编排层转发 |
| **编排层直接 `new` 任何 adapter** | 绕过 `PortRegistry`，依赖注入名存实亡 | 构造时注入端口 |
| 提示词模板硬编码在 Python 字符串里 | 违反 D-008 软数据平级，提示词不可 diff | 全部在内容包，代码只做占位填充 |
| 事件里塞 API key | D-044 之外的安全事故 | 发射处脱敏（§5.3） |
| 用 `time.sleep` 在编排里等 | 阻塞内核线程，无法并行门禁（D-049） | 状态机推进 + 条件唤醒（D-042） |

---

## 3. 领域层（L0）

领域层是**唯一被所有人依赖的东西**，所以它必须最笨、最稳、最没脾气。

### 3.1 设计原则

1. **全是数据，没有行为。** 用 `dataclass(frozen=True)` + `Enum` + `Literal`。
   唯一允许的方法是 `to_dict` / `from_dict` 这类纯序列化。
2. **零依赖。** 只用 `dataclasses`、`enum`、`typing`。
3. **可 JSON 往返。** 任何领域对象 `from_dict(json.loads(json.dumps(x.to_dict()))) == x`。
   这是断点续跑（D-027）与日志追溯的物理基础。
4. **带 `schema_version`。** 每一个会落盘的领域对象都有。

### 3.2 核心类型清单

| 类型 | 职责 | 关键字段（节选） |
|---|---|---|
| `TaskSpec` | 一次任务的全部输入 | `run_id` / `tier` / `requirement_text`（D-033 单一多行原文） / `user_files`（仅文件名，D-022） |
| `TierSpec` | 档位定义（**数据，不是代码**，D-067） | `roles` / `phases` / `gate_groups` / `reasoning_policy` / `turn_limits` |
| `DCard` | 方向确认单 D1–D9（D-040） | 每字段带 `provenance: USER \| LEAD_INFERRED`，D8 恒为 `LEAD_INFERRED` |
| `DCardField` | 单个字段 | `code`(D1..D9) / `value` / `provenance` / `asked_at` / `degraded`（`[待填·已告知]`） |
| `Artifact` | 产物（D-011） | `name` / `kind`（outline / draft / memo / evidence…） / `producer` / `version` / `content_hash` |
| `RoleState` | 单个角色的状态（D-042 六态） | `role_id` / `status` / `turns` / `turn_limit`（D-058） / `wake_condition` |
| `GateState` | 单道门禁状态 | `gate_id` / `group` / `attempts`（≤2，D-050） / `verdict` / `evidence` / `owner` |
| `PhaseState` | 阶段状态 | `phase_id` / `entered_at` / `progress_note` / `artifacts` |
| `RunState` | 整次运行的可持久化全量状态 | 以上全部 + `current_phase` + `last_event_seq` |
| `Event` | 事件（§5） | `seq` / `kind` / `audience` / `level` / `actor` / `span` / `data` |

### 3.3 `DCardField.provenance` 为什么必须存在

D-034 是最高数据纪律：严禁编造。但**光靠提示词约束"不许编造"是不可靠的**，
D-008 的精神是"靠结构而非靠自觉"。所以每个方向确认单字段都必须标出处：

- `USER` —— 用户明确说的，或从 `user/` 文件里提取的。**只有这两种来源。**
- `LEAD_INFERRED` —— 范正章研判的。**仅 D8 允许**（D-040 明确 D8 由主理人研判并标注）。

这样导出前的"编造扫描"（§9.1）就变成一次机械检查：
`any(f.provenance == USER for f in required_fields if not f.value)` → 拦截。
不需要判断文本像不像编的。

D3 走过降级出口的，`degraded=True`，导出时按 D-040 强制在交付说明首行提示。

### 3.4 分层约束的强制执行

`tests/test_layering.py` 用 `ast` 静态扫描 `src/zhengtong/`，断言：

```
允许：  domain  → （无）
允许：  kernel  → domain
允许：  ports   → domain
允许：  adapters→ ports, kernel, domain, 第三方
允许：  services→ ports, kernel, domain
允许：  orchestration → services, ports, kernel, domain
允许：  presentation → orchestration, kernel, domain
允许：  cli, config, app → 全部

禁止：  任何层 → 自己的上层（除 cli/app 装配根）
禁止：  presentation → services          # 接入层不得直连服务（§2.3）
禁止：  domain, kernel, ports → 任何第三方库
禁止：  orchestration → adapters          # 编排层不得 import 或构造适配器
禁止：  任何层内出现 `AdapterClass(...)`     # 适配器只能由 app.py 装配
```

最后一条的实现方式：测试扫 `src/zhengtong/` 下除 `adapters/` 与 `app.py` 外
所有文件的 AST，若出现对 `zhengtong.adapters` 下任一模块的 import，
或以端口返回类型为目标直接调用其构造函数，测试失败。

**为什么需要最后两条**：只禁 import 方向挡不住"合法 import 了但没用注入"。
比如编排层写 `from zhengtong.adapters.llm.openai_style import OpenAiStyleClient`
再 `OpenAiStyleClient(cfg)`——依赖方向表看不出问题，但依赖注入已经形同虚设，
测试时无法替换成假端口。**分层规则必须连"谁能造对象"一起管。**

**这条测试失败 = 架构被破坏 = CI 红。** 这是"可维护性强"唯一诚实的兜底方式。
写在 README 里让人自觉的做法，三个月内必然失效。

### 3.5 跨层调用规约（比依赖方向更能防退化）

依赖方向只管"谁能看见谁"，管不住"传了什么过去"。
一个常见的退化：编排层为了方便，把整个 `RunState` 递给服务层——
服务层拿到后改了其中某个字段，编排层以为是自己改的。
这时 `frozen=True` 也没用，因为**它拿到的是同一个对象**。

所以追加三条**参数层**的规约：

| 规约 | 内容 | 防止什么 |
|---|---|---|
| **只传值对象** | 跨层参数必须是 L0 的 `frozen` 值对象或纯数据；**禁止传编排层的可变状态对象** | 下层偷偷改上层的状态 |
| **入深出浅** | 编排层调服务时传 `RunState` 的**片段**（如 `ContextView` + `TaskSpec`），服务返回**新对象**；服务不得持有传入对象的引用 | 服务变成隐藏的状态容器 |
| **状态只在编排层写** | `RunState` 的**任何字段**只允许 `orchestration` 层赋值；服务层只读 | 状态散落各处、无法追责 |

举例说明。错误：

```python
# ✗ 服务层拿到了整个运行状态
def compose_for(self, run_state: RunState) -> ContextView: ...

# ✓ 只拿片段，返回新对象，不持有引用
def compose_for(self, view: ContextView, spec: TaskSpec) -> ContextView: ...
```

这条比依赖方向规则更有效——依赖方向错了测试立刻红，
而"传了整个状态对象"是能通过所有现有测试的慢性退化。

**实现约定**：服务层函数的第一个参数**不叫 `state`**。
用命名约束让人一眼看出传窄了。

---

## 4. 端口层（L3）

### 4.1 端口清单

端口是**抽象**（`typing.Protocol` 或 ABC），不含实现、不含第三方类型。
每个端口的方法签名只用领域类型和标准库类型。

| 端口 | 关键方法 | 决策依据 |
|---|---|---|
| `LLMPort` | `chat(req: ChatRequest) -> ChatResponse` | D-004 OpenAI 风格；D-026 不流式 |
| | `probe(kind: ProbeKind) -> ProbeResult` | D-024 四项 + D-048 推理关闭 |
| | `capabilities() -> Capabilities` | D-024 工具调用支持 |
| `ContentPackPort` | `manifest() -> PackManifest` | D-014 版本化 + 内容哈希 |
| | `read(rel_path) -> str` / `list(kind) -> list[str]` | D-014 |
| `ArtifactStorePort` | `write/read/list/publish/publish_draft` | D-011 公共区 + 草稿纸 |
| `UserFilePort` | `scan() -> list[FileEntry]` | D-022 自动扫描 |
| | `read(name) -> FileContent` | D-053 2 万字截断 |
| `RetrieverPort` | `search(query, scope, k) -> list[Hit]` | D-073 中文 n-gram 为主路径 |
| | `index(scope) -> IndexStats` | D-017 索引可从内容包与 `user/` 重建 |
| | `available() -> Backend` | 报 n-gram 与 FTS5 各自可用性（D-073） |
| `ValidatorPort` | `run(name, args) -> ValidationResult` | D-054 移植两个校验器 |
| `SecretPort` | `get(key) -> SecretRef` | 密钥明文可存（D-002），但只出 `SecretRef` |
| `ClockPort` | `now() / monotonic() / sleep()` | 可注入假时钟，测试不真等 |

### 4.2 端口设计的三个纪律

1. **签名里不许出现第三方类型。** 不许写 `def chat(self, req: httpx.Request)`。
   出现了就说明抽象漏了。
2. **不许端口里塞"顺手做一件别的事"。** 例如 `LLMPort.chat` 不许顺便记账 token。
   记账由 L4 遥测服务根据返回值做——否则每个适配器实现者都要记得记账。
3. **能力探测独立成方法，不塞进 `chat` 的返回值。** 因为 D-048 表明
   "关推理写法"有 4 种候选，必须**主动按序试探**，不是问一次拿到的。

### 4.3 注册表

```python
# ports/registry.py —— 全系统唯一的装配点登记处
class PortRegistry:
    llm: LLMPort
    content: ContentPackPort
    storage: ArtifactStorePort
    userfiles: UserFilePort
    retriever: RetrieverPort          # 换检索实现只动 app.py 一行
    validators: dict[str, ValidatorPort]
    secrets: SecretPort
    clock: ClockPort
```

编排层构造时**接收** `PortRegistry`（依赖注入），不自己 `new`。
`app.py` 是唯一知道所有具体实现的地方，它负责装配。
这就是"低耦合"落地的地方：**换后端 = 写一个 adapter + 改 app.py 一行。**

---

## 5. 事件脊柱与可观测性 ★

这是本架构最核心的一节。可观测性不是"加日志"，是**结构**。

### 5.1 设计判据

要能回答的每个问题，都必须能从日志里查出来：

| 问题 | 靠什么回答 |
|---|---|
| 现在到哪一步了？ | `phase.*` 事件 |
| 谁做的？ | `Event.actor`（角色 id 或 `system`） |
| 依据是什么？ | `Event.span` 调用链 + 产物 `content_hash` |
| 花了多久？ | `Event.ts_monotonic` 差值 |
| 花了多少 token？ | `ChatResponse.usage`，聚合进遥测台账 |
| 为什么退回？ | `gate.verdict` + `evidence` 事件 |
| 这稿是谁的锅？ | 门禁 `owner`（D-049 的责任映射） |
| 密钥有没有泄漏？ | 发射处脱敏（§5.3），不靠显示端过滤 |

### 5.2 单一事件流 · 边缘过滤

```
              ┌──────────────────────────────────────┐
  所有层 ────► │            EventBus（单例）           │
   发射        │  追加写 journal(JSONL) + 通知订阅者    │
              └──────────────┬───────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
     StandardProjection              DebugProjection
     只放行 audience=USER           全量透传（D-023
     且 kind 在白名单内              "有啥输出啥"）
              │                             │
              ▼                             ▼
        TUI 界面 / 交付说明        终端 + prompt 落盘
```

**关键决定：过滤发生在投影端，不在发射端。**

反例（必须避免）：在每个 `logger.info()` 调用点判断"现在是不是调试模式，
不是就跳过"。那样会漏——因为总有人忘记判断，而且判断逻辑散落在 200 个调用点。
边缘过滤保证了**只有一处**决定"给不给用户看"，且这处的规则可以用一个表描述完。

### 5.3 脱敏在发射处

```python
class EventBus:
    """单点发射：seq 分配、脱敏、落盘、分发都在泵线程里完成。"""

    def __init__(self) -> None:
        self._inbox: SimpleQueue[Event] = SimpleQueue()  # 任意线程可投递
        self._seq = 0
        self._pump = Thread(target=self._drain, daemon=True)
        self._pump.start()

    def emit(self, ev: Event) -> None:
        """任意线程可调用。只入队，不分配 seq，不落盘。"""
        self._inbox.put(ev)

    def _drain(self) -> None:
        """唯一的发射点。seq 在此分配，故必然唯一且单调。"""
        while True:
            ev = self._inbox.get()
            if ev is _SHUTDOWN:
                return
            self._seq += 1
            safe = self._redactor.scrub(ev)   # ← 在这里脱敏，早于落盘与分发
            safe.seq = self._seq
            self._journal.append(safe)        # 落盘的是已脱敏的
            for sink in self._sinks:
                sink(safe)                    # 分发的是已脱敏的
```

**为什么必须是队列而不是加锁（D-075）。**
D-064 决定门禁走线程池，于是门禁线程与编排线程会并发调 `emit`。
若 `seq += 1` 裸写在 `emit` 里，多线程会读到同一个值再各自自增，
产生**重复 seq**——而 seq 是 trace 与 replay 的唯一排序依据，
重复即意味着回放时间线是假的，排查问题时看到的因果关系不成立。
实测：4 线程 × 2000 次无锁分配，得 7997 个唯一值、3 个重复。

加锁能解，但只是把竞态从"分配"挪到"锁竞争"。队列方案更强，因为它顺带
保证了一件事：**事件顺序与状态机推进顺序一致**。门禁线程不再自己决定
"我的事件落在第几位"，而是由泵线程按到达次序统一编号——
回放时看到的顺序，就是内核实际观察到的顺序。

代价是引入至多一次队列延迟。对遥测与审计完全无影响（D-031 只统计耗时区间），
而 `flow.*` 这类需要即时反馈的调用走的是返回值，不是事件流。

因为**落盘的就是已脱敏的**，调试模式打开也不会把密钥写进日志。
"显示端过滤"做不到这一点——它只保护屏幕，不保护磁盘。
（约束：API key 可明文存 `system/`，但不得进入任何日志与续跑摘要。）

`SecretPort` 进一步保证：编排层拿到的 `SecretRef` 根本不是明文，
要到 L2 适配器构造 HTTP 请求的那一刻才解出真值。**上层永远拿不到明文。**

### 5.4 事件目录（`EventKind`）

事件种类要**少而正交**，每种都对应一个用户能问的问题。

| 组 | 事件 | 携带 |
|---|---|---|
| 运行 | `run.started` / `run.finished` / `run.failed` / `run.resumed` | `tier` / `run_id` |
| 阶段 | `phase.entered` / `phase.exited` / `phase.progress` | `phase_id` |
| **阶段成果** | `phase.artifact_ready` | `phase_id` / `name` / `kind` / **`content` 全文** / `hash` / `is_gate` |
| 角色 | `role.turn` / `role.tool_call` / `role.sleep` / `role.wake` / `role.report` | `role_id` / 工具名 / 轮次 |
| 模型 | `model.request` / `model.response` / `model.error` / `model.retry` | token / 耗时 / `finish_reason` |
| 门禁 | `gate.started` / `gate.verdict` / `gate.rework` / `gate.escalated` | `gate_id` / 判定 / 证据锚点 |
| 提问 | `user.asked` / `user.answered` | 字段清单（D1–D9），**只记字段码不记答案全文** |
| 上下文 | `context.composed` / `context.compacted` | 占用率 / 保留了哪几类（D-039） |
| 产物 | `artifact.published` / `artifact.drafted` | `name` / `kind` / `hash` |
| 导出 | `export.blocked` / `export.done` | 被拦截的占位符清单 / 文件列表（D-038） |

`audience=USER` 的白名单**只有**四类（对齐 D-023 用户只需知道四件事）：
`phase.artifact_ready`、`export.*`、`run.failed`、`user.asked`。
其余全部 `INTERNAL`——这条白名单同时落实 D-046（不显示成员状态）
与 D-030（不展示审核意见）。

**注意白名单里为什么只有 `phase.artifact_ready` 而不是整个 `phase.*`**：
进度类事件（`phase.entered` / `phase.progress`）的标准界面**只用来看那一行总体进度**
（D-046），不需要单独渲染成卡片；真正要给用户"看内容"的只有三份阶段成果
（方向确认单 / 文章大纲 / 正文初稿，D-030），它们由 `phase.artifact_ready` 承载**全文**。

所以这个事件的 `data.content` **装的是成果全文，不是摘要**。
界面拿到它直接渲染成成果卡；调试模式则拿它和后续的 `artifact.published` 对照，
检查两者内容一致（防"界面显示的和落盘的不是一份"这类最难查的错）。

### 5.5 追溯能力清单

| 手段 | 命令 / 位置 | 用途 |
|---|---|---|
| 追加式日志 | `system/logs/<run-id>.jsonl` | 全量事件，永不覆盖 |
| 提示词落盘 | `system/logs/prompts/<run-id>/<seq>.json` | 调试模式可事后复盘，不必守着终端 |
| 时间线回放 | `zhengtong trace <run-id>` | 人可读时间线，含阶段/门禁/耗时/token |
| 轮次重放 | `zhengtong replay <run-id> --from <seq>` | 从某事件重放到末尾（纯投影，不调模型） |
| 健康自检 | `zhengtong doctor` | D-024 四项 + D-048 推理探测 + D-045 字体 + 内容包完整性 |
| 遥测台账 | `system/telemetry.json` | 各步骤历史耗时均值 → 喂 D-031 预估 |

D-031 明确"不编造数字"。所以 `zhengtong` 在**没有同类步骤历史**时，
界面如实显示"暂无法估计"，而不是填一个假数。遥测台账是唯一数据来源。

---

## 6. 适配层（L2）

第三方库**只允许出现在这一层**。这是整个架构换依赖成本的决定性因素。

### 6.1 `adapters/llm/openai_style.py`

职责：把 `ChatRequest` 变成 HTTP 请求，把响应变回 `ChatResponse`。**不含任何业务判断。**

必须正确处理的四件事（每件都来自实测教训，D-048 / D-051）：

1. **端点原样请求。** 用户填完整地址，程序**不拼接、不改写 URL**（D-004）。
2. **`finish_reason` 分流。**
   - `stop` → 正常
   - `length` 且正文为空 → 视为失败，**本次关推理重试**（D-051，不是翻倍上限）
   - `length` 且正文非空 → 保留正文，事件标"输出被截断"
3. **`reasoning_content` 一律丢弃**，不写进成员消息历史、不落盘（D-052）。
   但 `usage.completion_tokens_details.reasoning_tokens` 要读出来——D-048 的判定靠它。
4. **服务端结构化错误**（`code` / `type` / `trace_id`）翻译成中文提示（D-024）。

### 6.2 `adapters/llm/reasoning_probe.py`

D-048 的探测式适配，独立成一个文件，因为它是**唯一一处**需要知道后端烙印的地方。

```
按序尝试 4 种写法，每种发一个最短请求：
  1. thinking: {"type": "disabled"}                      → vLLM（含当前实测服务）
  2. chat_template_kwargs: {"enable_thinking": false}      → llama.cpp 新版、transformers
  3. reasoning_effort: "none"                             → OpenAI 风格后端
  4. 提示词前缀 /no_think                                 → llama.cpp 老版、Qwen 模板
判定：reasoning_tokens 是否降为 0  ← 不是看 HTTP 状态码
```

**四条无效写法全部返回 HTTP 200**（D-048 实测），只看状态码会误判为"支持关闭"。
这个坑必须固化成一行断言写在代码注释里，否则后人会重犯。

4 种全败 → 明确告知用户"这个服务关不掉推理，将按开启运行"，**不静默失败**。
探测结果落 `system/caps.json`，**每档位每环节**按 D-048 的表取值。

### 6.3 `adapters/userfiles/readers/`

| 格式 | 实现 | 决策 |
|---|---|---|
| `.txt` `.md` | 标准库，编码自动探测（UTF-8 → GB18030 → 回退） | D-032 |
| `.docx` | `zipfile` + `xml.etree`，**零第三方依赖** | D-032 |
| `.pdf` / `.doc` / 图片 | **不实现解析**，只登记文件名并在界面提示 | D-032 用户明确砍除 |

单个文件读取上限 **20,000 汉字**，超出部分不读，事件标明"已截断"（D-053）。
读取器**只读**——不移动、不改写、不归档用户文件（D-022 / D-032）。

#### `.docx` 必须先查元数据再读内容（D-078）

`.docx` 是 zip。`zipfile.read(entry)` 会把整个 entry 解压进内存，
而 `ZipInfo` 里已经写着解压后的大小——**先查再读，一个字节都不多解**：

```python
MAX_ENTRY   = 20 * 1024 * 1024    # 单 entry 解压后上限
MAX_TOTAL   = 100 * 1024 * 1024   # 全包解压后总量上限
MAX_RATIO   = 100                 # 压缩比上限，防 zip bomb

def guard(zf: ZipFile) -> None:
    total = 0
    for info in zf.infolist():          # 元数据在 central directory 里，不解压即可读
        if info.file_size > MAX_ENTRY:
            raise DocxTooLarge(f"{info.filename} 解压后 {info.file_size >> 20}MB")
        if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
            raise DocxTooLarge(f"{info.filename} 压缩比异常")
        total += info.file_size
        if total > MAX_TOTAL:
            raise DocxTooLarge("解压总量超限")
```

**这是真实可触发的，不是防御性编程。** 实测构造一个 zip bomb：
磁盘上只有 **190 KB**，解压后 **190 MB**——纯 `read()` 会直接把这 190MB
打进内存。一个畸形或恶意的 `.docx` 就能让程序 OOM。
元数据检查在循环里就把它挡掉，实测确认拦截。

**XML 实体展开已是安全的**，无需额外处理：Python 的 `xml.etree`
底层 expat 对未定义实体抛 `ParseError`，实测含外部实体的 docx 被拒。
保留这一默认行为，**不要**为了"兼容性"去换允许实体展开的解析器。

### 6.4 `adapters/validators/`

| 移植 | 改造 | 决策 |
|---|---|---|
| `hard_constraint_check.py` | 入参改结构化数据 | D-054 移植 |
| `evidence_anchor_check.py` | 入参改结构化数据 | D-054 移植 |
| `consistency_check.py` | **不移植** | D-054 含 OCR / AI 检测，与 D-032 冲突 |

**这三个脚本的判定边界必须原样保留**（gate-manual 明文规定）：
脚本只判"字符串是否可定位"，**不产出质量结论**，其输出**不得作为门禁通过依据**。
所以在代码里它们实现的是 `ValidatorPort`，返回值类型就叫 `ValidationResult` 而不是
`GateVerdict`——类型系统层面阻止误用。

---

## 7. 服务层（L4）

L4 是"业务能力"，不含编排决策。它回答"怎么做"，不回答"什么时候做"。

### 7.1 `services/prompting/` 提示词装配

**代码不硬编码任何公文内容**（D-014）。装配流程：

```
角色提示词（内容包/prompts/<role>.md）
  + 按需装载的规则条目（内容包/references/*.md）
  + D-Card 事实块
  + 产物契约（上游产物的引用，不是全文）
  + 工具清单（本次实际可调用的子集）
  → 最终 prompt
```

三条纪律：

1. **规则按需装载，不常驻**（D-025 分层装载）。例：主体排序规则仅在
   D-Card 出现 ≥3 个主体时才进 prompt。这直接省上下文预算。
2. **产物只传引用不传全文**（D-025 结果直传）。下游要内容就用
   `shared.read(name)` 自己读。避免同一份大纲在 5 个角色的上下文里各存一份。
3. **提示词可 diff**（D-008 软数据平级）。装配结果在调试模式落盘到
   `system/logs/prompts/`，改一句提示词能看出装配后变成什么样。

### 7.2 `services/context/` 上下文管理

**核心决定：上下文是一个"视图"，不是"累积的消息列表"。**

```python
@dataclass(frozen=True)
class ContextView:
    system_and_role: tuple[Msg, ...]            # 恒在
    pinned_facts:      tuple[Msg, ...]          # D-039 绝不丢弃
    artifact_refs:     tuple[ArtifactRef, ...]  # 只传引用
    recent_window:     tuple[Msg, ...]          # 有限长
    summary_of_dropped: Msg | None              # 早期讨论的摘要
```

**全部用 `tuple`，不用 `deque` / `list`。** 视图必须真正不可变——
若装 `deque`，可变状态会穿透 `frozen` 壳，hash 也不稳定，
而下面三个卖点全都建立在"视图是值"上。
窗口滑动这种可变动作放在**生成视图的 composer** 里，不放在视图本身：
composer 读旧视图、算出新 `tuple` 视图返回，视图自身始终是常量。

这样做的三个好处：

1. **压缩 = 改视图，不是破坏性删除。** 视图切换可逆、可审计、可回放。
2. **压缩这件事本身可观测。** 触发压缩时发 `context.compacted` 事件，
   报"保留了 X / Y / Z"（D-025 要求在标准模式提示这一句，不展开细节）。
3. **不会累积出病态历史。** 因为根本没有"追加到列表"这个动作。

Token 估算按 **D-025 实测基线**：汉字 `÷ 1.6`（即每汉字算 0.625 token），非汉字 `÷ 2`。

> **⚠️ 此处曾被误改过一次，已回归 D-025 原值。**
> 曾据"中文 0.6–1.0 token/字"的通用经验，判定 `÷1.6` 为系统性低估并改成 `×0.9`。
> **该判断已作废**：D-025 自带实测——16,000 汉字 → 8,064 token（1.98）、
> 60,000 → 30,066（1.99），即**每汉字约 0.502 token**。
> `÷1.6` 意味着每汉字算 0.625，**比实测高 24%**，方向是高估、触发更早，
> 正是 D-025 明写的意图："宁可压缩勤一点，不可撞上限"。
> 若改成 `×0.9` 反而比实测保守 79%，会**无谓地频繁触发压缩**，每次都丢上下文。
> 详见 D-077（含完整作废记录）。

**估算值与真实值必须分开对待（D-077 确立的纪律）：**

| 用途 | 用哪个 | 理由 |
|---|---|---|
| 压缩触发判定（85% 阈值） | **保守估算** | 早触发优于晚发现 |
| 界面展示用量 | API 返回的真实值 | 用户看的是实际消耗 |
| 门禁 token 台账（D-031） | 真实值 | 耗时预估须基于事实 |

理由：真实值只有请求发出之后才知道，那时窗口已满——**估算的作用不是"算准"，
而是"提前示警"**，故不能用真实值做触发判定。

**实现约束**：估算公式**只允许存在于 `estimate.py` 一处**并写明实测来源，
禁止各调用点各写一套系数。注意 D-053 的"128K ≈ 64000 汉字"取的是每汉字 0.5，
与本处 0.625 不同源，**实施时需对齐**（已有必过测试 `test_token_estimate`）。

压缩保留优先级（从后往前丢，D-039）：

| 级别 | 内容 |
|---|---|
| 绝不丢弃 | 方向确认单全部字段、用户明示的事实与禁止事项、硬约束清单（D7）、已发现的严重问题 |
| 尽量保留 | 文章大纲、已写好的正文段落 |
| 可摘要替代 | 早期讨论过程、工具调用原始返回、参考材料原文 |

`pinned_facts` 这一栏就是"绝不丢弃"那四类的**物理载体**——
不是靠压缩算法记得保留，而是结构上就有一个不会被丢弃的字段。

### 7.3 `services/retrieval/` 检索

**中文 n-gram 倒排是主路径。SQLite FTS5 降为拉丁字符的补充路径。**
全部自写，零第三方依赖（D-013）。不上向量检索：离线 embedding 体积大，
远程 embedding 会把公文内容发出去——数据纪律不允许。

#### 为什么不用 FTS5 做主路径

这不是偏好问题，是实测事实。FTS5 默认分词器 `unicode61` 按 `[A-Za-z0-9_]+` 切词，
**汉字不在这个字符类里**，于是整句无空格中文被当成一个超长 token。
而公文恰恰全是无空格中文。实测：

| 查询 | FTS5 命中 |
|---|---|
| `增长` | **0** |
| `通知` | **0** |
| `工作` | **0** |
| `关于进一步做好稳增长工作的通知` | 1（整句当一个 token 才对上） |

换 `trigram` 也不行：`稳增长`、`的通知` 命中，但**两字及以下的词全部失效**——
而"通知""报告""请示""批复""通报"在公文里是最高频的检索词。

所以本项目的检索主路径**不能**依赖 FTS5 的分词能力，这是地基问题，不是调参问题。

#### 分词：字级 n-gram 加权

把连续汉字切成 1/2/3/4-gram，拉丁字母与数字按整词（大小写归一）。
n-gram 长度携带权重——**越长的片段信息量越大**：

| n | 权重 | 作用 |
|---|---|---|
| 1 | 0.15 | 兜底，保证单字词可召回 |
| 2 | 0.45 | 主力，"通知""批复"靠它 |
| 3 | 1.00 | 提升区分度 |
| 4 | 1.40 | 长查询靠它拿到高区分度 |

1-gram 保留是**功能性必需**，不是妥协：去掉它，"批复"这类两字词会直接归零。

#### 打分：加权 TF-IDF + 长度归一

```
score(d) = Σ_t  idf(t) · qw(t) · tf(t,d)·k₁ / (tf(t,d) + k₁·(b + b·len(d)/maxlen))
idf(t)   = ln(1 + (N − df(t) + 0.5) / (df(t) + 0.5))     k₁ = 1.2   b = 0.25
```

实测（5000 篇公文语料，自查询）：**top-8 召回 100%**，单次查询 0.6–1.9 ms，
建索引 1533 篇/秒。

#### 一条必须写进代码注释的判据

**命中数不是检索质量的指标。**
5 个候选里只要 top-1 正确，检索层就是合格的——精确过滤是下游门禁与模型的活。
实测中"党政机关公文处理工作条例"返回 5 个候选，但加权后 top-1 全部正确。
**禁止**在检索层加"必须唯一命中"之类判断，那会误杀合法结果。

#### 三路职责（主次已按实测倒置）

| 路径 | 职责 | 状态 |
|---|---|---|
| **中文 n-gram 倒排（自写）** | **中文检索主路径** | 标准库即可实现，必有 |
| 加权 TF-IDF（自写） | 打分与排序 | 同上 |
| FTS5 | **仅**拉丁字母、数字、专有名词的精确匹配 | 可选增强 |

`sqlite3` 与 `FTS5` 是标准库但**可能未编译**。FTS5 现在是可选增强，
所以它不可用时**只损失拉丁字符检索能力，中文主路径完全不受影响**——
这比旧设计"FTS5 挂了要降级到 n-gram 兜底"健壮得多。

索引全部落在 `data/` 或 `system/`，**可从内容包与 `user/` 完整重建**（D-017）。
删干净后立刻能重新工作，不残留隐藏状态。

**规模护栏（实测得出，超出即降级而非 OOM）：**

| 规模 | 倒排表内存 | 建索引 | 单次查询 |
|---|---|---|---|
| 5000 篇 × ~1500 字 | 24–78 MB | 0.2–3.3 s | 0.6–17 ms |
| 2 万字单文件（D-053 上限） | — | — | — |

`user/` 侧文档量由用户决定，无硬上限。若索引文档数超过 `INDEX_DOC_CAP`
（默认 20000），降级为**只索引标题与首段**并如实提示——
而不是继续吃内存直到进程被杀。

### 7.4 `services/tools/` 工具执行

工具的**定义**（名字、schema、谁能调、什么副作用）是一份**数据**；
**执行**是注册表里的一个处理器。数据化让"这份工具清单"可以打印给人看、
可以做权限静态分析、可以在调试模式导出成 OpenAI function schema。

#### 工具全集（D-042 已定，此处只补实现约定）

| 工具 | 可调角色 | 副作用类别 | 实现约定 |
|---|---|---|---|
| `shared.write(name, kind, content)` | 全体 | `PUBLISH` | 写公共区，登记 `Artifact` |
| `shared.read(name)` / `shared.list(kind)` | 全体 | `READ` | 只能读**已发布**的 |
| `kb.search(query, scope)` / `kb.read_pack(item)` | 全体 | `READ` | scope 限内容包与 `user/` |
| `kb.read_user_file(filename)` | 全体 | `READ` | 2 万字截断（D-053） |
| `msg.report_to_lead(...)` | 全体 | `SIGNAL` | 成员**唯一**的对外汇报通道（D-041） |
| `msg.request_from_lead(question, blocking)` | 全体 | `SIGNAL` | `blocking=True` 即进入"等待用户"态 |
| `draft.submit(content, version)` | 全体 | `PUBLISH` | 走草稿纸，计入 3 文件上限 |
| `validate.run(checker, args)` | 全体 | `PURE` | 见 §6.4 的边界声明 |
| `flow.status()` | **仅主理人** | `QUERY` | 数据供主理人与调试模式，**不进标准界面**（D-046） |
| `flow.sleep(reason)` | 全体 | `SUSPEND` | →"挂起"，**不再发任何模型请求** |
| `flow.wake_on(event, role)` | 全体 | `SUSPEND` | →"等待条件"，命中回"运行中" |
| `flow.ask_user(fields)` | **仅主理人** | `ASK_USER` | 弹结构化表单（D-040） |
| `flow.rollback(step, keep)` | **仅主理人** | `REWORK` | 保留 D-Card，重做指定步骤 |
| `flow.inject_directive(text)` | 仅主理人 | `SIGNAL` | 用户插话，转达为 `user_directive` |

**已砍除**（D-042，且代码里不允许复活）：`fs.list` / `fs.write` / `fs.delete`
及公共区外的一切文件读写。砍掉之后路径校验只需回答一个问题
——"能不能碰 `user/`"，不再需要拦截跨成员越界。这不是简化，是**把复杂度消灭在设计里**。

#### 权限模型

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    parameters: dict            # JSON Schema
    allowed_roles: frozenset[str]   # 角色白名单
    effect: Effect                     # PUBLISH / READ / SIGNAL / SUSPEND / ASK_USER / REWORK / PURE
    requires_publish: bool = False    # 产物必须经公共区
```

每次调用三重检查 + 一条审计记录：

1. **角色白名单** —— 调 `flow.ask_user` 的不是主理人 → 拒绝。
2. **路径白名单** —— 交给 `WorkspaceGuard`（见下）。
3. **草稿纸上限** —— 每人每次任务最多 3 个文件，超出自动清最早的（D-042）。
4. **审计** —— 工具名、角色、时间、参数（脱敏）、结果摘要、裁决，全部进 journal。

#### `WorkspaceGuard`：路径解析的唯一实现（D-076）

```python
def resolve_under(raw: str, root: Path) -> Path:
    """把任意输入解析为 root 内的真实路径；越界即抛 PathEscape。"""
    # normcase：Windows 上大小写不敏感，统一大小写后再比
    # realpath：解析符号链接与 Windows reparse point（junction）
    resolved = Path(os.path.realpath(os.path.normcase(str(raw))))
    base    = Path(os.path.realpath(os.path.normcase(str(root))))
    try:
        resolved.relative_to(base)      # is_relative_to 的语义，3.8+ 可用
    except ValueError:
        raise PathEscape(f"{raw} 越出 {root}")
    return resolved
```

**为什么 `abspath` 不够——这是可复现的越权，不是理论风险。**
实测：`user/evil.txt` 是指向 `/etc/passwd` 的符号链接时，

```
abspath(user/evil.txt) = …/user/evil.txt   → 判定"在 user/ 内" ✅ 放行 ❌
realpath(user/evil.txt) = /etc/passwd        → 实际指向系统文件      ❌
```

`abspath` 只做字符串拼接，**不解析文件系统**。本项目能读 `user/` 下任何文件，
用户放一个软链就等于读全盘——路径守卫必须以 `realpath` 为准。

Windows 上额外两个坑，本实现一并覆盖：

| 坑 | 处理 |
|---|---|
| 大小写不敏感（`User\` == `user\`） | `normcase()` 归一 |
| reparse point / junction（`mklink /J`） | `realpath()` 在 3.8+ 已解析 |
| NTFS `\\?\` 长路径前缀 | `normcase()` 会剥离 |

`realpath` 对**不存在的路径**也能安全工作（逐级解析已存在的部分），
所以**先校验后创建**的顺序是安全的——不存在文件无法通过检查，
而新建文件必然由程序自己在已校验的父目录下创建。

**必须有的测试用例**（缺一即视为守卫未实现）：

1. `user/evil.txt` → 指向 `/etc/passwd` 的软链 → **拒绝**
2. `user/normal.txt` 普通文件 → 放行
3. `user/../../etc` 路径穿越 → **拒绝**
4. `system/cfg.toml` 越权访问 → **拒绝**
5. Windows 上 `User\` 大小写变体 → 放行（在同一根下）
6. Windows 上 junction 指向根外 → **拒绝**

拒绝是**正常返回**不是异常：给模型一句"该角色无权调用此工具"，
让它换条路，而不是崩掉整个流程。

### 7.5 `services/observability/` 遥测

聚合作业：按"步骤类别"（起草 / 门禁 / 推演 / 装配）记录耗时与 token 均值，
落 `system/telemetry.json`。这是 D-031 耗时预估的**唯一**合法数据源。

---

## 8. 编排层（L5）★

编排是本项目的业务核心：把专家团那套契约变成可执行的状态机。

### 8.1 内核—驱动分离

```
┌─────────────────────────────┐
│  Kernel（纯同步状态机）       │
│  next() -> Event | None      │
│  不知道 TUI 存在             │
└──────────┬──────────────────┘
           │ 事件流
   ┌───────┼────────┬─────────────┐
   ▼       ▼        ▼             ▼
 TUI     无界面    测试 driver    doctor
(worker  (直接跑)  (假 LLM 跑)   (探测)
 线程)                          │
   │                            │
   └── 同一个 while 循环 ───────┘
```

**三种接入方式共用同一份驱动循环**（D-037）：

```python
def drive(kernel, projection):
    while (ev := kernel.next()) is not None:
        projection.handle(ev)
```

- TUI 跑这段循环在 **worker 线程**，通过 `call_from_thread` 把事件丢给 asyncio 侧。
  67 秒的模型请求不会冻住界面（D-024 实测已证明这个量级真实存在）。
- 无界面模式直接跑，投影换成标准输出。
- 测试用**脚本化假 LLM** 跑，能在零网络零成本下验证全流程状态迁移。

TUI 侧要做的桥接只有约 20 行，收益是**编排层 100% 可离线单测**。
这就是 D-037 "共用同一套编排内核，只换交互适配层"的准确含义。

### 8.2 并发模型：线程池，不用事件循环

D-049 要求 12 道门禁**并行**执行，压成 4 次调用。用
`concurrent.futures.ThreadPoolExecutor`（标准库、Windows 友好、无事件循环复杂度）：

- 内核是同步的，线程池只用于"同时发几个请求"这一个场景。
- D-024 实测非流式可撑 67 秒，网关无空闲超时——所以并发就是等，不需要流式配合。
- 不用 `asyncio` 是因为它会把编排层和 Textual 的 asyncio 世界绑死，
  破坏 §8.1 的"内核可脱离界面单测"。

### 8.3 阶段图

阶段与门禁的编排是**代码**，门禁细则与知识是**内容包**。这个切分很重要：
控制流在代码里（可测、可 diff），领域知识在内容包里（可编辑、可换版）。

```
Phase -1  D-Lock 方向锁 ──────────── 硬停车：问 D1–D9（D-040）
   │        降级出口：D3 用户拒答 → [待填·已告知]，导出时首行提示
   ▼
Phase 0   因果推演 ───────────────── 陈纲举《因果备忘录》（5 节点闭环推演）
   ▼
Phase 0.5 政策取证 ───────────────── 骆准绳；Flash 简单检索 / Pro 强制逐条留出处
   ▼
Phase 1   起草 ───────────────────── 辞达意；D7 硬约束不得沉降
   ▼
Phase 2   格式壳 + 合规标注 ───────── 骆准绳
   ▼
Phase 3   12 道门禁（并行 4 组，D-049）
   │  组1 骆准绳：G1 格式 / G2 合规 / G7 依据 / G10 政策
   │  组2 陈纲举：G5 结构 / G8 因果
   │  组3 辞达意：G4 语言
   │  组4 文择体：G3 文种（仅 Pro；Flash 由主理人研判并标注）
   │  主理人独占：DG-X 交付一致终闸 + G12 用户输入回看
   ▼
Phase 4   定稿格式化 ─────────────── 占位符扫描（D-034），未清零则**禁止导出**
   ▼
导出      主件 + 配套件（D-038），UTF-8 / GB18030（D-044）
```

**咨询档**不走这条线：仅范正章一人，不触发方向确认、不做政策检索、不走门禁，
产出为"有依据、有出处、给明确建议"的专家答复（D-035）。

### 8.4 档位是数据（D-067）

```python
TIERS: dict[str, TierSpec] = {
    "咨询":      TierSpec(roles=["范正章"], phases=[ADVICE], gate_groups=[]),
    "成文Flash": TierSpec(roles=["范正章","陈纲举","辞达意","骆准绳"], ...),
    "成文Pro":   TierSpec(roles=[全部5人], ...),
}
```

档位差异（人数、门禁范围、推理策略、提问字段、轮次上限）**全在数据里**。
新增一个档位 = 加一条 `TierSpec`，**编排代码零改动**。
轮次上限 60/12/14/16/20 也在数据里（D-058）。

### 8.5 成员状态机（D-042 六态）

```
        ┌──────┐
        │ 空闲 │  编排层尚未派发
        └──┬───┘
           │ 派发
           ▼
                    ┌──────────────────────────────┐
                    │           运行中              │◀──── 条件命中 / 用户已答
                    └──┬────────┬────────┬────────┘
             产物就绪  │        │ sleep  │ ask_user │ 超时/失败
                           ▼        ▼        ▼        ▼
              ┌────────────┐ ┌──────┐ ┌──────────┐ ┌──────────┐
              │ 完成/失败  │ │ 挂起 │ │ 等待用户  │ │ 等待条件 │
              └────────────┘ └──┬───┘ └────┬─────┘ └────┬─────┘
                     ▲          │        │            │
                     └──────────┴────────┴────────────┘
                              条件命中即回「运行中」

        终态「完成/失败」之后，该角色不再接收派发；
        若需继续，由编排层新建一次派发（六态机不含"完成态复活"）。
        「完成/失败」只能从「运行中」到达——挂起与两个等待态都不是终点。
```

四态之间可双向迁移（`运行中 ⇄ 挂起 / 等待用户 / 等待条件`），
因为一次任务里一个成员可能被反复唤醒多次；
但**唯一不可逆的边是进入「完成/失败」**。

两条不可违反的状态约束（D-042）：

1. `挂起` 状态下**绝不发起任何模型请求**。不空转、不轮询、不烧算力。
2. 产物就绪等场景**应主动 sleep + 注册条件唤醒**，而不是干等。
   D-049 的门禁并行正是复用这套机制，没有新增任何调度概念。

### 8.6 幂等语义与断点续跑（D-027）

这是架构里最容易做错、错了最难查的一处，所以单独设计。

**问题**：续跑时，如果某个工具调用"发布了产物、但状态还没落盘就崩了"，
重跑就会重复发布；如果不重跑，状态就永远缺一环。

**方案：效果记账 + 幂等重放。**

```
每个有副作用的工具调用带 call_id（由 语义唯一键 确定性生成，见下）
执行前：查 journal —— 该 call_id 是否已有记录？
  有 → 直接重放记录的结果，不重新执行
  无 → 执行 → 立即把结果摘要写 journal → 再更新 RunState
```

配合**原子写**（写临时文件 + `os.replace`）与 `schema_version`，
断点续跑就变成"从状态继续推进"，而不是"从某行代码继续执行"。
因为内核是状态机而不是脚本，所以续跑天然是安全的。

#### `call_id` 必须由语义唯一键生成（D-074）

```python
def call_id(kind: str, run_id: str, phase_id: str, *disc: str) -> str:
    """做什么 决定 id；第几次做 不决定。"""
    return sha256("|".join((kind, run_id, phase_id, *disc)).encode()).hexdigest()[:16]

# 写产物：同一阶段的同名产物，重试多少次都是同一个 id
call_id("publish", run_id, phase_id, artifact_name)
# 汇报：同一阶段的同一类汇报，天然幂等
call_id("report",  run_id, phase_id, role_id, report_kind)
```

**旧口径"角色 + 轮次 + 序号"是错的**，因为它把两个不同概念混在了一个键里：
它想表达"这是同一个副作用"，却用"这是第几次尝试"来编码。三处都会坏：

| 场景 | 旧口径后果 |
|---|---|
| 网络重试 3 次（D-027） | 轮次 +1 → call_id 变了 → **幂等失效，重复记账** |
| 模型不确定性（D-005） | 同一 prompt 重放，模型给出的工具调用顺序可能不同 → **序号不稳定 → id 不稳定** |
| 门禁退回重写（D-050） | 两次是不同产出，本就不该命中幂等——**这条恰好反证"轮次"语义上不是"同一件事"** |

语义键方案同时解决三者：重试必然命中幂等；退回重写因 `phase_id`
或 `report_kind` 不同而自然不命中；模型调用顺序变化不影响 id
（因为 id 不含序号）。实测确认：同一语义键重试 4 次 id 完全一致，
不同 phase/kind 各自得到不同 id。

**注意 `call_id` 与 `Event.seq` 是两回事**，不要混：
`seq` 回答"第几个发生的"，`call_id` 回答"是哪件事"。前者用于排序与回放，
后者用于幂等。回放时前者会原样重演，后者必须保持稳定——否则重放就会
变成"重新执行"，而幂等的前提正是重放不等于重做。

### 8.7 门禁处置（D-016 三档 + D-050 上限 + D-028 无绕过）

```
门禁失败
  ├─ 红线（E1/E3/E6/E8…）   → 退回重写，不放行，无任何绕过出口（D-028）
  ├─ 要素（要素缺失/D-Card 未回填）→ 退回 + 附缺失清单 + 弹表单问用户缺什么
  └─ 建议（措辞可优化）      → 不阻断，写 review-notes/ 存档，不向用户展示

同一门禁退回上限 2 次；第 3 次 → 主理人说明情况 + 列历次退回原因 → 请用户裁决（D-050）
```

**环境性缺失不走这条路**（gate-manual 的"反死循环铁律"）：无库 / 无网导致的
取证失败，必须**一次性升级用户决策**，不得按"退 2 次→用户裁决"空转——
反复退回本身就是设计缺陷。这条要写成代码里的显式分支，不能靠提示词记得。

**编制—门禁配平铁律**：某档含某门禁，其责任成员必须在该档编制内，
主理人不得代行取证。唯一例外是 G3（Flash 时降级给主理人，且成果里必须带
"文种由主理人研判"这一行，**要向用户展示**，因为文种判错整篇翻车）。

### 8.8 用户交互的三种形态（D-012）

| 形态 | 机制 | 关键约束 |
|---|---|---|
| 被动回答 | 硬停车 → 结构化表单 → 回填 D-Card | 不退出到命令行；答完自动续跑 |
| 主动插话 | `Ctrl+O` → 线程安全队列 → **下一个检查点**生效 | 不抢占正在进行的请求（不破坏上下文）；不重启流程；不丢弃已过的门禁 |
| 回退重写 | `flow.rollback(step, keep)` | 保留已定 D-Card，让指定角色重做某步 |

插话"在检查点生效"这件事，靠 §8.1 的驱动循环天然成立：
用户指令进队列，`kernel.next()` 在每个阶段边界检查一次并注入。
**不是靠定时器打断**，所以永远不会把请求拦腰砍断。

---

## 9. 导出与数据纪律

### 9.1 导出前的机械拦截（D-034，最高数据纪律）

导出前扫描产出文件：

| 检查 | 失败处置 |
|---|---|
| 残留占位符 `〔待填：…〕` `〔待核实：…〕` | **禁止导出**，列出待补信息 |
| 必填字段 `provenance != USER` 且无值（D4/D5/D7） | 禁止导出 |
| 未标注来源的数据型断言 | 禁止导出 |
| D-Card 降级字段 `degraded=True` | 允许导出，**交付说明首行醒目提示** |

这是"把提示词约束变成机械检查"的落地。拦截信息同时走
`export.blocked` 事件（`audience=USER`），所以标准界面也能提示。

### 9.2 编码（D-044）

`config.encoding.gb18030: true|false`，**默认 `false` 即 UTF-8**。
该开关**只影响产出文件**；日志与界面**恒为 UTF-8**，不受影响。
用 GB18030（而非 GBK）因为它是超集，生僻字不丢。

### 9.3 一次任务多份产出（D-038）

```
data/<run-id>/output/
  01-主件-<文种>-<标题>.txt      ← 唯一主件
  02-附件1-....txt
  03-附件2-....txt
```

界面在最终成果处**列出全部文件并标明哪个是主件**（D-038）。
用户拿到的是"一个项目的全部东西"，不是一个文件。

---

## 10. 可观测性的两个"不做"

架构设计最容易犯的错是把可观测性做成"显示一切"。这里明确**不做**：

1. **标准模式不显示成员状态**（D-046）。只显示一行总体进度：
   `第 3 步 / 共 7 步 · 正在起草正文 · 预计还需 2 分钟`。
   `flow.status()` 的数据供主理人内部使用与调试模式使用，**不进标准界面**。
2. **审核意见一律不向用户展示**（D-016 / D-030）。`review-notes/` 只在
   `data/` 里做内部存档，支撑调试追溯与回退重写。

判据很统一：**可观测性是给"排障"和"回退"用的，不是给用户看的消遣。**
用户要看的是四件事（D-023）：到哪一步了、还要多久、这阶段成果是什么、有没有出错。
标准模式给足这四件，多的一个字都不显示。

---

## 11. 错误与重试模型

### 11.1 错误分类

```python
class AppError(Exception):
    code: str              # 稳定标识，如 "PROVIDER_AUTH_FAILED"
    message_zh: str        # 给用户看的中文，D-020 标准严谨通用语言
    detail: str            # 给调试看，原始错误
    retryable: bool
    remediation: str       # 用户可以怎么做
```

**UI 永不显示原始异常文本**（D-020）。所有面向用户的文案走 `message_zh` + `remediation`，
且必须用标准中文，不生造术语。

### 11.2 三种失败，三种处置

| 失败 | 处置 | 决策 |
|---|---|---|
| 网络 / 服务端临时错 | **自动重试 3 次，间隔递增** | D-027 |
| 内容不合格（门禁） | 退回责任成员，**上限 2 次**，第 3 次请用户裁决 | D-050 |
| 红线不通过 | **无任何绕过出口**，必须修正 | D-028 |

**两者互不占用**（D-050 明文）：重试 3 次是网络失败，退回 2 次是内容返工。
这个区分必须体现在类型上——用两个不同的错误码，不是一个 `max_retries` 字段。

持续失败时明确告知三件事：**失败在哪一步、报了什么错、用户可以怎么处理**（D-027）。

---

## 12. 升级不破坏的六条机制

"升级无需破坏重构"不是态度，是工程约束。六条机制：

### 机制 1：端口隔离依赖

第三方库只在 L2。换 HTTP 库 = 新增一个 adapter + 改 `app.py` 一行。
换检索实现 = 新增一个 `RetrieverPort` 实现。**编排层不知道，也不需要知道。**

### 机制 2：版本化 schema + 前向容忍

每个会落盘的领域对象带 `schema_version`：

| 情况 | 行为 |
|---|---|
| 读到**更旧**版本 | 走迁移链升级（机制 3） |
| 读到**更新**版本 | 拒绝加载并给出清晰提示，**不崩溃、不猜** |
| 读到未知字段 | **忽略并记一条警告**（前向容忍） |

消费者一律用 `.get(key, default)`，**不许用 `[]`**。这条在代码评审清单里。

### 机制 3：迁移链（迁移是数据，不是代码）

```
system/migrations/
  0001_initial.py
  0002_add_artifact_kind.py
  ...
```

按序号顺序执行，**幂等**（跑两遍结果相同），执行记录写 `system/state.json`。
所以旧 `data/` 能被新程序升级打开，不需要用户手工处理。

### 机制 4：只加不改

- **事件**：只能加字段，不能改语义、不能删字段。
- **配置**：只能加键，不能改键名。改名 = 旧键保留 + 新键兜底 + 一次性迁移。
- **工具 schema**：只能加可选参数。改必填参数等于破坏契约。

### 机制 5：稳定 API 面

只有两个包是对外稳定面：

| 稳定面 | 承诺 |
|---|---|
| `zhengtong.domain` | 1 个大版本内只加不改 |
| `zhengtong.kernel` | 1 个大版本内只加不改 |

其余全部标注 `@unstable`（重构时允许改）。**限制爆炸半径**——
不是所有代码都需要长期兼容，只需要这两个。

弃用流程：`@deprecated(reason, since, remove_in="0.5.0")` → 启动时打警告
→ 至少保留一个 minor → 到点才删。**不静默移除**。

### 机制 6：契约测试作为升级门禁

```python
tests/test_layering.py      # 依赖方向
tests/test_schema_tolerant.py  # 旧 state/config/journal 能被新代码读
tests/test_port_contract.py    # 端口签名未变
tests/test_reexport_golden.py  # 同一输入导出结果逐字节一致
```

升级提交若让这四个测试变红，就是**真的破坏兼容**，不是"顺手重构"。

### 12.1 内容包解耦

内容包换版 = 换目录或改 `manifest.json` 版本号，**重启即生效**（D-018 不做热重载）。
`manifest.json` 记录名称、版本、内容哈希、依赖声明，并声明兼容区间：

```json
{ "name": "gongwen-core", "version": "1.2.0",
  "requires_zhengtong": ">=0.1,<0.3",
  "files": { "prompts/team-lead.md": "sha256:…" } }
```

程序启动时校验兼容区间与内容哈希。**内容包缺失 → 明确报错 + 恢复指引，
不从网络下载、不在用户目录重建**（D-014）。

这就实现了"提示词是可 diff、可回滚、可评审的一等资产"（D-008）：
改提示词不用改一行代码，也就不会有"提示词改动引入代码 bug"这类混合故障。

---

## 13. 模块树

```
src/zhengtong/
│
├── domain/                          L0 · 零依赖
│   ├── ids.py                       RunId / ArtifactId 的强类型（防传错）
│   ├── task.py                      TaskSpec
│   ├── tier.py                      TierSpec / TIERS（D-067 档位数据化）
│   ├── dcard.py                     DCard / DCardField / provenance
│   ├── artifact.py                  Artifact / ArtifactKind
│   ├── role.py                      RoleId / RoleState / 六态
│   ├── gate.py                      GateId / GateState / 责任映射（D-049）
│   ├── phase.py                     PhaseId / PhaseState
│   ├── event.py                     Event / EventKind / Audience / Level / Span
│   ├── errors.py                    AppError + 全部稳定错误码
│   └── run_state.py                 RunState（全量可持久化状态）
│
├── kernel/                          L1 · 零第三方依赖
│   ├── bus.py                       EventBus：脱敏 → 落盘 → 分发
│   ├── journal.py                   追加式 JSONL 日志
│   ├── redactor.py                  发射处脱敏
│   ├── projection.py                Projection 抽象 + Standard + Debug
│   ├── driver.py                    drive() 驱动循环（TUI/无界面/测试共用）
│   ├── scheduler.py                 线程池调度（D-049 并行）
│   └── result.py                    Result 风格返回（拒绝是正常返回不是异常）
│
├── ports/                           L3 · 纯抽象
│   ├── llm.py  content_pack.py  storage.py  user_files.py
│   ├── validators.py  secrets.py  clock.py  retriever.py
│   └── registry.py                  PortRegistry（唯一装配点）
│
├── adapters/                        L2 · 第三方库只在这里
│   ├── llm/
│   │   ├── openai_style.py          端点原样请求 / finish_reason 分流
│   │   ├── reasoning_probe.py       D-048 四写法探测（判 reasoning_tokens==0）
│   │   ├── error_map.py             服务端结构化错误 → 中文提示
│   │   └── usage.py                 token 统计
│   ├── content/local_pack.py        本地内容包 + manifest 校验
│   ├── storage/local_fs.py          原子写 / 符号链接解析 / 草稿纸 3 文件上限
│   ├── userfiles/
│   │   ├── scanner.py               自动扫描登记（D-022）
│   │   └── readers/{txt_md,docx}.py  docx 走 zipfile+xml，D-078 先查元数据
│   ├── validators/
│   │   ├── hard_constraint.py       移植 + 结构化入参（D-054）
│   │   └── evidence_anchor.py       同上
│   ├── secrets/toml_store.py        明文可存，但只出 SecretRef
│   ├── retriever/
│   │   ├── ngram.py                 中文 n-gram 倒排（主路径，D-073）
│   │   ├── tfidf.py                 加权 TF-IDF 打分（D-073）
│   │   └── fts5.py                  拉丁字符补充路径；探测不可用则跳过
│   └── clock/system_clock.py        默认真时钟
│
├── services/                        L4 · 业务能力
│   ├── prompting/
│   │   ├── assembler.py             提示词装配（按需装载 / 产物只传引用）
│   │   └── templates.py             占位填充（**不含公文内容**）
│   ├── context/
│   │   ├── view.py                  ContextView（视图，非累积列表）
│   │   ├── composer.py              按 D-025 五个手段组装
│   │   ├── compactor.py             85% 触发；D-039 保留优先级
│   │   └── estimate.py              D-025 实测基线 ÷1.6（D-077 分离纪律）
│   ├── retrieval/{indexer,search}.py  中文 n-gram 主路径
│   ├── tools/
│   │   ├── spec.py                  ToolSpec 数据定义
│   │   ├── registry.py              工具注册表
│   │   ├── guard.py                 WorkspaceGuard：realpath+D-076 六条用例
│   │   ├── handlers.py              14 个工具的实现
│   │   └── audit.py                 审计记录
│   ├── validators/runner.py        调 ValidatorPort
│   └── telemetry/ledger.py          耗时/token 台账（D-031 唯一数据源）
│
├── orchestration/                   L5 · 编排
│   ├── kernel.py                    状态机本体，实现 next()
│   ├── run.py                       一次运行的驱动与生命周期
│   ├── phases.py                    阶段推进（Phase -1 → 导出）
│   ├── gates.py                     门禁编排 + 并行分组 + 三档处置
│   ├── roles.py                     角色调度 + 六态 + 轮次上限
│   ├── checkpoint.py                原子落盘 + 续跑
│   ├── idempotency.py               call_id 语义唯一键效果记账（D-074）
│   ├── directive.py                 用户插话队列（检查点生效）
│   └── review.py                    review-notes 存档
│
├── export/                          独立小层
│   ├── guard_scan.py                占位符与编造扫描（D-034）拦截
│   ├── txt.py                       TXT 写出
│   ├── encoding.py                  UTF-8 / GB18030（D-044）
│   └── naming.py                    主件 + 配套件编号（D-038）
│
├── presentation/                    L6 · 只做翻译
│   ├── tui/
│   │   ├── app.py                   Textual App + worker 线程桥接
│   │   ├── widgets/                 进度行 / 成果卡 / 需求输入框 / 提问表单
│   │   ├── theme.py                 Unicode 制表符（D-043）
│   │   └── font_check.py            启动检测，只提示不阻止（D-045）
│   ├── headless/
│   │   ├── cli.py                   zhengtong run --档位 --需求
│   │   └── stdio_form.py            无人值守：硬停车从 stdin 读答案
│   └── doctor/                      D-024 四项 + D-048 探测 + 完整性
│
├── config/
│   ├── schema.py  loader.py  defaults.py  migrate.py
│
├── app.py                           ★ 唯一知道所有层的装配根
└── cli/main.py                      入口
```

**装配根纪律**：`app.py` 是**唯一**允许 import 全部层的地方。
其他任何文件 import L6 就是架构破坏（§3.4 测试会抓）。

---

## 14. 决策 → 机制 对照表（可审计）

| 决策 | 架构机制 | 节 |
|---|---|---|
| D-001 完全独立于 QwenPaw | **不 import 任何 QwenPaw 模块**；端口层使第三方依赖可枚举可替换（§2.3）；可移植打包见 D-019 行 | §2.3 §4 |
| D-002 / D-010 目录三分、数据与程序同目录 | 端口按职责而非按目录划分，目录只影响装配 | §13 |
| D-003 / D-032 Python、三依赖 | 第三方库只在 L2，依赖清单可审计 | §2.3 |
| D-004 端点原样请求 | `LLMPort` 不接受 URL 拼接意图，适配器原样转发 | §6.1 |
| D-005 工具调用是模型自主行为 | 工具是**数据**（ToolSpec）+ 注册表，模型选谁调由模型定 | §7.4 |
| D-055 规则在内容包、无不可用分支 | 去 AI 味规则是 `references/deai-rules.md`，由内容包装载器读，**不是可选模块**；G4 判定读它 | §6.4 §7.1 |
| D-059 / D-060 中文命名 | 标识符拼音英文、界面与文档中文；中文子模块直接连写不加连字符 | 附录 B |
| D-006 提示词改写授权与边界 | 提示词全在内容包，代码只做占位填充 → 改写不碰一行代码 | §7.1 |
| D-056 references 是知识库不是提示词 | 知识与流程分离：角色提示词在 `prompts/`，门禁细则在 `references/`，由装配器按需取用 | §7.1 |
| D-007 许可证不作为设计约束 | 取舍一律以可维护性/可升级性/质量优先，故拒绝"接入层直连服务"这类便利妥协 | §2.3 §2.4 |
| D-047 内容包只放改写版 | 无"双版本运行时切换"分支 → `ContentPackPort` 只需 `read`/`list`，无 variant 参数 | §4.1 §12.1 |
| D-009 放松版面、内容要素不少 | 导出只保内容要素，版面要求不进代码（`export/` 无版式逻辑） | §9 |
| D-019 便携 zip、Win11 + Linux、全中文 | 依赖可枚举（§2.3）；路径与终端差异全在 L2/L6；文案走 `message_zh` | §2.3 §11.1 |
| D-008 软数据与代码平级 | 事件 schema、配置、内容包都版本化可 diff | §12 |
| D-011 草稿纸 + 公共交换区 | `ArtifactStorePort` 两类写入，草稿纸 3 文件上限是硬计数 | §7.4 |
| D-012 双模式可观测性 | 事件流 + 两个投影，边缘过滤 | §5.2 |
| D-012 插话在检查点生效 | 指令队列 + `next()` 阶段边界检查，天然不打断请求 | §8.8 |


| D-013 检索方案（**D-073 修订主次**） | `RetrieverPort` + 自建中文 n-gram 为主路径，FTS5 降为拉丁补充 | §7.3 |
| D-014 内容包 | manifest + 兼容区间 + 哈希校验 | §12.1 |
| D-015 借鉴不抄代码 | 只提取机制，每条设计标注决策编号，无依据不写 | 全文 |
| D-016 门禁三档处置 | 判定枚举 + 退回/存档分支，红线无绕过 | §8.7 |
| D-017 无持久记忆可清空重建 | 全部派生文件可从内容包与 `user/` 重建，启动自检重建三目录 | §9.3 §7.3 |
| D-018 不热重载 | 内核启动时一次性装配端口，配置只读 | §8.1 |
| D-020 用语规范 | `AppError.message_zh` 强制中文；UI 不显示原始异常 | §11.1 |
| D-021 三档用户自选 | `TIERS` 数据驱动，不自动判断不给建议 | §8.4 |
| D-022 user 自动扫描 | `UserFilePort.scan()` 适配器层自动完成，只给文件名 | §6.3 |
| D-023 两种运行模式 | 同一 `drive()` 循环，两个投影 | §5.2 §8.1 |
| D-024 连接检测四项 | `LLMPort.probe(kind)`，独立成方法 | §4.2 |
| D-025 上下文管理 | ContextView 视图模型 + 85% 兜底 + 五个主动手段 | §7.2 |
| D-026 不流式 / 温度 0.3 | 同步阻塞调用，内核跑 worker 线程 | §1.1 §8.1 |
| D-027 重试 3 次 + 断点续跑 | 幂等效果记账 + 原子落盘 + 状态机续跑 | §8.6 |
| D-028 红线无绕过 | 门禁返回值类型上就没有"强制通过"这个可能 | §8.7 |
| D-029 三档编制 | `TierSpec.roles` 数据 | §8.4 |
| D-030 阶段成果三处 | 投影白名单只放三类事件 | §5.4 |
| D-031 耗时预估不编造 | 遥测台账唯一数据源，无历史则显示"暂无法估计" | §7.5 |
| D-032 支持格式 | 三个 reader，不解析的只登记不静默丢 | §6.3 |
| D-033 单一多行输入框 | `TaskSpec.requirement_text` 一个字段，无分类 | §3.2 |
| D-034 严禁编造 | `DCardField.provenance` 结构化强制 + 导出前机械扫描 | §3.3 §9.1 |
| D-035 咨询模式边界 | `TierSpec.phases=[ADVICE]`，不挂门禁 | §8.4 |
| D-036 GPL-3.0 许可 | 依赖集中在 L2，清单可枚举可审计（§2.3）；每条依赖的许可在 `pyproject.toml` 声明 | §2.3 |
| D-037 无界面模式 | 同一内核换驱动，不是另一套代码 | §8.1 |
| D-038 多文件产出 | `output/` 编号命名 + 界面标主件 | §9.3 |
| D-061 六层依赖方向 | §2.1 分层图 + §2.3 单条依赖规则 + §2.4 反模式清单 | §2.1 §2.3 |
| D-062 单一事件流 | 全系统唯一 `EventBus`，边缘过滤分发两个投影 | §5.2 |
| D-063 内核纯同步状态机 | `Kernel.next() -> Event \| None`，不知道 TUI 存在 | §8.1 |
| D-064 门禁线程池并行 | 12 道压成 4 次调用；事件经队列由泵线程单点发射（D-075） | §8.2 §5.3 |
| D-065 幂等效果记账 | `call_id` 语义唯一键 + journal 前置查重 + 原子落盘（D-074） | §8.6 |
| D-066 六条升级不破坏机制 | 端口隔离 / 版本化 schema / 迁移链 / 只加不改 / 稳定 API / 契约测试 | §12 |
| D-067 档位是数据 | `TierSpec` 的 roles/phases/gates 字段，非硬编码分支 | §8.4 |
| D-068 ContextView 五元视图 | 全 `tuple` 真不可变；窗口滑动在 composer 而非视图 | §7.2 |
| D-069 provenance 结构化 | `DCardField.provenance: USER \| LEAD_INFERRED` + 导出前扫描 | §3.3 §9.1 |
| D-070 接入层不直连 + 只传值对象 | `test_layering` + `test_boundary` 强制，违反视同违反 D-066 | §3.4 §3.5 |
| D-071 分层测试管"谁能造对象" | 测试断言注入点，不止断言 import 方向 | §3.4 |
| D-072 阶段成果独立事件 | `phase.artifact_ready` 携全文；白名单只放行它而非整个 `phase.*` | §5.4 |
| D-074 call_id 语义唯一键 | sha256(kind\|run\|phase\|disc)；与 `Event.seq` 职责分离 | §8.6 |
| D-075 EventBus 单点发射 | 队列投递 + 泵线程分配 seq，顺序与状态机推进一致 | §5.3 |
| D-076 realpath 路径守卫 | `realpath`+`normcase`+`relative_to`；六条用例为必过测试 | §7.4 |
| D-077 token 估算/真实分离 | 触发用保守估算（D-025 基线）；展示与台账用真实值 | §7.2 |
| D-078 docx 元数据先查 | 单 entry 20MB / 总量 100MB / 压缩比 100:1 三重上限 | §6.3 |
| D-079 最低 Python 3.11 | `tomllib` 内置故不装 `tomli`；`tomli-w` 仅用于写 | §2.3 |
| D-039 压缩保留优先级 | `ContextView.pinned_facts` 物理不可丢 | §7.2 |
| D-040 D1–D9 字段 | `DCardField` 逐字段带 provenance 与采集时机 | §3.2 |
| D-041 独立上下文 | 每角色一份 `ContextView`，成员唯一出口是工具 | §7.2 §7.4 |
| D-042 工具层与六态 | ToolSpec 数据 + 六态状态机 + 双重白名单 | §7.4 §8.5 |
| D-043 终端字体 | 界面层 `theme.py`，核心不碰字体 | §13 |
| D-044 编码 | 只在 `export/encoding.py` 一处分支 | §9.2 |
| D-045 启动检测不阻止 | `font_check.py` 在接入层，失败仅提示 | §13 |
| D-046 不显示成员状态 | `audience=USER` 白名单不含 `role.*` | §5.4 §10 |
| D-048 推理探测 | `reasoning_probe.py` 判 `reasoning_tokens==0` 而非状态码 | §6.2 |
| D-049 门禁并行 | `TierSpec.gate_groups` + 线程池，复用 sleep/wake | §8.2 §8.3 |
| D-050 退回上限 2 次 | 与网络重试**不同错误码**，类型上分离 | §11.2 |
| D-051 max_tokens 从宽 | 适配器 `finish_reason` 分流 | §6.1 |
| D-052 推理不入历史 | 适配器层丢弃 `reasoning_content` | §6.1 |
| D-053 单文件 2 万字 | reader 层截断 + 事件标明 | §6.3 |
| D-054 校验脚本取舍 | 返回类型叫 `ValidationResult` 不叫 `GateVerdict` | §6.4 |
| D-057 E10–E14 归位 | 门禁责任映射表在 `domain/gate.py` | §3.2 |
| D-058 轮次上限 | `TierSpec` 内的数据，非硬编码 | §8.4 |

**没有单独列行的决策**：**D-061 ~ D-072** 共 12 条——
它们是本轮架构设计**自身产生**的决策，其机制即本文 §2 ~ §16 各节，不自引。
**D-001 ~ D-060 共 60 条全部有对应机制**（D-012 因含两项独立约束分两行列出，
故表中 59 行覆盖 60 条决策）。

**审计方法**：本表可机械校验——
```bash
sed -n '/^## 14\./,/^## 15\./p' ARCHITECTURE.md | grep -oE '^\| (D-[0-9]{3})' | sort -u
grep -oE '^### D-[0-9]{3}' DECISIONS.md | sort -u
```
两表取差，**结果应恰好是 D-001 ~ D-060**。
若多出任何一条，说明有决策忘了登记机制；若为 0，说明有本轮新决策被误列。

> **校验注意两种情况**：
> ① 一行可能声明多条决策（如 `| D-002 / D-010 ... |`），
>    所以不能只取行首第一个号，要取该行**全部** `D-xxx`。
> ② 机制说明里会**引用**其他决策号，
>    所以只统计表格**首列**的号，不统计"架构机制"列里出现的号。
>
> ```bash
> # 取表格首列（第一个 | 与第二个 | 之间）的所有决策号
> sed -n '/^## 14\./,/^## 15\./p' ARCHITECTURE.md \
>   | grep -E '^\| ' | cut -d'|' -f2 | grep -oE 'D-[0-9]{3}' | sort -u
> ```
>
> 如此得到的应是 **D-001 ~ D-060 全部 60 条**，不多不少。

---

## 15. 实现顺序

严格按依赖方向自底向上，**每层先有测试再接上层**。

**端口层（第 2 步）必须排在领域层行为（第 1b 步）之前**，
因为端口签名大量使用领域类型——不先有类型就没什么可签。
但它只需要领域的**形状**（字段定下来即可），不需要完整行为。
所以第 1 步拆成"定形状"与"补行为"两段。

| 步 | 交付 | 依赖 | 完成判据 |
|---|---|---|---|
| **1a** | L0 领域层**形状**：类型、字段、`schema_version` | — | 只用标准库；全部 `frozen` + 可序列化 |
| **2** | L3 端口层：用 1a 的形状签接口 | 1a | 签名不含任何第三方类型；`PortRegistry` 字段齐全（含 `retriever`） |
| **1b** | L0 领域层**行为**：校验、默认值、往返测试 | 1a, 2 | JSON 往返测试全绿；`ContextView` 全 `tuple` 真正不可变 |
| **3** | L1 内核层：事件总线 + 两投影 + 脱敏 | 1b | 伪造密钥断言不入日志；`phase.artifact_ready` 载全文 |
| **4** | 装配根 `app.py` + 一套全假实现 | 1b, 2, 3 | **能装配并跑起来**（此时 L4/L5 尚无，全假即可） |
| **5** | L2 适配：配置与日志 | 4 | 原子写、符号链接解析、启动自检重建三目录（D-017） |
| **6** | L2 适配：模型连接 | 5 | D-024 四项 + D-048 探测跑通真实端点 |
| **7** | L2 适配：文件读取 | 5 | txt/md/docx 读通；2 万字截断（D-053）；**docx zip bomb 元数据拦截（D-078）** |
| **8** | L4 检索 | 5 | 中文 n-gram 倒排 + 加权 TF-IDF 主路径（D-073）；top-k 召回 ≥95%；FTS5 仅拉丁补充 |
| **9** | L4 上下文管理 | 3, 5 | 85% 触发压缩；D-039 四类绝不丢弃有测试；**跨层只传值对象**（§3.5） |
| **10** | L4 工具层 | 3, 5 | 14 个工具 + 双重白名单 + 审计 + 草稿纸 3 文件上限（D-042） |
| **11** | L4 提示词装配 | 8, 9 | 按需装载与结果直传可验证（上下文占用可测） |
| **12** | **内容包**（**与 6–11 并行**） | — | 5 份角色提示词改写完成；门禁细则 / 红线 E1–E14 / D-Card **零丢失** |
| **13** | L5 编排 | 4–11 | 脚本化假 LLM 跑通咨询 / Flash / Pro 三条全流程 |
| **14** | L5 门禁 + 断点续跑 | 13 | 并行 4 组（D-049）；退回上限 2 次（D-050）；幂等重放（D-065） |
| **15** | 导出层 | 13 | 占位符与编造扫描拦截（D-034/D-069）；UTF-8/GB18030（D-044）；主件+配套件（D-038） |
| **16** | L6 接入层 | 13–15 | TUI + 无界面 + doctor 三驱动同源（D-063） |
| **17** | 跨平台验证 | 全部 | Win11 x64（家庭版/中文版）+ Linux；便携 zip；删目录自检；**实测三档耗时填入界面**（D-031） |

**第 13 步是第一个真正的里程碑**：在此之前全部是基础设施，
在此之后整条生产线才能被端到端验证。所以第 12 步（内容包）必须在第 13 步之前。

**双轨并行**：第 12 步（内容包改写，纯文本工作）与第 6–11 步（代码工作）
**互不依赖**，可由两方同时推进。这是当前最大的并行机会。

**贯穿全程的测试门禁**（第 2 步建目录、第 4 步跑起来）：

| 测试 | 断言 | 依据 |
|---|---|---|
| `test_layering` | 依赖方向 + **除 `adapters/` 与 `app.py` 外无人可构造适配器** | §3.4 |
| `test_boundary` | 跨层只传值对象；服务层不持有编排层状态 | §3.5 |
| `test_schema_tolerant` | 旧 `state`/`config`/`journal` 能被新代码读；未知字段不炸 | D-066 |
| `test_port_contract` | 端口签名未变 | D-066 |
| `test_reexport_golden` | 同一输入导出结果逐字节一致 | D-066 |

**这五个测试变红 = 真的破坏架构或兼容，不是"顺手重构"。**

---

## 16. 未决与风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 提示词改写可能丢失契约 | **最高风险**。门禁细则、红线、D-Card 是质量底线 | 改写必须逐条比对"不可丢清单"，做成检查表；第 12 步设独立验收 |
| FTS5 在目标 Python 发行版未编译 | **影响已降为轻微** | 中文主路径是自建 n-gram，不依赖 FTS5；启动探测，仅拉丁检索能力可能缺失并如实告知（D-073） |
| 索引内存随文档量增长 | 5 千篇约 24–78MB；畸形输入可能更大 | 超过 `INDEX_DOC_CAP`（2 万篇）降级为只索引标题与首段，并提示（D-073） |
| `user/` 内软链指向系统文件 | 越权读取 | `WorkspaceGuard` 强制 realpath+normcase，六条用例为必过测试（D-076） |
| 畸形/恶意 `.docx` 撑爆内存 | 进程 OOM | 先查 `ZipInfo` 元数据（单 entry/总量/压缩比三重上限），再决定是否读（D-078） |
| 真实端点行为与实测不同 | 推理关闭、max_tokens 策略失效 | 全部改成**探测式**而非硬编码；能力落 `system/caps.json` |
| 阶段图复杂度超预期 | 状态机难维护 | 阶段是数据（`TierSpec`），新增阶段不改状态机骨架 |
| 门禁并行时上下文争用 | 可能触发压缩 | 每角色独立 `ContextView`（D-041），争用不存在 |
| 无头模式遇硬停车 | 无人值守场景卡住 | 从 stdin 读表单答案（D-037 已定） |
| **架构规则被绕过**（直连服务 / 编排层 `new` 适配器 / 跨层传整个状态） | **最隐蔽的风险**。三种退化都能通过功能测试，只有结构测试能拦 | 已由 `test_layering` + `test_boundary` 强制（§3.4 §3.5）；代码评审时逐条对照 §2.4 清单打钩 |
| **架构文档与实现漂移** | 文档说的和代码做的不一致，三个月后无人可信文档 | 改架构先改本文再改码；§14 对照表是审计入口，改动后重跑一次覆盖检查 |
| 测试门禁被注释掉以赶进度 | 唯一有效的防线失效，且很难被发现 | 门禁测试与产品代码同仓、同 CI、**任何人不得注释**；违反视同违反 D-066 |

**风险表随文档修订同步更新**：本表已随 2026-10-01 首次自检修订，
新增后三行（其中"架构规则被绕过"正是首轮自检发现并修复的三处问题所对应的风险）。

**刻意不做的**（避免范围膨胀，理由充分）：
持久稿件库、热重载、向量检索、PDF/OCR/识图、流式输出、首次运行引导教学、
成员互相直连、跨成员草稿纸共享、自动判断档位。

---

## 附录 A · 稳定 API 面

```python
# 稳定面（1 个大版本内只加不改）
from zhengtong.domain import TaskSpec, DCard, Artifact, Event, AppError
from zhengtong.kernel import EventBus, drive, StandardProjection

# 非稳定面（重构时允许改，请勿依赖）
from zhengtong.orchestration import ...
from zhengtong.services import ...
```

## 附录 B · 命名

`政通` 为产品名（D-059）。中文子模块按 D-060 约定直接连写、不加连字符：
`政通·格式`、`政通·合规`、`政通·起草`。
代码标识符一律用拼音英文（`zhengtong`），界面与文档一律用中文。
