"""全假端口实现（第 4 步）。

**存在的理由**：架构 §15 要求"能装配并跑起来"时 L4/L5 尚无。
没有这套假实现，就无法验证装配根本身是否正确——
而装配错误（少一个端口、签名不匹配、依赖方向搞反）
恰恰是这类项目最容易犯且最难在真适配器里发现的错。

**诚实是本文件的第一原则**：

- 假实现**不假装成功**。凡未真正实现的路径一律返回 `Result.fail`，
  绝不返回 `ok`。一个会骗人的假实现比没有更坏——
  它让"跑起来了"变成假象，第 5 步接真适配器时炸在更远的地方。
- 桩数据**必须可辨认**（内容里明写"桩"字），出问题时一眼看出数据来自假实现
  而不是真模型，避免把桩数据误当真实结果交付出去。
- 每个类都声明 `: <Protocol>`，由类型检查器保证签名一致，
  而不只是 `isinstance` 的运行时擦边。

**零第三方依赖**，与 L1 一致：本文件属于内核侧的测试支撑，不引入 httpx/textual。
"""

# 本文件放宽 Unknown 类规则，理由同 domain/errors.py 的 D-085：
# `Result.ok(...)` 的返回类型是 `Result[Any]`（三方案实测后确认唯一可行解），
# 故各端口方法的返回标注为 Result[Unknown]。只影响静态提示，不影响运行行为。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnusedParameter=false
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..domain.artifact import ArtifactRef
from ..domain.errors import ErrorCode, Result
from ..domain.ids import RunId
from ..domain.role import RoleId
from ..domain.run_state import RunState
from ..domain.task import UserFile, 读到的文件
from ..ports.clock import ClockPort
from ..ports.content_pack import ContentPackPort
from ..ports.llm import (
    FinishReason,
    LlmPort,
    LlmRequest,
    LlmResponse,
    TokenUsage,
)
from ..ports.retriever import RetrieverPort, SearchHit, 文档条目
from ..ports.secrets import SecretsPort
from ..ports.storage import StoragePort
from ..ports.user_files import UserFilesPort
from ..ports.validators import ValidationIssue, ValidationResult, ValidatorPort
from . import paths

#: 桩内容统一前缀。任何含它的文本都一眼可知不是真产物。
STUB_MARK = "【桩】"


def _assert_protocols() -> None:
    """**在导入时**校验全部假实现满足对应 `Protocol`。

    这一句是本文件的核心价值：`Protocol` 的签名不一致**必须在导入时炸**，
    而不是等到某个分支第一次调它才炸——那时可能已跑了几十步、
    甚至已经写过日志。

    用 `isinstance` 而非类型标注：Protocol 的结构兼容性无法靠标注表达，
    `runtime_checkable` + isinstance 才是官方给的结构校验手段。
    此时每个类型参数都已收窄为具体类，故 isinstance 无 Unknown 告警。
    """
    pairs: tuple[tuple[object, type[object]], ...] = (
        (FakeClock(), ClockPort),
        (FakeSecrets(), SecretsPort),
        (FakeLlm(), LlmPort),
        (FakeStorage(), StoragePort),
        (FakeUserFiles(), UserFilesPort),
        (FakeContentPack(), ContentPackPort),
        (FakeRetriever(), RetrieverPort),
        (FakeValidators(), ValidatorPort),
    )
    for impl, proto in pairs:
        if not isinstance(impl, proto):  # pyright: ignore[reportUnnecessaryIsInstance]
            missing = [m for m in dir(proto) if not hasattr(impl, m)]
            raise TypeError(f"假实现未满足协议 {proto.__name__}，缺：{missing}")


def stub_text(kind: str) -> str:
    """造一段可辨认的桩文本。"""
    return f"{STUB_MARK}{kind}（假实现产出，非真实内容，不得用于交付）"


# ── Clock ──


@dataclass(slots=True)
class FakeClock:
    """假时钟。**时间可推进**，故压缩/超时逻辑可被确定性测试。"""

    t0: float = 1_700_000_000.0
    now_value: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.now_value = self.t0

    def now(self) -> float:
        return self.now_value

    def sleep(self, seconds: float) -> None:
        """推进时间而不是真睡——测试里真睡只会让套件变慢。

        **负数不是"倒着睡"，是编程错误**：真实现里 `sleep(-1)` 会挂起，
        而这里若照单全收，事件时间戳会倒退，
        破坏 `ts_monotonic` 的单调性假设。
        故显式拒绝，而不是让测试悄悄拿到一个倒退的时钟。
        """
        if seconds < 0:
            raise ValueError(f"假时钟不接受负数睡眠：{seconds}")
        self.now_value += seconds

    def stamp(self) -> str:
        return f"1970-01-01T00:00:{int(self.now_value) % 60:02d}Z"


# ── Secrets ──


