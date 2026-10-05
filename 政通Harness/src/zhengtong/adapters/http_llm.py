"""真实 LLM 适配器（`adapters/http_llm.py`）。

## 职责

实现 `LlmPort` 的两个方法：`complete`（发请求）与 `probe`（D-048 五项检测）。

## 三个实测坑（2026-09-30 真实端点报告）

### 坑一：推理模型有 `reasoning_content`

`max_tokens` 设小时，推理会**挤占输出额度**：

    finish_reason = "length"
    message.content = null
    message.reasoning_content = "正在思考…"

直接取 `content` 得到 `None`，往下走 `.strip()` 全线崩。
故正文为空时**回落到 `reasoning_content`**——那至少是模型真说过的内容，
比空字符串有用。

### 坑二：工具调用的 `arguments` 是 JSON 字符串

    tool_calls[0].function.arguments = '{"文本": "已回传"}'   # 字符串，不是 dict

要二次 `json.loads`。解析失败时**丢掉这一个工具调用**而非整次调用失败——
一个坏参数不该让整轮工作作废。

### 坑三：推理开关三档

`thinking.type`（`enabled`/`disabled`）与 `reasoning_effort` 是不同参数，
实测耗时差异巨大（门禁类 1.8s vs 3.2s，起草类 9.5s vs 20.1s）。
本适配器**只负责如实传输**，不替上层决定开不开——档位策略归 D-150。

## 重试（D-027）

失败重试 3 次、间隔递增。**但只对暂时性失败重试**：
5xx 与网络错误重试，4xx 直接失败——401 重试一万次仍是 401，
而每次重试都要用户等着。

## URL 原样使用（D-004）

用户填完整请求地址，**原样发出**，不拼接、不改写。
所以端点在构造时就存成字符串，全程不碰 `urllib.parse`。

## 密钥

只进 `Authorization` 头，**绝不进 body**——
body 会被日志记录，头不会。
"""

from __future__ import annotations

import json
from typing import Protocol, cast, final, runtime_checkable

from ..domain.errors import ErrorCode, Result
from ..ports.clock import ClockPort
from ..ports.llm import (
    LlmRequest,
    LlmResponse,
    TokenUsage,
    ToolCall,
    归一化结束原因,
    探测结论,
)
from ..ports.secrets import SecretsPort

#: D-027 定的重试次数。**不是可随意调的参数**——
#: 调大它意味着用户对着一个坏端点等更久。
默认重试次数 = 3

#: 递增间隔的基准秒与倍数：1s、2s、4s。
#: 指数退避——固定间隔会让重试打在同一个瞬时故障窗口上。
_退避基准秒 = 1.0


@runtime_checkable
class Transport(Protocol):
    """HTTP 传输。

    **单独抽成端口**不是过度设计：`complete` 与 `probe`
    要测「服务端返回畸形响应时适配器会怎样」，
    而真端点不能稳定地返回畸形响应。
    有了这一层，测试能构造任意 JSON，真端点走 `HttpxTransport`。
    """

    #: **没有 `post_stream`**：流式探测已按 D-295 作废。
    #: 全系统所有请求都走 `post_json`（`HttpxTransport` 把
    #: `httpx.HTTPError` 统一转成状态码 0），不引入第二条传输路径。
    def post_json(
        self, url: str, headers: dict[str, str], body: dict[str, object]
    ) -> tuple[int, dict[str, object]]:
        """POST 一个 JSON，返回 `(状态码, 解析后的 JSON)`。"""
        ...


#: 服务端 `finish_reason` → 本地枚举的对照表**放在端口**
#: （`ports/llm.py` 的 `结束原因对照`），此处不再另存一份——
#: 编排层要判断「认不认识这个结束原因」，两边查的必须是同一张表（D-268）。
_结束原因 = 归一化结束原因


