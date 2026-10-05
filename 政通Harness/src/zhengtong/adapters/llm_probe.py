"""D-048 五项连接探测。

## 五项是什么

架构 §4.2 定的检测顺序——**前一项不过就不测后一项**：

| 序 | 项 | 怎么测 | 不过说明什么 |
|---|---|---|---|
| 1 | 网络可达 | 任何 HTTP 往返 | 断网 / 端点地址错 |
| 2 | 鉴权 | 带密钥发一次 | 密钥错、没填 |
| 3 | 模型可调 | 发一次极短请求 | 模型名错、服务端不认 |
| 4 | 支持工具调用 | 带 `tools` 发一次 | 专家团跑不了 |
| 5 | 推理开关 | 按四种写法试探，看 `reasoning_tokens` 降为 0 | 推理关不掉则按开启运行（D-048，D-295 取代原流式项） |

## 为什么逐项而不多发一次

四项检测塞进一个请求里，**失败时无法定位是哪一项坏了**。
用户看到「连接失败」不知道该换密钥还是换模型名。
逐项测的代价是多几次往返，换来的是能报出确切原因。

## 探测用什么提示词

`ping`。**不是**真实业务提示词——连接检测要回答的是
「通不通、能不能干这活」，不是「模型答得好不好」。
用业务提示词会把「模型写得不好」混进「连接有问题」。

## max_tokens 要给足

坑一：`max_tokens` 设小时推理会挤占额度，正文返回 `null`。
探测请求若因这个拿到空正文，会被误判成「模型不可用」。
故探测一律给 `max_tokens=64`，够放一个 `ping`。

## 为什么这里不 import HttpLlm

探测要用 `LlmRequest`/`ToolSchema`（来自端口，可以直接 import），
但**不需要** `HttpLlm` 本身——它只用到 `model` 与 `complete` 两样。
（原先还有 `探测流式`，已按 D-295 随流式探测整体删除。）
故这里声明一个本地 `Protocol` 而不是 import 具体类：

- import `HttpLlm` 会与 `http_llm` 构成**环**（`http_llm.probe` 调本模块），
  类型检查器报 `Cycle detected`，而运行期可能某个 import 顺序才崩；
- 声明 `Protocol` 后依赖方向是单向的，且探测逻辑**可测**——
  给它一个假实现就能测，不必碰真网络。
"""

from __future__ import annotations

from typing import Protocol

from ..domain.errors import ErrorCode, Result
from ..domain.reasoning import 推理开关
from ..ports.llm import LlmRequest, LlmResponse, Message, ToolSchema, 探测结论
from .reasoning_probe import 探测推理

__all__ = ["探测五项", "探测结论"]


#: 探测只用得上这三样。**声明 Protocol 而非 import `HttpLlm`**，理由见模块文档。
class _可探测(Protocol):
    @property
    def model(self) -> str: ...

    # **返回 `LlmResponse` 而不是 `TokenUsage`**——
    # 探测要看正文（空正文要报错），TokenUsage 里没有正文。
    # 声明错了运行期照样跑通（`got.unwrap().text` 拿到的确实是响应），
    # 只在类型检查时暴露。**这类错最险：看起来对，跑起来也对，
    # 直到某次重构按类型去改它。**
    def complete(self, request: LlmRequest) -> Result[LlmResponse]: ...


#: 探测提示词。**极短**——探测的是链路，不是写作能力。
_探测词 = "ping"

#: 探测给的输出额度。**给足**，理由见模块文档。
_探测额度 = 64


def _请求(
    模型: str,
    词: str = _探测词,
    *,
    工具: tuple[ToolSchema, ...] = (),
) -> LlmRequest:
    """构造探测请求。**参数逐个显式列出**，不用 `**kw: object`。

    `**kw` + `dict[str, object]` 会让每个字段都退化成 `object`，
    Pyright 一次报六个「不能赋给 `str`/`float`/`int`」——
    **用 `object` 绕过类型检查，本项目已多次否掉（D-159）**。
    """
    return LlmRequest(
        model=模型,
        messages=(Message(role="user", content=词),),
        temperature=0.0,
        tools=工具,
        max_tokens=_探测额度,
    )