@dataclass(slots=True)
class FakeSecrets:
    """假密钥源。

    `redaction_patterns()` 返回**片段**而非完整 key——
    这与 `Redactor` 的设计一致：脱敏器自己不持有密钥全文，
    它自己的日志里也不可能出现密钥（D-030）。
    """

    api_key: str = f"{STUB_MARK}fake-key-not-a-real-secret"
    #: 额外提供的脱敏片段，供 Redactor 认领
    patterns: tuple[str, ...] = ()

    def get_api_key(self) -> str:
        return self.api_key

    def redaction_patterns(self) -> tuple[str, ...]:
        if self.patterns:
            return self.patterns
        # 取中间一段作片段：既能命中，又不把整把 key 交给脱敏器
        mid = self.api_key[len(self.api_key) // 4 : len(self.api_key) // 2]
        return (mid,) if mid else ()


# ── LLM ──


@dataclass(slots=True)
class FakeLlm:
    """假模型。

    **只回固定桩文本，不做任何推理。** 它让第 4 步能验证"装配与驱动跑得通"，
    但它**不能**用来验证公文质量——那是接上真模型之后的事。
    """

    #: 可编排的响应队列；用尽后回落到 `default_text`
    scripted: list[LlmResponse] = field(default_factory=list)
    default_text: str = stub_text("模型回复")
    #: 记录收到的请求，供测试断言编排层确实传对了东西
    calls: list[LlmRequest] = field(default_factory=list)
    fail_with: ErrorCode | None = None
    #: 脚本用尽后是否报错。默认 `False` 保持第 4 步「跑得通」的宽松行为，
    #: 但**测试该覆盖这两种模式**——见 `strict_script`。
    strict_script: bool = False

    def complete(self, request: LlmRequest) -> Result[LlmResponse]:
        self.calls.append(request)
        if self.fail_with is not None:
            # 真的失败，不假装成功
            return Result.fail(self.fail_with, "FakeLlm 被要求失败")
        if self.scripted:
            return Result.ok(self.scripted.pop(0))
        if self.strict_script:
            # **假实现不得比真实现宽松**：队列用尽却静默给桩文本，
            # 会让编排层「多发一次请求」这种真 bug 在假环境里照跑通过，
            # 接上真模型才炸。宁可当场失败。
            return Result.fail(
                ErrorCode.LLM_REQUEST_FAILED,
                f"FakeLlm 脚本已用尽（第 {len(self.calls)} 次调用），拒绝静默回落",
            )
        return Result.ok(
            LlmResponse(
                text=self.default_text,
                finish_reason=FinishReason.STOP,
                usage=TokenUsage(prompt_tokens=10, completion_tokens=10),
                call_id=_semantic_call_id(request),
            )
        )

    def probe(self) -> Result[TokenUsage]:
        return Result.ok(TokenUsage(prompt_tokens=1, completion_tokens=1))


# ── Storage ──


@dataclass(slots=True)
class FakeStorage:
    """假存储。**纯内存**，不碰文件系统——内核层本就无权碰盘。

    ## 审计 D-128：假实现也必须做目录权限检查

    原实现直接 `self.files[rel_path] = content`，**不检查路径**。
    这让假实现比真实现**宽松**，直接违反 D-112
    （「假实现的价值在于行为与真实现一致，一旦更宽松，
    它就不再是可信的参照物，而是误导源」）。

    后果很具体：编排层若在假环境里对 `user/` 发起写入
    （越权，是真 bug），假环境照跑通过；
    接上真适配器才炸，而失败点在编排层，看不出根因在假实现。

    故补上与 `FsStorage` 相同的顶层目录判断。
    差别只有一处，且是**不可不认的**：
    真实现判的是**软链接解析后的真实落点**，
    假实现没有文件系统、无法解析软链接，故只能判**字符串路径**——
    软链接越权（D-126）这一类**只在真适配器上可测**，
    假实现做不到，也不假装能做到。
    """

    files: dict[str, str] = field(default_factory=dict)
    states: dict[str, RunState] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    #: 是否模拟"正文已达标"（给 `draft_paper_ok` 用）
    draft_ok: bool = False

    def write_atomic(self, rel_path: str, content: str) -> Result[None]:
        got = _查权限(rel_path, paths.AI_WRITABLE)
        if got.is_error:
            return got
        self.files[rel_path] = content
        return Result.ok_none()

    def read_text(self, rel_path: str) -> Result[str]:
        got = _查权限(rel_path, paths.AI_READABLE)
        if got.is_error:
            # **原样搬运错误码，不改写**（审计 D-129）。
            # 曾在这里把 PATH_ESCAPE 降级成 FILE_UNREADABLE，理由是
            # "读不到文件"，但两者对调用方意义完全不同：
            # PATH_ESCAPE 是"你不该看这个"，FILE_UNREADABLE 是"文件不在"。
            # 混为一谈会让上层把**权限问题当成缺文件**去补材料，
            # 或者干脆当成偶发错误重试——越权被当成了偶发。
            err = got.error
            assert err is not None, "is_error 为真时 error 必非空"
            return Result.fail(err.code, err.detail)
        if rel_path in self.files:
            return Result.ok(self.files[rel_path])
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"文件不存在：{rel_path}")

    def exists(self, rel_path: str) -> bool:
        """**审计 D-142：这里也必须查权限。**

        原实现是 `return rel_path in self.files`，**不做任何权限检查**——
        于是对 `../../etc/passwd` 也返回 `True`，而真实实现在同一输入上
        返回 `False`（它走 `_resolve_for_read`，越权即否）。

        后果很具体：上层若据此判断「文件已就绪，可以读取」，
        在假环境里照跑通过，接上真适配器才发现读不到——
        **失败点在上层，看不出根因在假实现**。
        这正是 D-112 说的「假实现一旦更宽松，就不再是可信参照物」。

        返回类型是 `bool` 而非 `Result`，故越权时只能是 `False`——
        与真实实现在**同一输入上给出同一答案**，这才是关键。
        """
        got = _查权限(rel_path, paths.AI_READABLE)
        if got.is_error:
            return False
        return rel_path in self.files

    def list_dir(self, rel_path: str) -> tuple[str, ...]:
        """列条目名。**只取名字、不带子路径、不递归**（端口契约）。

        审计中发现的偏差：原实现返回 `子路径/文件名`
        （如 `("深层/x.txt",)`），而真实实现用 `p.name` 只返回 `("深层",)`。

        **这类"假实现比真实现宽松"的偏差最危险**：
        上层在假环境里照跑通过，接上真适配器立刻失败——
        而且失败点在上层，看不出根因在这里。

        越权或不存在都返回空元组，与真实实现一致（见 fs_storage.list_dir）。

        ## 审计 D-142：这里原本也**没有权限检查**

        原实现直接按前缀匹配，于是
        `list_dir("../../etc")` 在假实现上返回 `("x.txt",)`，
        真实实现返回 `()`。同样是「假实现比真实现宽松」。

        对照 `read_text` 已经补过的权限检查（D-128/D-129）——
        当时修了**会返回 Result 的方法**，而 `exists` / `list_dir`
        返回的是 `bool` 与 `tuple`，被漏掉了。

        **教训：补一处只补返回 `Result` 的那类，是不完整的。**
        返回裸值的便捷方法同样是契约的一部分，
        上层照样会拿它们的返回值做判断。
        """
        got = _查权限(rel_path, paths.AI_READABLE)
        if got.is_error:
            return ()
        prefix = f"{rel_path.rstrip('/')}/"
        names = {k[len(prefix) :].split("/", 1)[0] for k in self.files if k.startswith(prefix)}
        return tuple(sorted(names))

    def save_run_state(self, state: RunState) -> Result[None]:
        self.states[state.run_id] = state
        return Result.ok_none()

    def load_run_state(self, run_id: RunId) -> Result[RunState]:
        if run_id in self.states:
            return Result.ok(self.states[run_id])
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"无此 run 的状态：{run_id}")

    def put_artifact(self, ref: ArtifactRef, content: str) -> Result[None]:
        self.artifacts[str(ref)] = content
        return Result.ok_none()

    def draft_paper_ok(self, role: str, current_count: int) -> bool:  # noqa: ARG002
        return self.draft_ok


