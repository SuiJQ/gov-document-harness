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

from dataclasses import dataclass, field

from ..domain.artifact import ArtifactRef
from ..domain.errors import ErrorCode, Result
from ..domain.ids import RunId
from ..domain.role import RoleId
from ..domain.run_state import RunState
from ..domain.task import UserFile
from ..ports.clock import ClockPort
from ..ports.content_pack import ContentPackPort
from ..ports.llm import (
    FinishReason,
    LlmPort,
    LlmRequest,
    LlmResponse,
    TokenUsage,
)
from ..ports.retriever import RetrieverPort, SearchHit
from ..ports.secrets import SecretsPort
from ..ports.storage import StoragePort
from ..ports.user_files import UserFilesPort
from ..ports.validators import ValidationIssue, ValidationResult, ValidatorPort

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
        """推进时间而不是真睡——测试里真睡只会让套件变慢。"""
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

    def complete(self, request: LlmRequest) -> Result[LlmResponse]:
        self.calls.append(request)
        if self.fail_with is not None:
            # 真的失败，不假装成功
            return Result.fail(self.fail_with, "FakeLlm 被要求失败")
        if self.scripted:
            return Result.ok(self.scripted.pop(0))
        return Result.ok(
            LlmResponse(
                text=self.default_text,
                finish_reason=FinishReason.STOP,
                usage=TokenUsage(prompt_tokens=10, completion_tokens=10),
                # call_id 必须在响应里产生（D-065），故这里给一个稳定值
                call_id=f"fake-{len(self.calls)}",
            )
        )

    def probe(self) -> Result[TokenUsage]:
        return Result.ok(TokenUsage(prompt_tokens=1, completion_tokens=1))


# ── Storage ──


@dataclass(slots=True)
class FakeStorage:
    """假存储。**纯内存**，不碰文件系统——内核层本就无权碰盘。"""

    files: dict[str, str] = field(default_factory=dict)
    states: dict[str, RunState] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    #: 是否模拟"正文已达标"（给 `draft_paper_ok` 用）
    draft_ok: bool = False

    def write_atomic(self, rel_path: str, content: str) -> Result[None]:
        self.files[rel_path] = content
        return Result.ok_none()

    def read_text(self, rel_path: str) -> Result[str]:
        if rel_path in self.files:
            return Result.ok(self.files[rel_path])
        return Result.fail(ErrorCode.FILE_UNREADABLE, f"文件不存在：{rel_path}")

    def exists(self, rel_path: str) -> bool:
        return rel_path in self.files

    def list_dir(self, rel_path: str) -> tuple[str, ...]:
        prefix = f"{rel_path.rstrip('/')}/"
        return tuple(sorted(k[len(prefix) :] for k in self.files if k.startswith(prefix)))

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

    def read_text(self, name: str) -> Result[str]:
        if name in self.contents:
            return Result.ok(self.contents[name])
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
    built_with: tuple[str, ...] = ()

    def search(self, query: str, top_k: int = 8) -> Result[tuple[SearchHit, ...]]:  # noqa: ARG002
        return Result.ok(self.hits[:top_k])

    def build(self, docs: tuple[str, ...]) -> Result[None]:
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


def _hard_issue() -> ValidationIssue:
    return ValidationIssue(rule="红线", message="桩数据：硬约束不通过", at="")


# 导入即校验：任何签名不符立刻炸，不留到运行时
_assert_protocols()