def 探测五项(llm: _可探测) -> Result[探测结论]:
    """跑完五项，逐项收集结论。

    每项失败都带该项对应的错误码与中文原因——
    用户要能据此知道该改哪一项，而不是笼统的「连接失败」。

    **前项失败则后项记为未测**，不硬试：鉴权没过时再发工具请求
    只会得到同一个 401，多花一次往返还多一条误导性错误。
    """
    详情: dict[str, str] = {}

    def 结论(
        *,
        通过项: tuple[str, ...] = (),
        未测: tuple[str, ...] = (),
        推理结论: 推理开关 | None = None,
    ) -> Result[探测结论]:
        return Result.ok(
            探测结论(
                通过项=通过项,
                详情=dict(详情),
                未测=未测,
                推理结论=推理结论 or 推理开关(),
            )
        )

    def 半途而废(
        项名: str,
        说明: str,
        *,
        网络连通: bool = False,
        鉴权: bool = False,
    ) -> Result[探测结论]:
        """前项失败：**已确认过的项如实标通过**，其余记未测。

        不这么做就会说谎——比如工具探测失败时，
        网络与鉴权明明已确认通过，却显示成未通过，
        用户会去查错方向（D-295）。
        """
        详情[项名] = 说明
        已过 = tuple(n for n, ok in (("网络连通", 网络连通), ("鉴权", 鉴权)) if ok)
        # **后项一律未测**，由项序推出来，不手写——
        # 手写会漏：网络失败时鉴权也该是未测（没送达就没有鉴权可言），
        # 早先手写 `("工具调用", "推理开关")` 就漏了鉴权那一项。
        序 = 探测结论.项序
        后项 = 序[序.index(项名) + 1 :]
        return 结论(通过项=已过, 未测=后项)

    # 1 网络可达 + 2 鉴权 + 3 模型可调：一次极短请求即可确认。
    #    拆成三次要多花两次往返，而这三项的失败原因可以从
    #    同一个响应的错误码里区分出来（见 http_llm 的错误映射）。
    got = llm.complete(_请求(llm.model))
    if got.is_error:
        err = got.error
        assert err is not None
        # **状态码 0 = 压根没送达**（`_映射错误` 把所有 `httpx.HTTPError`
        # 统一转成 0）。这才是「网络连通」失败；其余都说明网络是通的。
        if "连不上端点" in err.detail:
            return 半途而废("网络连通", err.detail)
        if err.code is ErrorCode.LLM_AUTH_FAILED:
            return 半途而废("鉴权", err.detail, 网络连通=True)
        return 半途而废("模型调用", err.detail, 网络连通=True, 鉴权=True)

    响应 = got.unwrap()
    if not 响应.text.strip():
        # 有响应但正文空——多半是 max_tokens 不够或模型异常
        详情["网络连通"] = "请求已送达并收到响应"
        详情["鉴权"] = "服务端未拒绝该密钥"
        return 结论(
            # 模型调用是**真失败**（拿到了响应但正文空），
            # 不是未测——只有它后面那两项才是没机会跑。
            通过项=("网络连通", "鉴权"),
            未测=("工具调用", "推理开关"),
        )

    详情["网络连通"] = "请求已送达并收到响应"
    详情["鉴权"] = "服务端未拒绝该密钥"
    详情["模型调用"] = f"模型 {llm.model} 已返回正文"

    # 4 工具调用支持
    工具通过 = True
    工具请求 = _请求(
        llm.model,
        "调用 ping 工具",
        工具=(ToolSchema(name="ping", description="连通性探测工具", parameters={}),),
    )
    got2 = llm.complete(工具请求)
    if got2.is_error:
        err2 = got2.error
        assert err2 is not None
        工具通过 = False
        详情["工具调用"] = f"工具调用探测失败：{err2.detail}"
    else:
        # **服务端接受但不触发不阻断**：有些模型不主动调工具。
        # 专家团运行时靠提示词引导，硬拦在这里会误杀可用端点。
        详情["工具调用"] = "服务端接受了带工具定义的请求"

    # 5 推理开关（D-048）：能不能把推理关掉。
    # 这一项**不参与整体成败**——关不掉也能写公文，
    # 只是要多花推理额度。故它失败时单项记未通过，
    # 而 `远端自检项` 据此只提示不判死。
    推理通过 = True
    推理结论: 推理开关 | None = None
    got3 = 探测推理(llm.model, llm.complete)
    if got3.is_error:
        err3 = got3.error
        assert err3 is not None
        推理通过 = False
        详情["推理开关"] = f"推理探测失败：{err3.detail}"
    else:
        探 = got3.unwrap()
        # **把整个结论带出去**（D-380/D-384）：此前只留一句中文，
        # 序号烂在适配器里，运行时无从知道该用哪种写法——
        # `写法表` 里那四道关闭写法从未用于关闭任何一次请求。
        推理结论 = 探
        详情["推理开关"] = 探.说明()

    return 结论(
        推理结论=推理结论 or 推理开关(),
        通过项=(
            "网络连通",
            "鉴权",
            "模型调用",
            *(("工具调用",) if 工具通过 else ()),
            *(("推理开关",) if 推理通过 else ()),
        ),
    )