def _用量(数据: object) -> TokenUsage:
    """解析 `usage`。**缺字段一律当 0**，不抛异常——
    服务端不保证给，用量只用于展示与台账（D-077），不值得为它崩掉整次调用。
    """
    字典 = _取字典(数据)
    if 字典 is None:
        return TokenUsage()
    细节 = _取字典(字典.get("completion_tokens_details"))
    推理 = 0
    if 细节 is not None:
        值 = 细节.get("reasoning_tokens")
        if isinstance(值, int):
            推理 = 值
    return TokenUsage(
        prompt_tokens=_整数(字典.get("prompt_tokens")),
        completion_tokens=_整数(字典.get("completion_tokens")),
        reasoning_tokens=推理,
    )


def _取字典(值: object) -> dict[str, object] | None:
    """把任意 JSON 值收窄成 `dict[str, object] | None`。

    ## 为什么 isinstance 之后还要 cast

    `isinstance(x, dict)` 让 Pyright 把类型收成 `dict[Unknown, Unknown]`，
    **元素的类型仍然是未知的**——于是 `for k in 值:` 里的 `k`、
    以及每次 `值[k]` 的实参都报 `reportUnknownVariableType` /
    `reportUnknownArgumentType`。**这不是配置问题，是 `dict` 无参的必然结果。**

    最小复现（`/tmp/probe.py` 那种）里 Protocol 调用返回 `dict[str, object]`
    是正常的，所以问题只出在 isinstance 分支上。

    故用 `cast` 显式声明键值类型：`dict[object, object]`——
    JSON 的键**总是字符串**，值才是异构的，所以这个标注是准确的，
    不是为了让检查器闭嘴而撒的谎。
    """
    if not isinstance(值, dict):
        return None
    原样 = cast("dict[object, object]", 值)
    出: dict[str, object] = {}
    for 键 in 原样:
        出[str(键)] = 原样[键]
    return 出


def _整数(值: object) -> int:
    return 值 if isinstance(值, int) else 0


#: 回落内容的标记（第 14.5 步，D-052 落地）。
#:
#: **为什么必须打标记**：思考是模型的内部草稿，不是给用户看的答案。
#: 回落对**诊断**是对的——「这轮没出正文、只出了思考」是有用信号；
#: 但回落文本若与正文**长得一样**，下游就无从分辨，下一轮会把它
#: 当既有结论读进去，越滚越歪。
#:
#: 故回落仍是诊断路径，**但带标记**：调用方一眼能认出这不是正文。
_思考标记 = "【深度思考·非正文】"


def _正文(消息: dict[str, object]) -> str:
    """取正文；为空时回落到 `reasoning_content` 并**打上标记**（坑一 + D-052）。

    ## 回落的正确形态

    原先回落**原样返回**思考内容，于是它与正文一字不差地混进 `text`。
    现在加了 `_思考标记` 前缀：诊断价值保留（调用方看得到模型确实在思考），
    但**不再冒充正文**。

    ## 为什么不干脆删掉回落

    删了就把「模型这轮没出正文」这个信号一并丢了——那是个真有用的
    诊断信息。正确做法是把它放在**诊断路径**上，而不是内容路径上。
    """
    正文 = 消息.get("content")
    if isinstance(正文, str) and 正文.strip():
        return 正文
    推理 = 消息.get("reasoning_content")
    if isinstance(推理, str) and 推理.strip():
        return f"{_思考标记}{推理}"
    return 正文 if isinstance(正文, str) else ""