# ── User files ──


@dataclass(slots=True)
class FakeUserFiles:
    """假用户文件源。

    **只给文件名与元信息，不给内容**（D-022/D-023）：
    AI 初始只获得文件名，内容必须经 `read_text` 按需取。
    这里刻意保留该约束，否则假实现会比真实现宽松，
    上层就会在假环境里"跑通"、接真适配器后立刻失败。
    """

    registered: tuple[UserFile, ...] = ()
    contents: dict[str, str] = field(default_factory=dict)

    def scan_and_register(self) -> tuple[UserFile, ...]:
        return self.registered

    def list_registered(self) -> tuple[UserFile, ...]:
        return self.registered

    def read_text(self, name: str) -> Result[读到的文件]:
        if name in self.contents:
            文本 = self.contents[name]
            return Result.ok(
                读到的文件(
                    text=文本,
                    已截断=False,
                    完整字数=len(文本),
                    原始字数=len(文本),
                )
            )
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"文件不存在：{name}")


# ── Content pack ──


@dataclass(slots=True)
class FakeContentPack:
    """假内容包。返回桩提示词。"""

    version_value: str = "fake-0.0.0"

    def load_role_prompt(self, role: RoleId) -> Result[str]:
        return Result.ok(stub_text(f"{role.value} 的角色提示词"))

    def load_reference(self, ref: str) -> Result[str]:
        return Result.ok(stub_text(f"参考 {ref}"))

    def verify_manifest(self) -> Result[None]:
        return Result.ok_none()

    def version(self) -> str:
        return self.version_value


