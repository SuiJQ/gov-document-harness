"""稳定错误码。

**这些码是接口，不是给人看的文案。** 界面显示的中文提示在 adapters 层
（`error_map.py`），这里只负责可判定的分类与可断言的稳定标识。

为什么不直接抛异常（D-064 / 架构 §8.5）：
**拒绝是正常返回，不是异常。** 门禁不通过、用户拒答、路径越界——
这些都是系统预期内的分支，走 `Result` 而不是栈回溯。
只有真正不可恢复的编程错误才允许抛异常。

因此 `AppError` 不是 `Exception` 的子类，而是一个**返回值类型**。
它携带 `code`（供断言与测试）、`detail`（供日志与界面），
不携带栈信息——因为它表示的是一次正常发生的拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Generic, TypeVar


class ErrorCode(StrEnum):
    """稳定错误码。

    用 `StrEnum`（3.11 内置）而非 `str, Enum`：前者天生就是字符串，
    可直接进 JSON 与日志，而不必在每个序列化点写 `.value`——
    漏一处就是一处线上 bug。这也是 D-079 提版本的实得之一。

    **码一经发布不得更名、不得复用。** 界面文案可以改，码不能改：
    日志里可能躺着几个月前的旧码，改名等于让它们全部失去含义。
    废弃的码保留原样并标注"已废弃"，不再使用但也不删除。
    """

    # ── 用户输入与需求 ──
    REQUIREMENT_EMPTY = "E001"  # 需求为空
    TIER_UNKNOWN = "E002"  # 档位名无法识别
    USER_REFUSED_ANSWER = "E003"  # 用户拒答，且字段为必填（D7 等）

    # ── 方向确认单（D-Card）──
    DCARD_FIELD_MISSING = "E010"  # 必填字段缺失且 provenance 声明为用户来源
    DCARD_PROVENANCE_INVALID = "E011"  # 必填字段被标为 LEAD_INFERRED（D-069/D-040）
    DCARD_INFERRED_NOT_ALLOWED = "E012"  # 非 D8 字段出现 LEAD_INFERRED

    # ── 检索与知识 ──
    RETRIEVAL_FAILED = "E020"  # 检索不可用（不应发生：中文主路径零依赖）
    RETRIEVAL_EMPTY = "E021"  # 检索无候选，且必须出处的门禁要据此拦截

    # ── 模型连接 ──
    LLM_AUTH_FAILED = "E030"
    LLM_REQUEST_FAILED = "E031"
    LLM_NO_TOOL_CALL = "E032"
    LLM_CONTEXT_OVERFLOW = "E033"
    LLM_RETRY_EXHAUSTED = "E034"  # 重试 3 次仍失败（D-027）
    LLM_ENDPOINT_REJECTED = "E035"  # 端点拒绝了请求（端点/协议不匹配）
    LLM_OUTPUT_TRUNCATED = "E036"  # 输出触到上限被截断——**不是完成**，不得交付
    LLM_CONTENT_FILTERED = "E037"  # 服务端合规拦截——红线，硬退回
    LLM_FINISH_UNKNOWN = "E038"  # 结束原因不认识——**无法断定内容完整**

    # ── 门禁 ──
    GATE_FAILED = "E040"
    GATE_NO_EVIDENCE = "E041"  # 无证据不通过
    GATE_REWRITE_EXHAUSTED = "E042"  # 返工上限用尽（D-050 上限 2 次）
    GATE_RESPONSIBLE_NOT_IN_TIER = "E043"  # 取证方不在该档编制内（D-049 配平铁律）

    # ── 导出 ──
    PLACEHOLDER_REMAIN = "E050"  # 残留占位符，禁止导出（D-034）
    UNSOURCED_ASSERTION = "E051"  # 未标注来源的数据型断言（D-069）

    # ── 文件与路径 ──
    PATH_ESCAPE = "E060"  # 越出允许的根（D-076）
    TOOL_NOT_ALLOWED = "E065"  # 角色不在该工具白名单内（D-042 双重白名单）
    TOOL_UNKNOWN = "E066"  # 调用了不存在的工具名（D-042 已砍除的 fs.* 也走这里）
    TOOL_NOT_WIRED = "E067"  # 回调未接线（D-406）。**与 E065 分开不是洁癖**

    # `_未接线` 原先返回 `TOOL_NOT_ALLOWED`（E065「角色不在白名单内」）。
    # 实测：`msg.report_to_lead` 的白名单**确实包含**调用者，
    # 仍报 E065——排障时会一路查到角色配置，而白名单没问题。
    # 「拼错名字 / 越权 / 缺回调」三件事必须一眼可分，
    # 否则每次都要读完调用链才知道真因。
    FILE_UNREADABLE = "E061"
    FILE_TOO_LARGE = "E062"
    DOCX_MALFORMED = "E063"  # 非合法 zip（D-078）
    DOCX_BOMB_SUSPECTED = "E064"  # 解压体积或压缩比超限（D-078）

    # ── 配置与存储 ──
    CONFIG_INVALID = "E070"
    CONFIG_SECRET_LEAK = "E071"  # 待脱敏内容出现在不该出现的位置（D-030）
    STORAGE_WRITE_FAILED = "E072"

    # ── 编排 ──
    KERNEL_STALLED = "E080"  # 状态机无法推进（防挂死）
    ROLE_ROUND_LIMIT = "E081"  # 成员轮次超限（D-058）
    RUN_RESUMED = "E082"  # 从检查点续跑，非错误，仅记录
    # ── 状态自检（E09x）：与门禁配平类错误区分 ──
    # 审计 D-115：原先这两种错误都借用 E043/E041，而那两码
    # 字面专指门禁取证。用户看到的是「门禁取证问题」，
    # 实际问题却是「状态被污染」，排查方向会被带偏。
    STATE_ROLE_NOT_IN_TIER = "E090"  # 状态里含本档未编入的角色
    STATE_UNKNOWN_GATE = "E091"  # 状态里出现未知门禁编号

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class AppError:
    """一次正常的拒绝。

    **不是异常**——见模块文档。不携带栈信息：它表示的是
    "系统按设计拒绝了一次操作"，而不是"程序崩了"。
    """

    code: ErrorCode
    detail: str = ""
    #: **诊断用**，不进用户文案（D-412）。
    #:
    #: 为什么需要它：重试发生在 `http_llm.complete` **内部**（D-027），
    #: 而 `LlmPort` 协议明写「上层不感知重试」——
    #: 编排层只看得到最终成败，于是 `model.retry` 事件**发不出来**
    #: （D-408 当时刻意没假装能发）。
    #:
    #: 失败路径上没有 `LlmResponse` 可挂字段，`Result` 又是通用容器，
    #: 改它会波及全仓所有拒绝。故挂在**这一次拒绝自己的**诊断位上——
    #: 成功路径的重试次数由 `LlmResponse.重试次数` 带，两者互补。
    #:
    #: **可选且默认空**：不构造它的调用点一律不受影响（全仓仅一处构造）。
    诊断: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.code}] {self.detail}" if self.detail else f"[{self.code}]"


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Result(Generic[T]):
    """成功或拒绝，二者必居其一。

    成功值放在 `value` 里而不是靠泛型协变技巧绕：
    **先判断 `is_ok` 再取 `value`**，类型检查器就能把成功路径的值
    类型推对；失败路径上的 `value` 是 `None`，取它本身就是编码错误。

    用 `Generic[T]` 而非 3.12 的 `class Result[T]`：PEP 695 语法在
    3.11 不可用（D-079 定 3.11），Ruff 会直接报 invalid-syntax。

    ```python
    r = load_config()
    if r.is_ok:
        cfg = r.value      # 类型是 Config，不是 object
    else:
        show(r.error.detail)   # 此处 r.value 是 None
    ```
    """

    error: AppError | None = None
    value: T | None = None

    @property
    def is_ok(self) -> bool:
        return self.error is None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    def unwrap(self) -> T:
        """取出成功值；处于错误态时抛 `RuntimeError`。

        这里是全 L0 唯一允许抛异常的地方，且只因为
        "在错误态调用 unwrap"本身就是编码错误。
        """
        if self.error is not None:
            raise RuntimeError(f"对错误结果调用了 unwrap：{self.error}")
        if self.value is None:
            raise RuntimeError("成功态但无值：请用 Result.ok(value) 构造")
        return self.value

    @classmethod
    def ok(cls, value: T) -> Result[T]:
        return cls(value=value)

    @classmethod
    def ok_none(cls) -> Result[None]:
        """成功且无值。

        单独提供是因为 `Result.ok(None)` 推不出 `T = None`
        （`NoneType` 不参与类型推断），检查器会判为 Unknown。
        校验类函数几乎都用这个，故给它一个正式名字。
        """
        return Result(error=None, value=None)

    @classmethod
    def fail(
        cls,
        code: ErrorCode,
        detail: str = "",
        诊断: dict[str, object] | None = None,
    ) -> Result[Any]:  # pyright: ignore[reportExplicitAny]  # 见上方三方案论证
        """构造失败结果。

        `诊断` 是**可选的**诊断位（D-412）：不进用户文案、不进 `__str__`，
        只给需要记过程留痕的上层读（如 `model.retry` 的重试次数）。
        不传就是空 dict——全仓既有的失败构造点一律不受影响。

        **为什么是 `Any`，以及为什么试过 `Self` 不行**
        （D-085：三方案均已实测，记下来免得后人再试一遍）：

        失败结果的 `value` 恒为 `None`，没有任何成功值可携带，
        所以调用点的 `T` 无从推断。三个候选：

        - `Result[Never]`：不变性导致 `Result[Never]` 不能赋给 `Result[None]`，报错。
        - `Self`：语法合法，但 `Result.fail(...)` **裸调用**时 `T` 无处绑定，
          退回 `Result[Unknown]`，等于把问题换个地方暴露。
        - `Result[Any]`：`Any` 双向兼容，各处返回类型都接受它。这是唯一可行解。

        `Any` 在此处的代价（放行下游类型检查）是可接受的：
        失败路径上能取到的只有 `None`，且 `unwrap()` 有断言兜着。
        真正需要携带类型信息的是**成功路径**，那条路走 `ok(value)`，
        类型完全严格。

        故此处是全项目**唯一**被允许出现 `Any` 的签名，
        配置里对 `reportExplicitAny` 做了定点豁免并注明此处。
        """
        return Result(error=AppError(code, detail, 诊断 or {}), value=None)


#: 已废弃但保留原样的码。留着是为了让旧日志里的码仍能查到含义（D-064 错误码约定）。
DEPRECATED_CODES: Final[frozenset[ErrorCode]] = frozenset()