def _工具调用(列表: object) -> tuple[ToolCall, ...]:
    """解析 `tool_calls`，**`arguments` 二次 JSON 解析**（坑二）。

    解析失败时该工具调用以**空参数**保留：
    丢掉它会让上层以为模型没调工具从而重复触发；
    带着空参数让上层看到「调用了但参数坏了」，能报更准的错。
    """
    if not isinstance(列表, list):
        return ()
    出: list[ToolCall] = []
    # 同 `_取字典` 的理由：isinstance 之后元素类型仍未知。
    for 项 in cast("list[object]", 列表):
        项字典 = _取字典(项)
        if 项字典 is None:
            continue
        函数 = _取字典(项字典.get("function"))
        if 函数 is None:
            continue
        名字 = 函数.get("name")
        if not isinstance(名字, str) or not 名字:
            continue
        原始参数 = 函数.get("arguments")
        参数: dict[str, object] = {}
        直接给对象 = _取字典(原始参数)
        if 直接给对象 is not None:
            参数 = 直接给对象  # 有些服务端直接给对象
        elif isinstance(原始参数, str) and 原始参数.strip():
            try:
                # `json.loads` 返回 `Any`——**赋给 `object` 不够**，
                # 赋值的瞬间 Any 已经流进变量类型了。用 `cast` 断开。
                解出 = cast("object", json.loads(原始参数))
            except (ValueError, TypeError):
                解出 = None
            解析出 = _取字典(解出)
            if 解析出 is not None:
                参数 = 解析出
        标识 = 项字典.get("id")
        出.append(
            ToolCall(
                id=标识 if isinstance(标识, str) else "",
                name=名字,
                arguments=参数,
            )
        )
    return tuple(出)


def _服务错误码(数据: object) -> str:
    """从错误体里取结构化 `code`（报告实测有 `code`/`type`/`trace_id`）。

    取不到就返回空串——上层据此显示通用中文提示。
    """
    字典 = _取字典(数据)
    if 字典 is None:
        return ""
    错误 = _取字典(字典.get("error"))
    if 错误 is None:
        return ""
    for 键 in ("code", "type"):
        值 = 错误.get(键)
        if isinstance(值, str) and 值:
            return 值
    return ""