# ── Retriever ──


@dataclass(slots=True)
class FakeRetriever:
    """假检索器。返回空结果而非编造命中——编造召回结果会掩盖检索层的问题。"""

    hits: tuple[SearchHit, ...] = ()
    built_with: tuple[文档条目, ...] = ()

    def search(self, query: str, top_k: int = 8) -> Result[tuple[SearchHit, ...]]:  # noqa: ARG002
        return Result.ok(self.hits[:top_k])

    def build(self, docs: tuple[文档条目, ...]) -> Result[None]:
        self.built_with = docs
        return Result.ok_none()


# ── Validators ──


@dataclass(slots=True)
class FakeValidators:
    """假校验器。

    `passed` 可编排：默认**全过**（让第 4 步能跑到 DONE），
    置 `pass_hard=False` 可模拟硬约束不通过，用来验证
    "红线必须退回修正、没有绕过出口"（D-028/D-038）。
    """

    pass_hard: bool = True
    pass_structure: bool = True
    ran: list[str] = field(default_factory=list)

    # ARG002：`payload` 是 Protocol 强制的签名，本步不实现真实校验故不读它。
    # 不能改名或加默认值——那会让假实现不再满足 Protocol，
    # 签名校验（`_assert_protocols`）立刻失败。
    def run_hard_constraint(self, payload: dict[str, object]) -> Result[ValidationResult]:  # noqa: ARG002
        self.ran.append("hard")
        if self.pass_hard:
            return Result.ok(ValidationResult(script="hard", passed=True))
        return Result.ok(
            ValidationResult(
                script="hard",
                passed=False,
                issues=(
                    # 无锚点 → blocking（无法定位即无法核实）
                    _hard_issue(),
                ),
            )
        )

    def run_structure(self, payload: dict[str, object]) -> Result[ValidationResult]:  # noqa: ARG002
        self.ran.append("structure")
        return Result.ok(ValidationResult(script="structure", passed=self.pass_structure))


def _semantic_call_id(request: LlmRequest) -> str:
    """按 D-065 生成 `call_id`：**语义哈希，不是位置序号**。

    ## 为什么这里必须和真实现一样

    D-065 把 `call_id` 定为幂等记账的**语义唯一键**，并明确
    「不由角色、轮次或位置序号生成」。

    原实现用 `f"fake-{len(self.calls)}"` —— 正是被明文禁止的方式。
    后果不是"假的东西不够真"，而是**基于它的测试会测不出真问题**：
    断点续跑重放同一逻辑调用时，真实现给同一个 `call_id`（幂等生效），
    假实现给一个新的序号（看着像新调用）。于是"重放是否幂等"
    这条判据在假环境里**恒真**，接上真适配器才暴露。

    ## 为什么不用 prompt 内容哈希

    同一句 prompt 在不同轮次是不同的逻辑调用；而内容相同、
    角色与门禁不同的调用又是不同的。这里用 `Span` 的三个判别因子
    （role / gate / round_no），与架构 §8.6 一致。
    """
    span = request.span
    parts = [
        request.model,
        span.role if span and span.role else "-",
        span.gate if span and span.gate else "-",
        str(span.round_no) if span and span.round_no is not None else "-",
        str(request.temperature),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"fake-{digest[:16]}"


def _hard_issue() -> ValidationIssue:
    return ValidationIssue(rule="红线", message="桩数据：硬约束不通过", at="")


def _查权限(rel_path: str, 允许: tuple[str, ...]) -> Result[None]:
    """检查 `rel_path` 的顶层目录是否在 `允许` 之内。

    放在**字符串路径**上判断，`user/`、`system/` 一律拒绝。
    `..` 直接拒绝——不做规范化（规范化是 `guard` 的职责，
    假实现不重复那个逻辑，也不假装有）。

    **这一步比真实现弱，且必须承认**：真实现判的是软链接解析后的
    真实落点，假实现没有文件系统，判不了。
    所以"软链接越权"这一类只能在真适配器上测——
    `FakeStorage` 做不到，也不该让人以为它能做到。
    """
    顶层 = rel_path.replace("\\", "/").split("/", 1)[0]
    if 顶层 in (".", "..") or rel_path.startswith("/"):
        return Result.fail(ErrorCode.PATH_ESCAPE, f"越权路径：{rel_path}")
    if 顶层 not in 允许:
        return Result.fail(ErrorCode.PATH_ESCAPE, f"不可访问该目录：{顶层}")
    return Result.ok_none()


# 导入即校验：任何签名不符立刻炸，不留到运行时
_assert_protocols()