@final
class HttpLlm:
    """`LlmPort` 的真适配器。**终态**——不打算被继承，
    故标 `@final`：属性不必逐个标注类型，Pyright 也不再警告。
    """

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        transport: Transport,
        secrets: SecretsPort,
        clock: ClockPort,
        max_retries: int = 默认重试次数,
    ) -> None:
        # 端点原样保存，全程不解析不拼接（D-004）
        self._endpoint = endpoint
        # 探测用的默认模型名。**正常调用时以请求里的 model 为准**
        # （D-082：模型名由用户手填，代码不猜）。
        self._model = model
        self._transport = transport
        self._secrets = secrets
        self._clock = clock
        self._max_retries = max_retries

    # ── 请求体组装 ──

    def _请求体(self, request: LlmRequest) -> dict[str, object]:
        消息: list[dict[str, object]] = []
        for m in request.messages:
            项: dict[str, object] = {"role": m.role, "content": m.content}
            if m.tool_calls:
                项["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.arguments, ensure_ascii=False),
                        },
                    }
                    for c in m.tool_calls
                ]
            if m.tool_call_id:
                项["tool_call_id"] = m.tool_call_id
            消息.append(项)

        体: dict[str, object] = {
            "model": request.model,
            "messages": 消息,
            "temperature": request.temperature,
        }
        # **max_tokens 为 0 时不传**：传 0 会被服务端理解为「不要输出」
        if request.max_tokens > 0:
            体["max_tokens"] = request.max_tokens
        if request.tools:
            体["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        # **额外字段原样附加**（D-245）：只推理关闭探测会用（D-004
        # 要求原样请求，程序不拼接不改写，故这里不做任何翻译）。
        # 为空时什么都不加，正常路径的请求体与从前**逐字节相同**。
        for 键, 值 in request.额外字段:
            体[键] = 值
        return 体

    def _头(self) -> dict[str, str]:
        # **密钥只进头**。body 会被日志记录，头不会。
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._secrets.get_api_key()}",
        }

    # ── 错误映射 ──

    def _映射错误(self, 状态码: int, 体: object) -> tuple[ErrorCode, str]:
        # **状态码 0 不是「被拒」，是「压根没连上」**（D-282）。
        #
        # `HttpxTransport.post_json` 把 `httpx.HTTPError` 统一转成 `(0, {})`
        # （连接被拒、DNS 失败、超时都在里面）。此前 0 掉进兜底分支，
        # 用户看到的是**「请求被拒（0）」**——
        # 既说错了事（不是被拒，是连不上），又报了个没意义的数字 0。
        #
        # 起真本地端点实测时撞到的：端点指了个死端口，
        # 一路报「请求被拒（0）」，用户完全不知道该去检查地址还是网络。
        if 状态码 == 0:
            return (
                ErrorCode.LLM_REQUEST_FAILED,
                "连不上端点：请求没能送达服务器"
                "（地址不通、网络不可达、或服务未启动）。"
                "请检查 config.toml 里的 endpoint 是否完整可访问",
            )
        服务码 = _服务错误码(体)
        if 状态码 in (401, 403):
            return ErrorCode.LLM_AUTH_FAILED, f"鉴权被拒（{服务码 or 状态码}）"
        if 状态码 == 404:
            # 报告实测：`model_not_available` 走 404
            return ErrorCode.LLM_REQUEST_FAILED, f"端点或模型不存在（{服务码 or 状态码}）"
        if 状态码 in (408, 429) or 状态码 >= 500:
            return ErrorCode.LLM_REQUEST_FAILED, f"服务端暂时不可用（{服务码 or 状态码}）"
        return ErrorCode.LLM_REQUEST_FAILED, f"请求被拒（{服务码 or 状态码}）"

    @staticmethod
    def _可重试(状态码: int) -> bool:
        """**只对暂时性失败重试**。

        401/403/404/400 重试一万次结果相同，
        而每次重试都让用户干等——那是拿用户的时间换确定性。

        **但 0 要重试**（D-282）：那是网络层失败，
        服务还没起来、DNS 刚生效、代理刚连上都会这样，
        **重试大概率能成**。用户的要求是「单次请求失败自动重试 3 次」，
        而连不上恰恰是最该重试的那一类。
        """
        return 状态码 in (0, 408, 429) or 状态码 >= 500

    # ── 公开方法 ──

    def complete(self, request: LlmRequest) -> Result[LlmResponse]:
        体 = self._请求体(request)
        头 = self._头()
        末次: tuple[ErrorCode, str] = (ErrorCode.LLM_REQUEST_FAILED, "未发出请求")

        for 次第 in range(self._max_retries + 1):
            if 次第 > 0:
                # 指数退避：1s、2s、4s（D-027「递增间隔」）。
                # **不用 `2 ** n`**：底数为 int、指数为 int 时，
                # Pyright 推成 `Any`（因为 `__pow__` 允许返回 float），
                # 实参会顺着流进 `sleep`。
                # 改成显式的 `float(2) ** 次第`：底数已是 float，
                # 结果类型确定。
                self._clock.sleep(_退避基准秒 * float(2) ** 次第)
            try:
                状态码, 数据 = self._transport.post_json(self._endpoint, 头, 体)
            except UnicodeEncodeError:
                # **单独分支，不并进下面的通用异常**（D-282）。
                #
                # 实测（起真本地端点时撞到的）：密钥或地址里
                # 混入非 ASCII 字符，httpx 按 latin-1 编码 HTTP 头时报
                # `UnicodeEncodeError`。而通用分支会把它说成
                # 「连接失败：UnicodeEncodeError」——
                # **用户完全看不懂，也不知道该改哪里**。
                #
                # 这不是臆想的场景：密钥由用户手填、地址由用户粘贴，
                # 混进一个全角标点或中文字符完全可能。
                #
                # **也不该重试**：这不是网络抖动，是输入问题，
                # 重试 3 次只是让用户多等三倍时间看同一句话。
                return Result.fail(
                    ErrorCode.LLM_REQUEST_FAILED,
                    "密钥或请求地址含非 ASCII 字符，无法作为 HTTP 头发送"
                    "（HTTP 头只能传 ASCII）。请检查 system/config.toml 里的"
                    " api_key 与 endpoint 是否混入了中文或全角标点",
                )
            except Exception as exc:
                # **只记异常类型名，不嵌 `str(exc)`**。
                #
                # 实测 httpx 1.x 当前的消息不含 URL/密钥
                # （`ConnectError` 只带 errno、`ConnectTimeout` 只带 "timed out"），
                # **但那是第三方库当前版本的行为**。若用户把 key 写进 URL query
                # （常见错误），而 httpx 换版本、加代理或开了 http2 后
                # 把 URL 放进异常消息——密钥就直接进日志了。
                #
                # D-030 要求「任何日志不得含密钥」，
                # **不能把这条建立在第三方库的当前实现上**。
                #
                # 保留类型名是为了排查：`ConnectError` 多半是端点地址错或断网，
                # `ConnectTimeout` 多半是网络不通。丢了它用户只能自己猜。
                末次 = (
                    ErrorCode.LLM_REQUEST_FAILED,
                    f"连接失败：{type(exc).__name__}",
                )
                continue

            if 状态码 == 200:
                响应 = self._解析(数据)
                if 响应 is not None:
                    return Result.ok(响应)
                末次 = (ErrorCode.LLM_REQUEST_FAILED, "响应结构无法识别")
                continue

            末次 = self._映射错误(状态码, 数据)
            if not self._可重试(状态码):
                # 确定性失败，重试无意义
                return Result.fail(末次[0], 末次[1])

        if self._max_retries > 0:
            return Result.fail(
                ErrorCode.LLM_RETRY_EXHAUSTED,
                f"重试 {self._max_retries} 次仍失败：{末次[1]}",
            )
        return Result.fail(末次[0], 末次[1])

    def _解析(self, 数据: object) -> LlmResponse | None:
        字典 = _取字典(数据)
        if 字典 is None:
            return None
        选择 = 字典.get("choices")
        if not isinstance(选择, list) or not 选择:
            return None
        首 = _取字典(cast("list[object]", 选择)[0])
        if 首 is None:
            return None
        消息 = _取字典(首.get("message"))
        if 消息 is None:
            return None
        标识 = 字典.get("id")
        原始结束 = 首.get("finish_reason")
        return LlmResponse(
            text=_正文(消息),
            finish_reason=_结束原因(原始结束),
            raw_finish_reason=原始结束 if isinstance(原始结束, str) else "",
            usage=_用量(字典.get("usage")),
            tool_calls=_工具调用(消息.get("tool_calls")),
            call_id=标识 if isinstance(标识, str) else "",
        )

    # ── 供探测模块使用的公开访问器 ──
    #
    # **不给探测模块开私有属性后门**（`llm._endpoint`）。
    # 那样它就成了「本模块的内部结构可以随便依赖」的先例，
    # 改字段名时不会有任何提示，直到运行期才炸。
    # 访问器是显式契约：改这里会改到类型检查。

    def close(self) -> None:
        """释放底层连接。

        **真传输持有 HTTP 连接池**，长跑不关会耗尽。
        传输层没有 `close` 时静默跳过——那说明用的是假传输，
        本来就没连接可关。
        """
        关 = getattr(self._transport, "close", None)
        if callable(关):
            _ = 关()  # 有些 close 返回 client，丢弃即可

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def model(self) -> str:
        return self._model

    def probe(self) -> Result[探测结论]:
        """D-048 五项检测。

        逐项发**小请求**——连接检测要的是「通不通、能不能干这活」，
        不是「模型答得好不好」，所以不跑真实业务提示词。
        **返回逐项结论而非一个总判**（D-295）：体检要回答
        「这台机器到底行不行」，中途失败后面的项根本没跑，
        笼统报一条「连接失败」等于让用户自己猜还有几项没验。
        第 5 项是推理开关（D-048），取代原先的流式探测。

        实现放在 `llm_probe.py`（与本文件分开，因为它要构造
        一整套探测请求，塞在这里会把 `complete` 的逻辑冲淡）。
        局部 import 而非文件头：探测模块要用本类的类型，
        放到文件头会成环。
        """
        from .llm_probe import 探测五项

        return 探测五项(self)
